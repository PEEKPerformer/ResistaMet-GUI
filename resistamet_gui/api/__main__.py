"""Run the API as a child process.

This is what the Tauri shell will launch. It exists in step 1 to prove the
session shape end to end — a real process, a real socket, a real shutdown —
rather than to ship a packaged sidecar.

Three things a parent process needs, and gets here:

* **A handshake.** The port and the token are printed to stdout as one JSON
  line and nothing else ever goes to stdout, so the parent can read exactly
  one line and know where to connect. Logs go to stderr.
* **A watchdog.** When the parent dies, our stdin closes; that is the signal to
  shut down. Without it a killed parent leaves a process holding the
  instrument.
* **An ordered shutdown.** Stop the run, wait for it to turn the output off and
  finalize the file, then exit — with a grace period long enough for a stop to
  land during a slow VISA operation.

``--check-visa`` serves nothing: it prints what VISA this machine has as one
JSON line and exits, which is how a frozen install is diagnosed on a PC with
no development tools.
"""
import argparse
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import socket
import sys
import threading

from ..config import ConfigManager
from ..session.manager import MeasurementSession
from .app import create_app
from .event_hub import EventHub

logger = logging.getLogger(__name__)

#: Long enough for a stop to land during a slow VISA read and for the run to
#: turn the output off and finalize; see the stop-latency table in the design.
SHUTDOWN_GRACE_S = 35.0

#: How long the server may wait for open connections once it is told to
#: exit. A client that never hangs up must not stand between a signal and
#: the run being stopped.
SERVER_DRAIN_S = 3


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="python -m resistamet_gui.api",
        description="Localhost API over a headless ResistaMet session.",
    )
    parser.add_argument("--host", default="127.0.0.1",
                         help="Bind address (default: 127.0.0.1). Do not expose this.")
    parser.add_argument("--port", type=int, default=0,
                         help="Port, or 0 to let the OS choose (default: 0).")
    parser.add_argument("--config", default=None, metavar="PATH",
                         help="config.json to read profiles from. Required in a "
                              "sidecar: relative paths would resolve against the "
                              "parent's working directory.")
    parser.add_argument("--token", default=None,
                         help="Bearer token. Generated and printed if omitted.")
    parser.add_argument("--simulate", action="store_true",
                         help="Run against the in-package simulator.")
    parser.add_argument("--sim-resistance", type=float, default=100.0, metavar="OHMS")
    parser.add_argument("--no-watchdog", action="store_true",
                         help="Do not exit when stdin closes (for interactive use).")
    parser.add_argument("--check-visa", nargs="?", const="quiet", default=None,
                         choices=["quiet", "bus"], metavar="quiet|bus",
                         help="Print what VISA this machine has as JSON and exit, "
                              "instead of serving. 'bus' also enumerates resources, "
                              "which puts traffic on the instrument bus.")
    parser.add_argument("--visa-library", default=None, metavar="'' | @ivi | @py | PATH",
                         help="Override the machine's configured VISA backend, for "
                              "--check-visa.")
    parser.add_argument("--gpib-interface", default=None, metavar="'' | PRLGX-...::INTFC",
                         help="Override the machine's configured GPIB interface, for "
                              "--check-visa. Only 'bus' opens it.")
    parser.add_argument("--allow-remote", action="store_true",
                         help="Permit a --host that is not a loopback address. The "
                              "token then crosses the network in plaintext.")
    args = parser.parse_args(argv)
    if not args.allow_remote and not _is_loopback(args.host):
        parser.error(f"--host {args.host} is not a loopback address: the API has no "
                     "transport security and the token would cross the network in "
                     "plaintext. Pass --allow-remote if that is really intended.")
    return args


class _RedactToken(logging.Filter):
    """Keep the bearer token out of the log.

    A browser cannot set a header on a WebSocket, so the token travels in
    the query string, and uvicorn logs the path of every WebSocket it accepts
    -- query string included, whatever ``access_log`` says. stderr is
    inherited by the parent and may end up in a file.
    """

    _TOKEN = re.compile(r'(token=)[^&\s"\']+')

    def _clean(self, value):
        return self._TOKEN.sub(r'\1***', value) if isinstance(value, str) else value

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._clean(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._clean(arg) for arg in record.args)
        return True


def _is_loopback(host: str) -> bool:
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _watch_stdin(on_eof):
    """Exit when the parent goes away: our stdin closes when it dies."""
    def run():
        try:
            for _ in sys.stdin:
                pass
        except Exception:
            pass
        logger.info("stdin closed; shutting down")
        on_eof()

    thread = threading.Thread(target=run, name="stdin-watchdog", daemon=True)
    thread.start()
    return thread


def _exit_through_the_shutdown_on_signals(server) -> None:
    """Make SIGTERM, SIGINT and Ctrl+Break end the process the ordered way.

    While it serves, uvicorn handles these itself and stops serving. It then
    puts back whatever handler was there before and raises the signal again
    -- and with the default handler in place that kills the process on the
    spot, before the ``finally`` in :func:`main` has stopped the run. A
    Python-level handler here is what gets put back, so the second delivery
    is harmless and ``main`` carries on to its shutdown.

    Closing the console window on Windows (CTRL_CLOSE_EVENT) is not a signal
    Python can handle; the parent closing stdin is the route that covers it.
    """
    def ask_to_exit(signum, frame):
        server.should_exit = True

    for name in ('SIGTERM', 'SIGINT', 'SIGBREAK'):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, ask_to_exit)


def build(args):
    """Wire session, hub and app together. Returns (app, session)."""
    hub = EventHub()
    session = MeasurementSession(hub.publish)
    # The API can tell its client that a save failed, so it asks to be told.
    # persist_on_open=False: starting the server writes nothing; migrations
    # apply in memory and reach the file with the first deliberate save.
    options = dict(raise_on_save_error=True, persist_on_open=False)
    config = (ConfigManager(config_file=args.config, **options)
              if args.config else ConfigManager(**options))
    token = args.token or secrets.token_urlsafe(32)
    app = create_app(session, token=token, config=config, hub=hub)
    return app, session, token


def check_visa(args) -> int:
    """Print this machine's VISA situation as one JSON line. Serves nothing.

    The diagnostic a frozen install needs: a VISA library can be present and
    still be unable to open a bus (NI-VISA without the GPIB driver behind it
    is the case that has cost the lab an afternoon). Exit status is 0 when a
    ResourceManager opened, 1 when none could.
    """
    from .. import visa_backend

    library, interface = args.visa_library, args.gpib_interface
    if library is None or interface is None:
        # Read-only: a diagnostic must not create the config it is asked
        # about, nor run the migrations that rewrite profiles.
        config = (ConfigManager(config_file=args.config, read_only=True)
                  if args.config else ConfigManager(read_only=True))
        if library is None:
            library = config.get_visa_library()
        if interface is None:
            interface = config.get_gpib_interface()
    report = visa_backend.report(library, probe_bus=(args.check_visa == 'bus'),
                                 gpib_interface=interface)
    print(json.dumps(report), flush=True)
    return 0 if report.get('ok') else 1


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(_RedactToken())

    if args.check_visa is not None:
        return check_visa(args)

    if args.simulate:
        from ..simulator import enable_simulation
        enable_simulation(dut_resistance_ohms=args.sim_resistance, model="2420")

    import uvicorn

    app, session, token = build(args)

    # Bind before serving so the handshake can name the real port: with
    # --port 0 the OS picks it, and the parent cannot connect to a port we
    # only learn about later.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != 'win32':
        # On POSIX this only lets a restart rebind a port in TIME_WAIT. On
        # Windows it would let another local process bind the same port while
        # we hold it, which is the threat the token exists for.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.host, args.port))
    listener.listen(128)
    port = listener.getsockname()[1]

    config = uvicorn.Config(app, log_config=None, access_log=False,
                             timeout_graceful_shutdown=SERVER_DRAIN_S)
    server = uvicorn.Server(config)
    app.state.api.server = server

    # One line, on stdout, once: everything the parent needs to connect.
    print(json.dumps({'url': f"http://{args.host}:{port}", 'token': token,
                       'pid': os.getpid()}), flush=True)

    def parent_went_away():
        # The run first: it can be turning the output off while the server
        # is still closing its connections.
        session.stop()
        server.should_exit = True

    if not args.no_watchdog:
        _watch_stdin(parent_went_away)

    _exit_through_the_shutdown_on_signals(server)
    try:
        server.run(sockets=[listener])
    finally:
        # The run gets its grace period before the process goes away, so the
        # output is off and the file is finalized.
        session.close(timeout=SHUTDOWN_GRACE_S)
        if session.state != 'idle':
            logger.error("the run did not end within %.0f s of being stopped; exiting "
                         "anyway. The source output may still be ON and the data file "
                         "is not finalized -- check the instrument's front panel.",
                         SHUTDOWN_GRACE_S)


if __name__ == "__main__":
    sys.exit(main())

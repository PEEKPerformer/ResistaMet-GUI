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
import json
import logging
import os
import secrets
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
    return parser.parse_args(argv)


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


def build(args):
    """Wire session, hub and app together. Returns (app, session)."""
    hub = EventHub()
    session = MeasurementSession(hub.publish)
    config = ConfigManager(config_file=args.config) if args.config else ConfigManager()
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

    if args.visa_library is not None:
        library = args.visa_library
    else:
        config = ConfigManager(config_file=args.config) if args.config else ConfigManager()
        library = config.get_visa_library()
    report = visa_backend.report(library, probe_bus=(args.check_visa == 'bus'))
    print(json.dumps(report), flush=True)
    return 0 if report.get('ok') else 1


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")

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
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.host, args.port))
    listener.listen(128)
    port = listener.getsockname()[1]

    config = uvicorn.Config(app, log_config=None, access_log=False)
    server = uvicorn.Server(config)
    app.state.api.server = server

    # One line, on stdout, once: everything the parent needs to connect.
    print(json.dumps({'url': f"http://{args.host}:{port}", 'token': token,
                       'pid': os.getpid()}), flush=True)

    if not args.no_watchdog:
        _watch_stdin(lambda: setattr(server, 'should_exit', True))

    try:
        server.run(sockets=[listener])
    finally:
        # The run gets its grace period before the process goes away, so the
        # output is off and the file is finalized.
        session.close(timeout=SHUTDOWN_GRACE_S)


if __name__ == "__main__":
    sys.exit(main())

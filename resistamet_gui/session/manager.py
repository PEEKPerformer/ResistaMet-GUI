"""One instrument, one run at a time, driven without a GUI.

``MeasurementSession`` is the headless counterpart of the QThread adapters in
``workers.py``: it owns the acquisition thread, the run state a client can
query, and the commands a client can send. The procedures it drives are the
same ``ContinuousRun`` and ``VdpRun`` the GUI uses, so there is no second
implementation of a measurement anywhere.

It contains no procedure logic — every method here is state, threading or
dispatch. Anything about how a measurement works belongs in the run modules.

Events go to the sink the caller supplies (a list in tests, the API's hub in
the sidecar). The session adds nothing to them beyond the run id the emitter
already stamps.

Single-run rule: one instrument, one run. ``start`` refuses unless the session
is idle, and ``identify`` and ``start`` exclude each other, because both open
the same VISA resource.
"""
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from ..schema.resolve import resolve_run_settings
from ..schema.settings_modes import ClientInfo
from ..schema.spots import SpotRequest, check_spot_mode
from .continuous_run import ContinuousRun
from .control import InvalidPromptChoice, RunControl
from .emitter import EventEmitter
from .instrument_lock import HeldInstrument, InstrumentBusy, hold_instrument
from .status import InstrumentInfo, PendingPrompt, SessionStatus
from .vdp_run import VdpRun

logger = logging.getLogger(__name__)

#: States a session can be in. 'stopping' is brief: the run is winding down.
STATES = ('idle', 'identifying', 'running', 'paused', 'awaiting_prompt', 'stopping')

VDP_MODE = 'vdp'


class SessionBusy(RuntimeError):
    """A command arrived in a state that cannot serve it."""


class MeasurementSession:
    """Runs one measurement at a time and reports it as events."""

    def __init__(self, sink: Callable[[Any], None], clock: Callable[[], float] = time.time):
        self._sink = sink
        self._clock = clock
        self._lock = threading.Lock()
        self._state = 'idle'
        self._run_id: Optional[str] = None
        self._run_count = 0
        self._run = None
        self._control: Optional[RunControl] = None
        self._thread: Optional[threading.Thread] = None
        self._last_event_seq = 0
        #: The instrument as last seen by a run or by identify(); see status().
        self._instrument: Optional[InstrumentInfo] = None
        self._mode: Optional[str] = None
        #: Whether the current run has sent its run_ended; read by _execute.
        self._run_ended_seen = False
        #: The address of the current (or last) run, for _record.
        self._run_address: Optional[str] = None
        #: The address whose output the last run left in doubt (run_ended
        #: with output_verified false), or None. The first connection back to
        #: it turns the output off before anything else and clears this.
        self._output_unknown_at: Optional[str] = None

    @property
    def output_unknown_at(self) -> Optional[str]:
        """Address where the instrument output may still be on, or None."""
        return self._output_unknown_at

    # --- state ------------------------------------------------------------

    @property
    def state(self) -> str:
        """Derived from the control where the run owns the truth."""
        with self._lock:
            state, control = self._state, self._control
        if state != 'running' or control is None:
            return state
        if control.pending_prompt is not None:
            return 'awaiting_prompt'
        if control.paused:
            return 'paused'
        return state

    def status(self) -> Dict[str, Any]:
        with self._lock:
            run_id, mode, run = self._run_id, self._mode, self._run
        prompt = self._control.pending_prompt if self._control else None
        # Built through the model so the reply and its exported contract
        # cannot drift; callers still get the plain dict they always did.
        return SessionStatus(
            state=self.state,
            run_id=run_id,
            mode=mode,
            path=getattr(run, 'filename', '') or None,
            last_seq=self._last_event_seq,
            pending_prompt=None if prompt is None else PendingPrompt(
                prompt_id=prompt.prompt_id,
                kind=prompt.kind,
                options=list(prompt.options),
                requires_human=prompt.requires_human,
                detail=dict(prompt.detail),
            ),
            instrument=self._instrument,
        ).model_dump()

    # --- commands ---------------------------------------------------------

    def start(self, profile: Dict[str, Any], mode: str, sample_name: str, username: str,
              overrides: Optional[Dict[str, Any]] = None,
              prompt_timeout_s: float = 900.0,
              spot: Optional[Any] = None,
              client: Optional[Any] = None) -> str:
        """Resolve settings, then run them. Returns the run id immediately.

        ``spot`` (a ``SpotRequest`` or its dict) says which placement of the
        four-point probe this run is. It rides in the run settings as
        ``settings['spot']``, beside the sections the profile provides, so the
        run procedure reads it the same way whoever started the run.

        ``client`` (a ``ClientInfo`` or its dict) names the program that asked
        for the run. It rides the same way, as ``settings['client']``, and
        ``build_metadata`` writes it into the file header.

        Raises ``SessionBusy`` unless idle, ``InstrumentBusy`` when another
        process holds the instrument, and ``ValueError`` when the strict
        resolver rejects the request or the spot — a run that cannot be
        described should never reach the instrument.
        """
        resolved = resolve_run_settings(profile, mode, overrides or {}, strict=True)
        if not resolved.ok:
            raise ValueError('; '.join(f"{i.key}: {i.message}" for i in resolved.issues
                                        if i.severity == 'error'))
        if spot is not None:
            check_spot_mode(mode)
            resolved.settings['spot'] = SpotRequest.model_validate(spot).model_dump()
        if client is not None:
            resolved.settings['client'] = ClientInfo.model_validate(client).model_dump()

        with self._lock:
            if self._state != 'idle':
                raise SessionBusy(f"session is {self._state}")
            # The instrument is taken here, before "started" is answered, so a
            # bus held by another process is a refusal the caller sees, not a
            # run that fails a moment later. The run releases it in cleanup.
            address = resolved.settings.get('measurement', {}).get('gpib_address', '')
            held = HeldInstrument(address)  # raises InstrumentBusy
            # The last run at this address may have left the output on. The
            # run's *RST turns it off before any configuration; the run says
            # so (log output_off_recovered), and _record clears the doubt.
            recover_output = (address == self._output_unknown_at)
            try:
                self._run_count += 1
                run_id = f"run-{self._run_count}"
                control = RunControl(run_id=run_id)
                emitter = EventEmitter(self._record, run_id=run_id, clock=self._clock)
                if mode == VDP_MODE:
                    run = VdpRun(sample_name, username, resolved.settings, control, emitter,
                                  safety_ack='prompt', prompt_timeout_s=prompt_timeout_s,
                                  instrument_lock=held, recover_output=recover_output)
                else:
                    run = ContinuousRun(mode, sample_name, username, resolved.settings,
                                         control, emitter, safety_ack='prompt',
                                         prompt_timeout_s=prompt_timeout_s,
                                         instrument_lock=held, recover_output=recover_output)
                thread = threading.Thread(target=self._execute, args=(run, held, emitter),
                                           name=f"resistamet-{run_id}", daemon=True)
            except Exception:
                held.release()
                raise
            previous = (self._run_id, self._mode, self._run, self._thread)
            self._state = 'running'
            self._run_id, self._mode = run_id, mode
            self._run_address = address
            self._control, self._run, self._thread = control, run, thread
            self._run_ended_seen = False

        try:
            thread.start()
        except Exception:
            # No thread means no _execute to put any of this back: without
            # it the session stayed 'running' and the instrument stayed held.
            held.release()
            with self._lock:
                self._state = 'idle'
                self._control = None
                self._run_id, self._mode, self._run, self._thread = previous
            raise
        return run_id

    def stop(self) -> None:
        """Ask the run to wind down. Safe to call when nothing is running."""
        control = self._control
        if control is None:
            return
        with self._lock:
            if self._state in ('running',):
                self._state = 'stopping'
        self._run.stop_measurement()

    def abort(self) -> None:
        """Stop now, including out of a prompt the run is parked on.

        stop() asks the loop to wind down; abort() also releases a run waiting
        on an operator decision, which stop already does by design — the
        difference is the reason reported, so an operator-driven stop and a
        client giving up are distinguishable afterwards.
        """
        control = self._control
        if control is None:
            return
        with self._lock:
            if self._state == 'running':
                self._state = 'stopping'
        control.finish('aborted')

    def pause(self) -> None:
        run = self._require_run()
        if hasattr(run, 'pause_measurement'):
            run.pause_measurement()

    def resume(self) -> None:
        run = self._require_run()
        if hasattr(run, 'resume_measurement'):
            run.resume_measurement()

    def mark_event(self, label: str = 'MARK') -> None:
        control = self._require_control()
        control.mark_event(label)

    def answer_prompt(self, prompt_id: str, choice: str,
                       fields: Optional[Dict[str, Any]] = None) -> bool:
        """Answer the pending prompt. False when the answer was not taken.

        That is a stale id, a second answer, or a choice the prompt did not
        offer. The last is logged here because False is all the caller sees,
        and the prompt is left pending for a real answer.
        """
        control = self._require_control()
        try:
            return control.answer_prompt(prompt_id, choice, fields)
        except InvalidPromptChoice as exc:
            logger.warning("prompt answer refused: %s", exc)
            return False

    def identify(self, address: str, visa_library: str = '',
                 gpib_interface: str = '') -> Dict[str, Any]:
        """Ask what is at an address. Refused while a run owns the bus."""
        from ..instrument import Keithley2400

        with self._lock:
            if self._state != 'idle':
                raise SessionBusy(f"session is {self._state}")
            self._state = 'identifying'
        try:
            with hold_instrument(address):
                instrument = Keithley2400(address, visa_library=visa_library,
                                          gpib_interface=gpib_interface).connect()
                try:
                    idn = instrument.query("*IDN?").strip()
                    if address == self._output_unknown_at:
                        # Identify sends no *RST, so the output the last run
                        # left in doubt is turned off here, first thing after
                        # *IDN?, and only once the instrument confirms it.
                        instrument.write(":OUTP OFF")
                        if int(float(instrument.query(":OUTP?"))) == 0:
                            self._output_unknown_at = None
                            self._say('output_off_recovered',
                                      "Output turned OFF after the previous run lost its link.")
                        else:
                            self._say('output_unverified',
                                      "Instrument output may still be ON — check the front panel.",
                                      level='warning')
                    spec = instrument.detect_model()
                finally:
                    instrument.close()
            found = {
                'address': address,
                'idn': idn,
                'model': spec.model if spec else None,
                'max_source_v': spec.max_source_v if spec else None,
                'max_source_i': spec.max_source_i if spec else None,
                'max_power_w': spec.max_power_w if spec else None,
            }
            self._instrument = InstrumentInfo(**found)
            return found
        finally:
            with self._lock:
                self._state = 'idle'

    def close(self, timeout: float = 35.0) -> None:
        """Stop any run and join its thread. Idempotent."""
        self.stop()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # --- internals --------------------------------------------------------

    def _execute(self, run, held: HeldInstrument, emitter: EventEmitter) -> None:
        try:
            run.execute()
        except Exception:
            # The procedures report their own failures; this is the last
            # resort so a crash cannot leave the session wedged in 'running'.
            logger.exception("run thread died")
        finally:
            # A run cleans up after itself, and cleaning up twice does
            # nothing. After a run that died with its instrument open this is
            # what turns the output off and closes the session, before the
            # lock below lets anyone else onto the bus.
            try:
                run._cleanup()
            except Exception:
                logger.exception("cleanup after a dead run failed")
            # The run releases the instrument in its own cleanup. Releasing
            # again is a no-op; after a run that died before its cleanup it
            # is what stops every later start being refused as "in use by
            # another process" by a lock this process still holds.
            held.release()
            if not self._run_ended_seen:
                self._end_for(run, emitter)
            with self._lock:
                self._state = 'idle'
                self._control = None

    def _end_for(self, run, emitter: EventEmitter) -> None:
        """Send the run_ended a run died without sending."""
        try:
            emitter.error('worker_error', 'run',
                          "The run ended unexpectedly; see the application log.")
            emitter.emit('run_ended', {
                'reason': 'worker_error', 'ok': False, 'samples': 0,
                'duration_s': 0.0, 'path': getattr(run, 'filename', '') or None,
                # Set by the cleanup _execute ran for the dead run.
                'output_verified': getattr(run, '_output_verified', True),
            })
        except Exception:
            logger.exception("could not report the end of a run that died")

    def _say(self, code: str, message: str, level: str = 'info') -> None:
        """A log line from the session itself, outside any run (run_id None)."""
        EventEmitter(self._sink, run_id=None, clock=self._clock).log(code, message, level=level)

    def _record(self, event) -> None:
        self._last_event_seq = event.seq
        if event.type == 'run_ended':
            self._run_ended_seen = True
            if event.payload.get('output_verified') is False:
                # Remembered until a connection back to this address turns
                # the output off: the run's *RST, or identify's :OUTP OFF.
                self._output_unknown_at = self._run_address
        if event.type == 'log' and event.payload.get('code') == 'output_off_recovered':
            self._output_unknown_at = None
        if event.type == 'instrument_connected':
            # Kept for status(): the event itself is gone for a client that
            # connects, or reloads, after it was sent.
            self._instrument = InstrumentInfo(**event.payload)
        self._sink(event)

    def _require_run(self):
        run = self._run
        if run is None or self.state == 'idle':
            raise SessionBusy("no run in progress")
        return run

    def _require_control(self) -> RunControl:
        control = self._control
        if control is None:
            raise SessionBusy("no run in progress")
        return control

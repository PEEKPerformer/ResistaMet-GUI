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
from ..schema.spots import SpotRequest, check_spot_mode
from .continuous_run import ContinuousRun
from .control import RunControl
from .emitter import EventEmitter
from .instrument_lock import HeldInstrument, InstrumentBusy, hold_instrument
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
        self._mode: Optional[str] = None

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
        return {
            'state': self.state,
            'run_id': run_id,
            'mode': mode,
            'path': getattr(run, 'filename', '') or None,
            'last_seq': self._last_event_seq,
            'pending_prompt': None if prompt is None else {
                'prompt_id': prompt.prompt_id,
                'kind': prompt.kind,
                'options': list(prompt.options),
                'requires_human': prompt.requires_human,
                'detail': dict(prompt.detail),
            },
        }

    # --- commands ---------------------------------------------------------

    def start(self, profile: Dict[str, Any], mode: str, sample_name: str, username: str,
              overrides: Optional[Dict[str, Any]] = None,
              prompt_timeout_s: float = 900.0,
              spot: Optional[Any] = None) -> str:
        """Resolve settings, then run them. Returns the run id immediately.

        ``spot`` (a ``SpotRequest`` or its dict) says which placement of the
        four-point probe this run is. It rides in the run settings as
        ``settings['spot']``, beside the sections the profile provides, so the
        run procedure reads it the same way whoever started the run.

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

        with self._lock:
            if self._state != 'idle':
                raise SessionBusy(f"session is {self._state}")
            # The instrument is taken here, before "started" is answered, so a
            # bus held by another process is a refusal the caller sees, not a
            # run that fails a moment later. The run releases it in cleanup.
            address = resolved.settings.get('measurement', {}).get('gpib_address', '')
            held = HeldInstrument(address)  # raises InstrumentBusy
            try:
                self._run_count += 1
                run_id = f"run-{self._run_count}"
                control = RunControl()
                emitter = EventEmitter(self._record, run_id=run_id, clock=self._clock)
                if mode == VDP_MODE:
                    run = VdpRun(sample_name, username, resolved.settings, control, emitter,
                                  safety_ack='prompt', prompt_timeout_s=prompt_timeout_s,
                                  instrument_lock=held)
                else:
                    run = ContinuousRun(mode, sample_name, username, resolved.settings,
                                         control, emitter, safety_ack='prompt',
                                         prompt_timeout_s=prompt_timeout_s,
                                         instrument_lock=held)
                thread = threading.Thread(target=self._execute, args=(run,),
                                           name=f"resistamet-{run_id}", daemon=True)
            except Exception:
                held.release()
                raise
            self._state = 'running'
            self._run_id, self._mode = run_id, mode
            self._control, self._run, self._thread = control, run, thread

        thread.start()
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
        """Answer the pending prompt. False when the id is stale."""
        control = self._require_control()
        return control.answer_prompt(prompt_id, choice, fields)

    def identify(self, address: str, visa_library: str = '') -> Dict[str, Any]:
        """Ask what is at an address. Refused while a run owns the bus."""
        from ..instrument import Keithley2400

        with self._lock:
            if self._state != 'idle':
                raise SessionBusy(f"session is {self._state}")
            self._state = 'identifying'
        try:
            with hold_instrument(address):
                instrument = Keithley2400(address, visa_library=visa_library).connect()
                try:
                    idn = instrument.query("*IDN?").strip()
                    spec = instrument.detect_model()
                finally:
                    instrument.close()
            return {
                'address': address,
                'idn': idn,
                'model': spec.model if spec else None,
                'max_source_v': spec.max_source_v if spec else None,
                'max_source_i': spec.max_source_i if spec else None,
                'max_power_w': spec.max_power_w if spec else None,
            }
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

    def _execute(self, run) -> None:
        try:
            run.execute()
        except Exception:
            # The procedures report their own failures; this is the last
            # resort so a crash cannot leave the session wedged in 'running'.
            logger.exception("run thread died")
        finally:
            with self._lock:
                self._state = 'idle'
                self._control = None

    def _record(self, event) -> None:
        self._last_event_seq = event.seq
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

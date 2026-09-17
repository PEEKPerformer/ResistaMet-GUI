import logging

from PySide6.QtCore import QThread, Signal

from .session.continuous_run import ContinuousRun
from .session.control import RunControl
from .session.emitter import EventEmitter
from .session.vdp_run import VdpRun

logger = logging.getLogger(__name__)

# Keithley 2400 series STATUS word bit masks (24-bit)
# Bit 3: Compliance — source is in real compliance
_STAT_BIT_COMPLIANCE = 1 << 3


class _QtSink:
    """Event -> Qt Signal. The transition's whole Qt surface for run events.

    An explicit if/elif chain on purpose: a reviewer can see every event that
    reaches the GUI and what it turns into. Events without a Signal — the ones
    the API needs and the Qt UI never asked for — are dropped here.
    """

    def __init__(self, worker):
        self._worker = worker

    def __call__(self, event):
        kind = event.type
        payload = event.payload
        if kind == 'log':
            # Message text is unchanged from what the GUI has always logged;
            # level and code are for clients that want to act on it.
            self._worker.status_update.emit(payload['message'])
        elif kind == 'error':
            self._worker.error_occurred.emit(payload['message'])
        elif kind == 'sample':
            self._worker.data_point.emit(
                payload['t_unix'], payload['values'],
                payload['compliance'], payload['event_marker'],
            )
        elif kind == 'compliance':
            self._worker.compliance_hit.emit(payload['kind'])
        elif kind == 'overpower_trip':
            self._worker.overpower_hit.emit(payload['measured_w'], payload['stop_w'])
        elif kind == 'sweep_segment':
            self._worker.sweep_complete.emit(
                payload['voltages'], payload['currents'], payload['compliance'])
        elif kind == 'acquisition_finished':
            self._worker.measurement_complete.emit(payload['mode'])
        elif kind == 'instrument_connected':
            self._worker.instrument_identified.emit(payload['model'])
        # line_frequency has no Signal: the GUI reads it from the status log.


class _QtOutputs:
    """Call-shaped facade over a worker's Qt Signals.

    The run code reports through plain method calls, so it stops naming Qt at
    every site. Calls that have become events go through :class:`_QtSink`
    instead; what is left here is the not-yet-converted remainder.
    """

    def __init__(self, worker):
        self._worker = worker

    def geometry_ready(self, index, geometry):
        self._worker.geometry_ready.emit(index, geometry)

    def geometry_complete(self, index, result):
        self._worker.geometry_complete.emit(index, result)

    def vdp_complete(self, result):
        self._worker.vdp_complete.emit(result)


class MeasurementWorker(QThread):
    """Worker thread for running measurements in different modes."""
    data_point = Signal(float, dict, str, str)  # timestamp, data dict, compliance, event
    status_update = Signal(str)
    measurement_complete = Signal(str)
    error_occurred = Signal(str)
    compliance_hit = Signal(str)  # 'Voltage' or 'Current'
    overpower_hit = Signal(float, float)  # measured_power_w, hard_stop_w (4PP only)
    sweep_complete = Signal(list, list, list)  # voltages, currents, compliance_list
    # Short model name ("2400", "2410", ...) once IDN has been parsed. The
    # GUI caches this for accuracy.py uncertainty lookups after the worker
    # thread tears down, since per-spot stats are computed post-measurement.
    instrument_identified = Signal(str)

    def __init__(self, mode, sample_name, username, settings, parent=None):
        super().__init__(parent)
        self._out = _QtOutputs(self)
        self._control = RunControl()
        self._run = ContinuousRun(mode, sample_name, username, settings,
                                   self._control, self._out, EventEmitter(_QtSink(self)))

    # --- the run's identity and state, as the GUI has always read them

    @property
    def mode(self):
        return self._run.mode

    @property
    def settings(self):
        return self._run.settings

    @property
    def filename(self) -> str:
        return self._run.filename

    @property
    def keithley(self):
        return self._run.keithley

    @property
    def exporter(self):
        return self._run.exporter

    @property
    def running(self) -> bool:
        return self._control.running

    @running.setter
    def running(self, value: bool) -> None:
        self._control.running = value

    @property
    def paused(self) -> bool:
        return self._control.paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self._control.paused = value

    @property
    def event_marker(self) -> str:
        return self._control.event_marker

    def get_and_clear_event_marker(self) -> str:
        return self._control.get_and_clear_event_marker()

    def run(self):
        """QThread entry point."""
        self._run.execute()

    def mark_event(self, name: str = "MARK") -> None:
        self._control.mark_event(name)

    def pause_measurement(self) -> None:
        self._run.pause_measurement()

    def resume_measurement(self) -> None:
        self._run.resume_measurement()

    def stop_measurement(self) -> None:
        self._run.stop_measurement()


class VdpMeasurementWorker(QThread):
    """Van der Pauw measurement worker per ASTM F76-08 Method A.

    State machine over F76's 4 physical cabling configurations. For each
    geometry the worker emits ``geometry_ready``, waits for the UI to call
    ``proceed()`` (after the user has reconnected leads), then takes +I
    and -I voltage readings (current reversal cancels thermal offsets per
    F76 sec. 11.1) and emits ``geometry_complete``. After 4 geometries it
    computes the vdP result and emits ``vdp_complete``.

    Signals:
        geometry_ready(int, dict): index 0..3, instruction dict
            (name, source_high, source_low, sense_high, sense_low,
            label_pos, label_neg, group).
        geometry_complete(int, dict): readings dict
            (name, label_pos, v_pos, label_neg, v_neg, current_a, group).
        vdp_complete(dict): final VdpResult fields + raw voltages.
        status_update(str), error_occurred(str), compliance_hit(str).
    """

    geometry_ready = Signal(int, dict)
    geometry_complete = Signal(int, dict)
    vdp_complete = Signal(dict)
    status_update = Signal(str)
    error_occurred = Signal(str)
    compliance_hit = Signal(str)
    instrument_identified = Signal(str)  # see MeasurementWorker

    MODE = 'vdp'

    def __init__(self, sample_name, username, settings, parent=None):
        super().__init__(parent)
        self._out = _QtOutputs(self)
        self._control = RunControl()
        self._run = VdpRun(sample_name, username, settings, self._control, self._out,
                            EventEmitter(_QtSink(self)))

    # --- the run's identity and state, as the GUI has always read them

    @property
    def sample_name(self):
        return self._run.sample_name

    @property
    def settings(self):
        return self._run.settings

    @property
    def filename(self) -> str:
        return self._run.filename

    @property
    def keithley(self):
        return self._run.keithley

    @property
    def exporter(self):
        return self._run.exporter

    @property
    def running(self) -> bool:
        return self._control.running

    @running.setter
    def running(self, value: bool) -> None:
        self._control.running = value

    def run(self) -> None:
        """QThread entry point."""
        self._run.execute()

    def proceed(self) -> None:
        """UI slot: user has reconnected leads; take this geometry's reading."""
        self._run.proceed()

    def stop_measurement(self) -> None:
        self._run.stop_measurement()

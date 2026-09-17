import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict

from PySide6.QtCore import QThread, Signal

from .data_export import build_metadata, get_column_config, make_exporter
from .instrument import Keithley2400, humanize_connection_error
from .session.continuous_run import ContinuousRun
from .session.control import RunControl
from .session.run_files import create_base_path
from .system_utils import SleepInhibitor

logger = logging.getLogger(__name__)

# Keithley 2400 series STATUS word bit masks (24-bit)
# Bit 3: Compliance — source is in real compliance
_STAT_BIT_COMPLIANCE = 1 << 3


class _QtOutputs:
    """Call-shaped facade over a worker's Qt Signals.

    The run code reports through plain method calls, so it stops naming Qt at
    every site. Each method maps one-to-one onto the Signal of the same name;
    a worker only ever calls the ones it declares. When the run procedures move
    out of this file, this class stays behind as the adapter and gains an
    event-to-Signal table instead.
    """

    def __init__(self, worker):
        self._worker = worker

    def data_point(self, timestamp, data, compliance_status, event_marker):
        self._worker.data_point.emit(timestamp, data, compliance_status, event_marker)

    def status_update(self, message):
        self._worker.status_update.emit(message)

    def measurement_complete(self, mode):
        self._worker.measurement_complete.emit(mode)

    def error_occurred(self, message):
        self._worker.error_occurred.emit(message)

    def compliance_hit(self, kind):
        self._worker.compliance_hit.emit(kind)

    def overpower_hit(self, measured_w, stop_w):
        self._worker.overpower_hit.emit(measured_w, stop_w)

    def sweep_complete(self, voltages, currents, compliance):
        self._worker.sweep_complete.emit(voltages, currents, compliance)

    def instrument_identified(self, model_name):
        self._worker.instrument_identified.emit(model_name)

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
                                   self._control, self._out)

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


class _VdpAborted(Exception):
    """Internal: worker was stopped via stop_measurement()."""


class VdpRun:
    """One van der Pauw run: ASTM F76 Method A, four manual geometries.

    Inherently human-in-loop — the operator rewires the leads between
    geometries — so the procedure waits on the control's proceed gate rather
    than owning any UI. Like ContinuousRun, it reports through an outputs
    facade and contains no Qt.
    """

    MODE = 'vdp'

    def __init__(self, sample_name, username, settings, control, out):
        self.sample_name = sample_name
        self.username = username
        self.settings = settings
        self._out = out
        self._control = control
        self._voltages: Dict[str, float] = {}
        self.keithley = None
        self.exporter = None
        self._instrument_idn = ""
        self._sleep_inhibitor = SleepInhibitor()
        self._start_time = 0.0
        self._i_mag = 0.0
        self.filename = ""

    @property
    def running(self) -> bool:
        return self._control.running

    @running.setter
    def running(self, value: bool) -> None:
        self._control.running = value

    def proceed(self) -> None:
        """UI slot: user has reconnected leads; take this geometry's reading."""
        self._control.proceed_event.set()

    def stop_measurement(self) -> None:
        self._out.status_update("Stopping vdP measurement...")
        self.running = False
        # Unblock any wait_for_user pause.
        self._control.proceed_event.set()

    def _emit_compress_status(self, orig_path: Path, gz_path: Path,
                              orig_mb: float, gz_mb: float) -> None:
        """Status callback fired by CsvExporter after gzip finalize."""
        self._out.status_update(
            f"Compressed {orig_path.name} -> {gz_path.name} "
            f"({orig_mb:.1f} MB -> {gz_mb:.1f} MB)"
        )

    def _emit_large_file_status(self, path: Path, size_mb: float) -> None:
        """Status callback fired by CsvExporter when an uncompressed run is large."""
        self._out.status_update(
            f"Run wrote {size_mb:.1f} MB to {path.name}. "
            f"Compression is off — enable in Settings -> Output to gzip future runs."
        )

    def execute(self) -> None:
        self.running = True
        try:
            self._connect_and_configure()
            self._run_geometries()
            self._compute_and_emit_result()
        except _VdpAborted:
            self._out.status_update("vdP measurement aborted by user")
        except Exception as e:
            logger.exception("vdP measurement failed")
            self._out.error_occurred(f"vdP error: {e}")
        finally:
            self.running = False
            self._cleanup()

    def _connect_and_configure(self) -> None:
        # Lazy imports to avoid a Qt-load-time cost when vdP isn't used.
        from .calculations_vdp import f76_geometries  # noqa: F401  (touched in _run_geometries)

        measurement = self.settings['measurement']
        gpib = measurement['gpib_address']
        self._out.status_update(f"Connecting to instrument at {gpib}...")
        try:
            self.keithley = Keithley2400(gpib).connect()
        except Exception as e:
            # Re-raise so the run() catch still fires, but with a message
            # the user can actually act on.
            raise RuntimeError(humanize_connection_error(e, gpib)) from e
        self._instrument_idn = self.keithley.query("*IDN?").strip()
        self._out.status_update(f"Connected to: {self._instrument_idn}")
        spec = self.keithley.detect_model()
        self._model_name = spec.model if spec else "2400"
        try:
            self._out.instrument_identified(self._model_name)
        except Exception:
            pass

        i_mag = abs(float(measurement['vdp_current']))
        v_comp = float(measurement['vdp_voltage_compliance'])
        nplc = float(measurement.get('nplc', 1.0))
        auto_range = bool(measurement.get('vdp_voltage_range_auto', True))
        if i_mag <= 0:
            raise ValueError("vdp_current must be > 0 A")
        if v_comp <= 0:
            raise ValueError("vdp_voltage_compliance must be > 0 V")

        self.keithley.write("*RST"); time.sleep(0.5)
        self.keithley.write("*CLS")
        azer = str(measurement.get('auto_zero', 'on')).upper()
        if azer == 'ONCE':
            self.keithley.write(":SYST:AZER:STAT ON")
            self.keithley.write(":SYST:AZER:STAT ONCE")
        else:
            self.keithley.write(f":SYST:AZER:STAT {azer}")
        self.keithley.write(":SENS:FUNC:CONC OFF")
        self.keithley.write(":OUTP:SMOD HIMP")

        # F76 sec. 7.3.2 requires the voltmeter to draw <0.1 % of the
        # source current. Routing V through the Force terminals (RSEN OFF)
        # would re-add contact and lead resistance to every reading --
        # this is the same lesson as the 4PP RSEN bug fixed in v1.7.0.
        self.keithley.write(":SYST:RSEN ON")
        self.keithley.write(":SENS:FUNC 'VOLT:DC'")
        self.keithley.write(":SOUR:FUNC CURR")
        self.keithley.write(f":SOUR:CURR:RANG {i_mag}")
        self.keithley.write(f":SOUR:CURR {i_mag}")
        self.keithley.write(f":SENS:VOLT:PROT {v_comp}")
        if auto_range:
            self.keithley.write(":SENS:VOLT:RANG:AUTO ON")
        else:
            self.keithley.write(":SENS:VOLT:RANG:AUTO OFF")
            self.keithley.write(f":SENS:VOLT:RANG {v_comp}")
        self.keithley.write(f":SENS:VOLT:NPLC {nplc}")
        self.keithley.write(":FORM:ELEM VOLT,CURR,STAT")
        self.keithley.write(":TRIG:DEL 0")
        self.keithley.write(":SOUR:DEL:AUTO ON")

        if measurement.get('filter_enabled', False):
            ftype = str(measurement.get('filter_type', 'repeat')).upper()[:3]
            fcount = int(measurement.get('filter_count', 10))
            self.keithley.write(f":SENS:AVER:TCON {ftype}")
            self.keithley.write(f":SENS:AVER:COUN {fcount}")
            self.keithley.write(":SENS:AVER ON")

        self._i_mag = i_mag

        # Output data file via the configured exporter.
        base_path = create_base_path(
            self.settings['file']['data_directory'], self.username,
            self.sample_name, self.MODE, f"{i_mag*1000:.2f}mA",
        )

        columns, units = get_column_config(self.MODE, measurement)
        export_metadata = build_metadata(
            user=self.username,
            sample_name=self.sample_name,
            mode=self.MODE,
            settings=self.settings,
            instrument_idn=self._instrument_idn,
            start_time=datetime.fromtimestamp(time.time()),
        )
        self.exporter = make_exporter(
            base_path=base_path,
            metadata=export_metadata,
            columns=columns,
            units=units,
            output_settings=self.settings.get('output'),
            on_compress=self._emit_compress_status,
            on_large_file=self._emit_large_file_status,
        )
        primary_paths = self.exporter.output_paths
        self.filename = str(primary_paths[0]) if primary_paths else str(base_path)
        names = ", ".join(p.name for p in primary_paths)
        self._out.status_update(f"Data file: {names}")

        self._sleep_inhibitor.inhibit(f"ResistaMet: vdP on {self.sample_name}")
        self._start_time = time.time()

    def _run_geometries(self) -> None:
        from .calculations_vdp import f76_geometries

        measurement = self.settings['measurement']
        settling = float(measurement.get('vdp_settling_s', 0.2))
        n_avg = max(1, int(measurement.get('vdp_readings_per_polarity', 1)))

        for idx, geom in enumerate(f76_geometries()):
            if not self.running:
                raise _VdpAborted()

            self._control.proceed_event.clear()
            self._out.geometry_ready(idx, {
                'name': geom.name,
                'source_high': geom.source_high,
                'source_low': geom.source_low,
                'sense_high': geom.sense_high,
                'sense_low': geom.sense_low,
                'label_pos': geom.label_pos,
                'label_neg': geom.label_neg,
                'group': geom.group,
            })
            self._out.status_update(
                f"{geom.name}: connect Force HI->C{geom.source_high}, "
                f"Force LO->C{geom.source_low}, "
                f"Sense HI->C{geom.sense_high}, "
                f"Sense LO->C{geom.sense_low}; press Measure."
            )
            self._control.proceed_event.wait()
            if not self.running:
                raise _VdpAborted()

            self.keithley.write(":OUTP ON")
            self.keithley.write(f":SOUR:CURR {self._i_mag}")
            time.sleep(settling)
            v_pos, stat_pos = self._read_averaged(n_avg)

            self.keithley.write(f":SOUR:CURR {-self._i_mag}")
            time.sleep(settling)
            v_neg, stat_neg = self._read_averaged(n_avg)

            # Return polarity to +I and disable output so the user can
            # safely reconnect leads for the next geometry.
            self.keithley.write(f":SOUR:CURR {self._i_mag}")
            self.keithley.write(":OUTP OFF")

            if (stat_pos | stat_neg) & _STAT_BIT_COMPLIANCE:
                self._out.compliance_hit("Voltage")

            self._voltages[geom.label_pos] = v_pos
            self._voltages[geom.label_neg] = v_neg

            elapsed = time.time() - self._start_time
            row = [
                elapsed, geom.name, geom.group,
                geom.source_high, geom.source_low,
                geom.sense_high, geom.sense_low,
                geom.label_pos, v_pos,
                geom.label_neg, v_neg,
                self._i_mag,
            ]
            try:
                self.exporter.write_row(row)
            except Exception:
                logger.warning("vdP: failed to write export row", exc_info=True)

            self._out.geometry_complete(idx, {
                'name': geom.name,
                'label_pos': geom.label_pos, 'v_pos': v_pos,
                'label_neg': geom.label_neg, 'v_neg': v_neg,
                'current_a': self._i_mag,
                'group': geom.group,
            })

    def _compute_vdp_combined_uncertainty(self, result):
        """Thin wrapper around calculations_vdp.vdp_combined_uncertainty.

        Kept on the worker so finalize() metadata can carry the same
        u_rs / u_rho the GUI shows in the result panel — both paths call
        the same pure helper, so they cannot drift out of sync.
        """
        from .calculations_vdp import vdp_combined_uncertainty
        nplc = float(self.settings['measurement'].get('nplc', 1.0))
        u = vdp_combined_uncertainty(
            voltages=dict(self._voltages),
            current=float(self._i_mag),
            sheet_resistance=float(result.sheet_resistance),
            rho_avg=float(result.rho_avg),
            model=self._model_name,
            nplc=nplc,
        )
        return u.u_rs, u.u_rho

    def _read_averaged(self, n: int):
        """Issue N :READ? queries and return (mean V, OR of STAT bits)."""
        v_sum = 0.0
        stat_or = 0
        for _ in range(n):
            raw = self.keithley.query(":READ?").strip()
            parts = [p.strip() for p in raw.split(',')]
            v_sum += float(parts[0])
            stat_or |= int(float(parts[-1]))
        return v_sum / n, stat_or

    def _compute_and_emit_result(self) -> None:
        from .calculations_vdp import calculate_van_der_pauw

        thickness = float(self.settings['measurement']['vdp_thickness_cm'])
        result = calculate_van_der_pauw(self._voltages, self._i_mag, thickness)

        # Combined uncertainty on Rs and ρ. Mirrors the GUI computation in
        # _vdp_on_complete (kept in sync intentionally so the CSV finalize
        # metadata carries the same numbers the result panel shows).
        u_rs, u_rho = self._compute_vdp_combined_uncertainty(result)

        result_dict = {
            'rho_a': result.rho_a,
            'rho_b': result.rho_b,
            'rho_avg': result.rho_avg,
            'sheet_resistance': result.sheet_resistance,
            'q_a': result.q_a,
            'q_b': result.q_b,
            'f_a': result.f_a,
            'f_b': result.f_b,
            'homogeneous': result.homogeneous,
            'asymmetry_pct': result.asymmetry_pct,
            'voltages': dict(self._voltages),
            'current_a': self._i_mag,
            'thickness_cm': thickness,
            'sheet_resistance_uncertainty': u_rs,
            'rho_avg_uncertainty': u_rho,
        }
        self._out.vdp_complete(result_dict)
        self._out.status_update(
            f"vdP done: Rs={result.sheet_resistance:.4g} Ohm/sq, "
            f"rho={result.rho_avg:.4g} Ohm.cm, "
            f"asym={result.asymmetry_pct:.2f}% "
            f"({'homogeneous' if result.homogeneous else 'NON-homogeneous'})"
        )
        try:
            self.exporter.finalize({'vdp_result': result_dict})
        except Exception:
            logger.warning("vdP: finalize with result failed", exc_info=True)

    def _cleanup(self) -> None:
        self._sleep_inhibitor.uninhibit()
        if self.keithley:
            try:
                self.keithley.write(":OUTP OFF")
                self.keithley.close()
                self._out.status_update("Instrument disconnected.")
            except Exception as e:
                self._out.status_update(f"Warning: cleanup error: {e}")
            finally:
                self.keithley = None
        if self.exporter:
            try:
                # finalize() is idempotent on already-finalized exporters.
                self.exporter.finalize()
            except Exception:
                pass
            self.exporter = None


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
        self._run = VdpRun(sample_name, username, settings, self._control, self._out)

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

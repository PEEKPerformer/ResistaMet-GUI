"""One van der Pauw run, ASTM F76-08 Method A.

Moved out of ``workers.py`` unchanged. Four physical cabling configurations,
rewired by hand between geometries, so the procedure waits on the control's
proceed gate; current reversal at each geometry is automated, giving the eight
voltage readings F76 averages. Reports through an outputs facade, no Qt.
"""
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict

from ..data_export import build_metadata, get_column_config, make_exporter
from ..instrument import Keithley2400, humanize_connection_error
from ..system_utils import SleepInhibitor
from .run_files import create_base_path

logger = logging.getLogger(__name__)

# Keithley 2400 series STATUS word bit masks (24-bit)
# Bit 3: Compliance — source is in real compliance
_STAT_BIT_COMPLIANCE = 1 << 3


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

    def __init__(self, sample_name, username, settings, control, events):
        self.sample_name = sample_name
        self.username = username
        self.settings = settings
        self._events = events
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
        prompt = self._control.pending_prompt
        if prompt is not None:
            self._control.answer_prompt(prompt.prompt_id, 'proceed')

    def stop_measurement(self) -> None:
        self._events.emit('stopping', {'reason': 'user_stop'})
        self._events.log('stopping', "Stopping vdP measurement...")
        # finish() also wakes a geometry wait.
        self._control.finish('user_stop')

    def _emit_compress_status(self, orig_path: Path, gz_path: Path,
                              orig_mb: float, gz_mb: float) -> None:
        """Status callback fired by CsvExporter after gzip finalize."""
        self._events.log('compress', 
            f"Compressed {orig_path.name} -> {gz_path.name} "
            f"({orig_mb:.1f} MB -> {gz_mb:.1f} MB)"
        )

    def _emit_large_file_status(self, path: Path, size_mb: float) -> None:
        """Status callback fired by CsvExporter when an uncompressed run is large."""
        self._events.warn('large_file', 
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
            self._control.finish('user_stop')
            self._events.log('aborted', "vdP measurement aborted by user")
        except Exception as e:
            self._control.finish('worker_error')
            logger.exception("vdP measurement failed")
            self._events.error('worker_error', 'run', f"vdP error: {e}")
        finally:
            self.running = False
            samples = self.exporter.row_count if self.exporter else 0
            self._cleanup()
            reason = self._control.finish_reason or 'completed'
            self._events.emit('run_ended', {
                'reason': reason,
                'ok': reason in ('completed', 'user_stop'),
                'samples': samples,
                'duration_s': time.time() - self._start_time if self._start_time else 0.0,
                'path': self.filename or None,
            })

    def _connect_and_configure(self) -> None:
        # Lazy imports to avoid a Qt-load-time cost when vdP isn't used.
        from ..calculations_vdp import f76_geometries  # noqa: F401  (touched in _run_geometries)

        measurement = self.settings['measurement']
        gpib = measurement['gpib_address']
        self._events.log('connecting', f"Connecting to instrument at {gpib}...")
        try:
            self.keithley = Keithley2400(gpib).connect()
        except Exception as e:
            # Re-raise so the run() catch still fires, but with a message
            # the user can actually act on.
            raise RuntimeError(humanize_connection_error(e, gpib)) from e
        self._instrument_idn = self.keithley.query("*IDN?").strip()
        self._events.log('connected', f"Connected to: {self._instrument_idn}")
        spec = self.keithley.detect_model()
        self._model_name = spec.model if spec else "2400"
        try:
            self._events.emit('instrument_connected', {
                'address': gpib,
                'idn': self._instrument_idn,
                'model': self._model_name,
                'max_source_v': spec.max_source_v if spec else None,
                'max_source_i': spec.max_source_i if spec else None,
                'max_power_w': spec.max_power_w if spec else None,
            })
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
        self._events.log('file_opened', f"Data file: {names}")

        self._sleep_inhibitor.inhibit(f"ResistaMet: vdP on {self.sample_name}")
        self._start_time = time.time()

    def _run_geometries(self) -> None:
        from ..calculations_vdp import f76_geometries

        measurement = self.settings['measurement']
        settling = float(measurement.get('vdp_settling_s', 0.2))
        n_avg = max(1, int(measurement.get('vdp_readings_per_polarity', 1)))

        for idx, geom in enumerate(f76_geometries()):
            if not self.running:
                raise _VdpAborted()

            prompt = self._control.raise_prompt('vdp_geometry', ['proceed', 'abort'], detail={
                'index': idx,
                'name': geom.name,
                'source_high': geom.source_high,
                'source_low': geom.source_low,
                'sense_high': geom.sense_high,
                'sense_low': geom.sense_low,
                'label_pos': geom.label_pos,
                'label_neg': geom.label_neg,
                'group': geom.group,
            })
            self._events.emit('prompt', {
                'prompt_id': prompt.prompt_id,
                'kind': prompt.kind,
                'options': prompt.options,
                'requires_human': prompt.requires_human,
                'detail': prompt.detail,
            })
            self._events.log('geometry_prompt', 
                f"{geom.name}: connect Force HI->C{geom.source_high}, "
                f"Force LO->C{geom.source_low}, "
                f"Sense HI->C{geom.sense_high}, "
                f"Sense LO->C{geom.sense_low}; press Measure."
            )
            choice = self._control.wait_for_prompt()
            self._events.emit('prompt_resolved', {
                'prompt_id': prompt.prompt_id, 'choice': choice})
            if not self.running or choice == 'abort':
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
                self._events.emit('compliance', {'kind': 'Voltage',
                                                  'stop_on_compliance': False})

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

            self._events.emit('vdp_geometry_complete', {
                'index': idx,
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
        from ..calculations_vdp import vdp_combined_uncertainty
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
        from ..calculations_vdp import calculate_van_der_pauw

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
        self._events.emit('vdp_result', result_dict)
        self._events.log('completed', 
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
                self._events.log('cleanup', "Instrument disconnected.")
            except Exception as e:
                self._events.warn('cleanup', f"Warning: cleanup error: {e}")
            finally:
                self.keithley = None
        if self.exporter:
            try:
                # finalize() is idempotent on already-finalized exporters.
                self.exporter.finalize()
            except Exception:
                pass
            self.exporter = None

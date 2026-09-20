"""One continuous-mode or sweep run, start to finalize.

Moved out of ``workers.py`` unchanged. The procedure owns the instrument
session, the exporter and the acquisition loop; it reports through an outputs
facade and reads stop/pause from a :class:`~resistamet_gui.session.control.RunControl`,
so the QThread adapter in ``workers.py`` and the headless session drive the same
code. No Qt here.
"""
import logging
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pyvisa

from ..constants import AUX_READY_TIMEOUT_S, MODE_DISPLAY_NAMES
from ..data_export import AUX_LOG_MODES, splice_before_tail
from ..formatting import format_power
from ..instrument import Keithley2400, humanize_connection_error
from ..sensors import aux_column_names, make_sensor, reading_to_columns
from ..system_utils import SleepInhibitor
from .control import RunStopped
from .instrument_lock import HeldInstrument, InstrumentBusy
from .configure import (
    configure_four_point, configure_resistance, configure_source_i,
    configure_source_v, configure_sweep,
)
from .run_files import create_base_path, open_exporter
from .spot_map import write_map_summary
from .spot_record import spot_record_from_settings
from .spot_stats import SpotSamples, spot_statistics
from .samples import (
    build_row, parse_four_point, parse_resistance, parse_source_i, parse_source_v,
)

logger = logging.getLogger(__name__)

# Keithley 2400 series STATUS word bit masks (24-bit)
# Bit 3: Compliance — source is in real compliance
_STAT_BIT_COMPLIANCE = 1 << 3


class ContinuousRun:
    """One continuous-mode or sweep run, start to finalize.

    Owns the instrument session, the exporter and the acquisition loop. It
    reports through an outputs facade and reads stop/pause from a RunControl,
    so the same procedure serves the QThread adapter below and the headless
    session. No Qt here.
    """

    def __init__(self, mode, sample_name, username, settings, control, events,
                  safety_ack='skip', prompt_timeout_s=None, instrument_lock=None):
        if mode not in ['resistance', 'source_v', 'source_i', 'four_point', 'sweep']:
            raise ValueError(f"Invalid measurement mode: {mode}")
        self.mode = mode
        #: The mode as log messages name it; self.mode stays the internal key.
        self._mode_name = MODE_DISPLAY_NAMES[mode]
        self.sample_name = sample_name
        self.username = username
        self.settings = settings
        self._events = events
        self._safety_ack = safety_ack
        #: None = wait forever (the GUI has an operator at the bench).
        self._prompt_timeout_s = prompt_timeout_s
        #: A HeldInstrument the caller already took (so its refusal was
        #: synchronous), or None to take it here. Released in _cleanup either way.
        self._instrument_lock = instrument_lock
        # Set by the configure step: the frozen per-mode state the loop reads.
        self._mode_state = None
        # Set by each delta read: the per-polarity values the row builder logs.
        self._last_delta = None
        #: The negative-V/I warning is given once per run, not per sample.
        self._fpp_negative_ratio_warned = False
        # Set before anything is opened: this run's spot resolved against the
        # sample outline, or None for a run that carries no spot.
        self._spot_record = None
        # The values behind the end-of-run statistics. Four-point only: no
        # other mode has a per-spot result.
        self._spot_samples = SpotSamples() if mode == 'four_point' else None
        self._spot_sample_warned = False  # debounce: say it once per run
        self._shut_down_started = False   # _shut_down runs once per run
        # Set when a spot's file is finalized; its map summary is written
        # once the instrument has been let go.
        self._pending_map_id = None

        # Start/stop/pause state and the marker queue, shared with whoever is
        # driving the run.
        self._control = control
        self._csv_error_count = 0  # Track consecutive CSV write failures
        self._max_csv_errors = 3   # Max consecutive errors before escalation

        self.keithley = None
        self.exporter = None
        self.start_time = 0
        self.filename = ""
        self._instrument_idn = ""

        # Optional auxiliary sensor (co-logging). Stays None on every path
        # that doesn't opt in, so the no-sensor path is unchanged.
        self._aux_sensor = None
        self._aux_channels = []
        self._aux_columns = []      # aux_<key>... + aux_fault (column order)
        self._aux_units = []
        self._aux_row_values = []
        self._aux_last_fault = None  # dedups per-point fault status messages

        # System sleep prevention
        self._sleep_inhibitor = SleepInhibitor()

        # Instrument health monitoring
        self._last_error_check = 0
        self._error_check_interval = 30.0  # Check instrument errors every 30 seconds

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

    def _open_aux_sensor(self, measurement_settings):
        """Open the auxiliary sensor, if this run co-logs one. False on failure."""
        # Optional auxiliary sensor (co-logging on any continuous mode,
        # anchored to the Keithley run). Opened here, before the exporter,
        # so its declared channels() drive the CSV column schema. A failure
        # to open aborts the run the same way an instrument-connect failure
        # does — but the message is prefixed so the GUI never routes it to
        # the SMU's GPIB-address remediation. wait_ready blocks (worker
        # thread, safe) until the reader thread has the channel description
        # and a first cached reading.
        if (self.mode in AUX_LOG_MODES
                and measurement_settings.get('aux_log_enabled')):
            driver = measurement_settings.get('aux_driver', 'arduino_thermocouple')
            aux_address = measurement_settings.get('aux_address', '')
            try:
                self._events.log('aux_connecting', 
                    f"Connecting to auxiliary sensor ({driver}) at {aux_address}..."
                )
                self._aux_sensor = make_sensor(driver, aux_address).open()
                if hasattr(self._aux_sensor, 'wait_ready'):
                    self._aux_sensor.wait_ready(AUX_READY_TIMEOUT_S)
                self._aux_channels = self._aux_sensor.channels()
                self._aux_columns = aux_column_names(self._aux_sensor)
                self._aux_units = [ch.unit for ch in self._aux_channels] + ['']
                self._events.emit('aux_connected', {
                    'driver': driver,
                    'address': aux_address,
                    'channels': [{'key': ch.key, 'label': ch.label, 'unit': ch.unit}
                                  for ch in self._aux_channels],
                })
                self._events.log('aux_ready', 
                    "Auxiliary sensor ready: "
                    + ", ".join(f"{ch.label} ({ch.unit})" for ch in self._aux_channels)
                )
            except Exception as e:
                self._events.error('aux_connect_failed', 'aux', 
                    "Auxiliary sensor: " + humanize_connection_error(e, aux_address)
                )
                self._control.finish('aux_connect_failed')
                return
        return True

    def _effective_settings(self):
        """What the instrument reported after configuration, for the file header.

        ``effective.voltage_compliance_V`` is the voltage limit in force for
        the run: resistance mode, manual range. In Auto range the same
        read-back is written as ``effective.voltage_compliance_V_at_configure``
        instead, because that is all it is. Auto-ohms changes the limit with
        the ohms range, so the number says what the instrument had before the
        output came on, not what any row was measured under.
        """
        state = self._mode_state
        limit = getattr(state, 'voltage_compliance_v', None)
        if limit is None or not math.isfinite(limit):
            return None
        if getattr(state, 'auto_range', False):
            return {'voltage_compliance_V_at_configure': limit}
        return {'voltage_compliance_V': limit}

    def _open_output_file(self, measurement_settings, source_value_str):
        """Create the exporter for this run. False on failure."""
        # File setup via the configured exporter (csv / hdf5 / csv+legacy_json).
        self.start_time = time.time()
        try:
            base_path = create_base_path(
                self.settings['file']['data_directory'], self.username,
                self.sample_name, self.mode, source_value_str,
            )
            self.exporter, self.filename = open_exporter(
                base_path=base_path,
                mode=self.mode,
                settings=self.settings,
                measurement_settings=measurement_settings,
                username=self.username,
                sample_name=self.sample_name,
                instrument_idn=self._instrument_idn,
                start_time=self.start_time,
                aux_columns=self._aux_columns,
                aux_units=self._aux_units,
                on_compress=self._emit_compress_status,
                on_large_file=self._emit_large_file_status,
                effective=self._effective_settings(),
                spot=self._spot_record.header() if self._spot_record else None,
            )
            # Hdf5Exporter does not expose them; the schema is still known
            # to the caller, so an empty list means "ask get_column_config".
            columns = getattr(self.exporter, 'columns', [])
            units = getattr(self.exporter, 'units', [])
            self._events.emit('file_opened', {
                'path': self.filename, 'columns': list(columns), 'units': list(units)})
            names = ", ".join(p.name for p in self.exporter.output_paths)
            self._events.log('file_opened', f"Data file: {names}")
        except Exception as e:
            self._events.error('file_create_failed', 'file', f"Error creating output files: {str(e)}")
            self._control.finish('file_create_failed')
            return
        return True

    def _run_sweep(self):
        """Run the instrument's own sweep engine, write the points, report them."""
        # Sweep mode: single atomic operation, then done
        if self.mode == 'sweep':
            if self._mode_state.up_down:
                planned = f"{self._mode_state.points} points each way"
            else:
                planned = f"{self._mode_state.points} points"
            self._events.log('sweep_started', f"Running I-V sweep ({planned})...")
            try:
                self.keithley.write(":OUTP ON")
                # Increase timeout for long sweeps
                if self.keithley.dev:
                    self.keithley.dev.timeout = max(10000, self._mode_state.points * 1000)
                response = self.keithley.query(":READ?").strip()
                self.keithley.write(":OUTP OFF")

                # Parse bulk response: every 3 values = (V, I, STAT)
                parts = [p.strip() for p in response.split(',') if p.strip()]
                voltages, currents, comp_list = [], [], []
                for i in range(0, len(parts), 3):
                    try:
                        v = float(parts[i])
                        c = float(parts[i + 1]) if i + 1 < len(parts) else float('nan')
                        stat = int(float(parts[i + 2])) if i + 2 < len(parts) else 0
                    except (ValueError, IndexError):
                        v, c, stat = float('nan'), float('nan'), 0
                    voltages.append(v)
                    currents.append(c)
                    comp_status = 'COMP' if (stat & _STAT_BIT_COMPLIANCE) else 'OK'
                    comp_list.append(comp_status)

                    # Write each point to export
                    row_data = [i // 3, v, c, comp_status]
                    try:
                        self.exporter.write_row(row_data)
                    except Exception:
                        pass

                # What the closing log line reports: every point in the file,
                # both legs of an up-then-down sweep. It used to give the
                # forward leg's count alone ("41 points" for 82 rows).
                points_summary = f"{len(voltages)} points acquired"

                # For up_down: run reverse sweep
                if self._mode_state.up_down:
                    self._events.log('sweep_reverse', "Running reverse sweep...")
                    # Swap start/stop for reverse
                    if self._mode_state.source == 'VOLT':
                        start_q = self.keithley.query(":SOUR:VOLT:START?").strip()
                        stop_q = self.keithley.query(":SOUR:VOLT:STOP?").strip()
                        self.keithley.write(f":SOUR:VOLT:START {stop_q}")
                        self.keithley.write(f":SOUR:VOLT:STOP {start_q}")
                    else:
                        start_q = self.keithley.query(":SOUR:CURR:START?").strip()
                        stop_q = self.keithley.query(":SOUR:CURR:STOP?").strip()
                        self.keithley.write(f":SOUR:CURR:START {stop_q}")
                        self.keithley.write(f":SOUR:CURR:STOP {start_q}")
                    self.keithley.write(":OUTP ON")
                    response2 = self.keithley.query(":READ?").strip()
                    self.keithley.write(":OUTP OFF")

                    parts2 = [p.strip() for p in response2.split(',') if p.strip()]
                    rev_v, rev_i, rev_comp = [], [], []
                    for i in range(0, len(parts2), 3):
                        try:
                            v = float(parts2[i])
                            c = float(parts2[i + 1]) if i + 1 < len(parts2) else float('nan')
                            stat = int(float(parts2[i + 2])) if i + 2 < len(parts2) else 0
                        except (ValueError, IndexError):
                            v, c, stat = float('nan'), float('nan'), 0
                        rev_v.append(v)
                        rev_i.append(c)
                        comp_status = 'COMP' if (stat & _STAT_BIT_COMPLIANCE) else 'OK'
                        rev_comp.append(comp_status)
                        row_data = [len(voltages) + i // 3, v, c, comp_status]
                        try:
                            self.exporter.write_row(row_data)
                        except Exception:
                            pass
                    points_summary = (f"{len(voltages) + len(rev_v)} points acquired "
                                      f"({len(voltages)} forward, {len(rev_v)} reverse)")
                    # Report both directions
                    self._events.emit('sweep_segment', {
                        'direction': 'forward', 'voltages': voltages,
                        'currents': currents, 'compliance': comp_list})
                    self._events.emit('sweep_segment', {
                        'direction': 'reverse', 'voltages': rev_v,
                        'currents': rev_i, 'compliance': rev_comp})
                else:
                    self._events.emit('sweep_segment', {
                        'direction': 'forward', 'voltages': voltages,
                        'currents': currents, 'compliance': comp_list})

                self._events.log('sweep_finished', f"Sweep complete: {points_summary}")
            except Exception as e:
                self._events.error('sweep_error', 'smu', f"Sweep error: {str(e)}")
            # Sweep is done — skip to finalization
            self._control.finish('completed')
            # Fall through to cleanup below


    def _enter_instrument_lock(self, address):
        """Hold the address for this run; released in _cleanup.

        Taken before anything is opened, so a second process is refused rather
        than allowed to interleave SCPI on the same bus. A lock the caller
        already holds is kept as it is.
        """
        if self._instrument_lock is not None:
            return self._instrument_lock
        return HeldInstrument(address)

    def _safety_prompt_declined(self) -> bool:
        """Ask before a hazardous voltage reaches the leads. True = cancel.

        The GUI asks in its own modal before starting, so it constructs runs
        with safety_ack='skip'; a headless client has no dialog, so the run
        itself must raise the question rather than silently energise leads at
        60 V.
        """
        if self._safety_ack != 'prompt':
            return False
        from ..safety import is_potentially_hazardous, warning_message

        measurement = self.settings.get('measurement', {})
        if bool(measurement.get('safety_voltage_warn_silenced', False)):
            return False
        check = is_potentially_hazardous(self.settings, self.mode)
        if not check.hazardous:
            return False
        if self._control.stopped():
            # Stop is already in: there is nobody to ask and nothing to start.
            return True

        prompt = self._control.raise_prompt(
            'safety_voltage_ack', ['acknowledge', 'cancel'], detail={
                'voltage_v': check.voltage_v,
                'threshold_v': check.threshold_v,
                'reason': check.reason,
                'message': warning_message(check),
            })
        self._events.emit('prompt', {
            'prompt_id': prompt.prompt_id, 'kind': prompt.kind,
            'options': prompt.options, 'requires_human': prompt.requires_human,
            'detail': prompt.detail,
        })
        choice, fields = self._control.wait_for_prompt(self._prompt_timeout_s)
        self._events.emit('prompt_resolved', {
            'prompt_id': prompt.prompt_id, 'choice': choice})
        if choice is None and not self._control.stopped():
            self._control.finish('prompt_timeout')
            self._events.warn('prompt_timeout',
                               "No answer to the touch-safety warning; abandoning the run.")
            return True
        if fields.get('silence_for_profile'):
            # Recorded on the event stream; persisting it belongs to whoever
            # owns the profile file, not to a run.
            self._events.log('safety_silenced',
                              "Touch-safety warning silenced for this profile.")
        if choice != 'acknowledge':
            # Cancelled, or stopped while the question was open. Without this
            # line the log's last entry was still the previous run's: nothing
            # recorded that a run was asked for and refused.
            self._events.log('safety_declined',
                              f"Run of '{self.sample_name}' not started: the touch-safety warning "
                              f"was not acknowledged ({check.reason} = {check.voltage_v:g} V, "
                              f"threshold {check.threshold_v:g} V).")
            return True
        return False

    def _spot_refused(self) -> bool:
        """Resolve this run's spot against the sample. True = do not start.

        This runs after run_started with the instrument lock held. Whatever
        goes wrong in it -- a settings value of the wrong type, arithmetic
        that overflows, a payload the event model rejects -- comes out as a
        refusal with the spot's own error code rather than as an unexpected
        worker error. A spot that cannot be checked is a spot that cannot be
        recorded.
        """
        try:
            return self._check_spot()
        except Exception as exc:
            self._events.error('spot_invalid', 'run', f"The spot cannot be recorded: {exc}")
            return True

    def _check_spot(self) -> bool:
        """The spot check itself. True = refused; may raise.

        Pure arithmetic on the settings, done before the instrument is opened,
        so a probe that is not on the sample never gets an output turned on
        under it. A spot near an edge is a warning, not a refusal: the
        measurement is valid, the centred correction is what is off, and the
        file records by how much.
        """
        if self.mode != 'four_point':
            return False
        self._spot_record = spot_record_from_settings(self.settings)
        record = self._spot_record
        if record is not None and record.ignored_position_correction is not None:
            self._events.warn('position_correction_ignored',
                f"Warning: fpp_position_correction is '{record.ignored_position_correction}', "
                f"which is not implemented. No position correction is applied; the file records 'warn'.")
        if record is None or record.position is None:
            return False
        position = record.position
        payload = {
            'spot': record.spot.model_dump(),
            'edge_clearance_s': position.edge_clearance_s,
            'edge_warn_pct': record.edge_warn_pct,
            'factor_here': position.factor_here,
            'factor_centre': position.factor_centre,
            'relative_error': position.relative_error,
            'factor_rows': record.factor_rows,
            'relative_error_rows': record.relative_error_rows,
            'compared_with': record.warning_compares_with,
        }
        if record.off_sample:
            message = (f"Spot '{record.spot.label}' is off the sample: a probe tip is "
                       f"{abs(position.edge_clearance_s):.2f} s beyond the edge.")
            self._events.emit('geometry_warning', {
                **payload, 'refused': True, 'reason': 'off_sample', 'message': message})
            self._events.error('spot_off_sample', 'run', message)
            return True
        if record.near_edge:
            if record.warning_compares_with == 'rows':
                compared = (f"the geometry factor this run applies ({record.factor_rows:.4g}) "
                            f"differs from the factor at the spot ({position.factor_here:.4g})")
            else:
                compared = (f"the factor at the centre of the sample ({position.factor_centre:.4g}) "
                            f"differs from the factor at the spot ({position.factor_here:.4g})")
            message = (f"Spot '{record.spot.label}' is {position.edge_clearance_s:.1f} s from "
                       f"the edge: {compared} by {abs(record.warning_error) * 100.0:.1f} % "
                       f"(threshold {record.edge_warn_pct:g} %). No position correction is applied.")
            self._events.emit('geometry_warning', {
                **payload, 'refused': False, 'reason': 'near_edge', 'message': message})
            self._events.warn('spot_near_edge', message)
        return False

    def execute(self):
        self.running = True
        self.paused = False
        instrument_ready = False
        file_ready = False
        # True when the run is turned away before it reaches the instrument:
        # reported as not ok whatever the reason, a stop included.
        refused = False

        # Everything from run_started on is inside this try. The steps before
        # the connect used to sit above it, and a fault in one of them left
        # the run without a run_ended and the instrument lock held for the
        # life of the process.
        try:
            self._events.emit('run_started', {
                'mode': self.mode,
                'sample_name': self.sample_name,
                'username': self.username,
                'settings': self.settings,
                'started_at': time.time(),
            })
            address = self.settings.get('measurement', {}).get('gpib_address', '')
            try:
                self._instrument_lock = self._enter_instrument_lock(address)
            except InstrumentBusy as exc:
                refused = True
                self._control.finish('instrument_busy')
                self._events.error('instrument_busy', 'smu', str(exc))
                return

            if self._spot_refused():
                refused = True
                self._control.finish('spot_refused')
                return

            if self._safety_prompt_declined():
                refused = True
                # finish() keeps the first reason, so a timeout reports as one.
                self._control.finish('cancelled')
                return

            measurement_settings = self.settings['measurement']
            file_settings = self.settings['file']

            sampling_rate = measurement_settings['sampling_rate']
            nplc = measurement_settings['nplc']
            settling_time = measurement_settings['settling_time']
            gpib_address = measurement_settings['gpib_address']
            visa_library = measurement_settings.get('visa_library', '')
            gpib_interface = measurement_settings.get('gpib_interface', '')
            auto_save_interval = file_settings['auto_save_interval']

            sample_interval = 1.0 / sampling_rate if sampling_rate > 0 else 0.1

            # Connect instrument
            try:
                self._events.log('connecting', f"Connecting to instrument at {gpib_address}...")
                self.keithley = Keithley2400(gpib_address, visa_library=visa_library,
                                             gpib_interface=gpib_interface).connect()
                self._instrument_idn = self.keithley.query("*IDN?").strip()
                self._events.log('connected', f"Connected to: {self._instrument_idn}")
                # Identify model and surface its limits — informational only;
                # the instrument enforces its own ranges via SCPI errors.
                self._model_spec = self.keithley.detect_model()
                # Short model string ("2400", "2410", ...) for accuracy.py
                # lookups. Falls back to "2400" if IDN parsing failed; that's
                # the most conservative baseline.
                self._model_name = self._model_spec.model if self._model_spec else "2400"
                try:
                    spec = self._model_spec
                    self._events.emit('instrument_connected', {
                        'address': gpib_address,
                        'idn': self._instrument_idn,
                        'model': self._model_name,
                        'max_source_v': spec.max_source_v if spec else None,
                        'max_source_i': spec.max_source_i if spec else None,
                        'max_power_w': spec.max_power_w if spec else None,
                    })
                except Exception:
                    pass
                if self._model_spec is not None:
                    spec = self._model_spec
                    self._events.log('model_detected', 
                        f"Detected: Keithley {spec.model} — "
                        f"max {spec.max_source_v:g}V / {spec.max_source_i:g}A / "
                        f"{spec.max_power_w:g}W"
                    )
                else:
                    self._events.warn('model_unknown', 
                        "Warning: instrument model not in known table — proceeding with defaults"
                    )
                try:
                    line_freq = float(self.keithley.query(":SYST:LFR?"))
                    self._events.emit('line_frequency', {'hz': line_freq, 'assumed': False})
                except Exception:
                    line_freq = 50.0
                    self._events.emit('line_frequency', {'hz': line_freq, 'assumed': True})
                    self._events.warn('lfr_assumed', "Warning: Could not query line frequency. Assuming 50Hz.")
                self.keithley.write("*RST"); time.sleep(0.5)
                self.keithley.write("*CLS")
                # Auto zero: ON (accurate), ONCE (fast), OFF (fastest)
                azer = str(measurement_settings.get('auto_zero', 'on')).upper()
                if azer == 'ONCE':
                    self.keithley.write(":SYST:AZER:STAT ON")
                    self.keithley.write(":SYST:AZER:STAT ONCE")
                else:
                    self.keithley.write(f":SYST:AZER:STAT {azer}")
                self.keithley.write(":SENS:FUNC:CONC OFF")
                self.keithley.write(":OUTP:SMOD HIMP")
                instrument_ready = True
            except Exception as e:
                self._events.error('connect_failed', 'smu', humanize_connection_error(e, gpib_address))
                self._control.finish('connect_failed')
                return

            # Configure instrument
            self._events.log('configuring', f"Configuring instrument for {self._mode_name} mode...")
            metadata = {}
            csv_headers = []
            source_value_str = ""

            try:
                if self.mode == 'resistance':
                    configured = configure_resistance(
                        self.keithley, self._events, measurement_settings, nplc,
                        max_source_v=self._model_spec.max_source_v if self._model_spec else None)
                elif self.mode == 'source_v':
                    configured = configure_source_v(self.keithley, self._events, measurement_settings, nplc)
                elif self.mode == 'source_i':
                    configured = configure_source_i(self.keithley, self._events, measurement_settings, nplc)
                elif self.mode == 'four_point':
                    configured = configure_four_point(self.keithley, self._events, measurement_settings, nplc)
                    if configured is None:
                        self._control.finish('power_envelope')
                        return  # pre-flight refused the power envelope
                    self._fpp_overpower_emitted = False  # debounce: emit once
                elif self.mode == 'sweep':
                    configured = configure_sweep(self.keithley, self._events, measurement_settings, nplc)
                else:
                    configured = None
                if configured is not None:
                    self._mode_state, metadata, csv_headers, source_value_str = configured

                # Hardware averaging filter (2400 series uses :SENS:AVER, not per-function paths)
                if measurement_settings.get('filter_enabled', False):
                    ftype = str(measurement_settings.get('filter_type', 'repeat')).upper()[:3]
                    fcount = int(measurement_settings.get('filter_count', 10))
                    self.keithley.write(f":SENS:AVER:TCON {ftype}")
                    self.keithley.write(f":SENS:AVER:COUN {fcount}")
                    self.keithley.write(":SENS:AVER ON")
                    self._events.log('filter', f"Hardware filter: {ftype} x{fcount}")

                self.keithley.write(":TRIG:DEL 0")
                self.keithley.write(":SOUR:DEL:AUTO ON")
            except Exception as e:
                self._events.error('configure_failed', 'smu', f"Error configuring instrument: {str(e)}")
                self._control.finish('configure_failed')
                return

            if not self._open_aux_sensor(measurement_settings):
                return

            if not self._open_output_file(measurement_settings, source_value_str):
                return
            file_ready = True

            # Prevent system sleep during measurement
            self._sleep_inhibitor.inhibit(f"ResistaMet: {self.mode} measurement on {self.sample_name}")

            # Sweep mode: one atomic operation, then straight to finalization
            if self.mode == 'sweep':
                self._run_sweep()

            # For sweep mode, self.running is already False — skip the polling loop
            if self.mode != 'sweep':
                # Continuous measurement modes: turn on output and enter polling loop
                self._events.log('starting', "Starting measurement...")
                try:
                    self.keithley.write(":OUTP ON")
                    self._events.log('settling', f"Waiting for settling time ({settling_time}s)...")
                    self._control.sleep(settling_time)
                except RunStopped:
                    raise
                except Exception as e:
                    self._events.error('output_on_failed', 'smu', f"Error turning on output: {str(e)}")
                    self._control.finish('output_on_failed')
                    # The data file is open by now. Leaving through the
                    # shutdown gives it its footer and the log its closing
                    # lines; a bare return left both to the silent backstop
                    # in _cleanup.
                    self._shut_down(instrument_ready, file_ready, nplc)
                    return

            last_save = self.start_time
            last_measurement_time = 0
            # For 4PP: respect a finite number of samples if provided
            target_samples = 0
            sample_count = 0
            if self.mode == 'four_point':
                try:
                    target_samples = int(measurement_settings.get('fpp_samples', 0))
                except Exception:
                    target_samples = 0
            end_time = None
            if self.mode in ('source_v', 'source_i'):
                dur = measurement_settings.get('vsource_duration_hours') if self.mode == 'source_v' else measurement_settings.get('isource_duration_hours')
                try:
                    dur_s = float(dur) * 3600.0
                    if dur_s > 0:
                        end_time = self.start_time + dur_s
                except Exception:
                    end_time = None

            # Retry configuration for transient errors (cable wiggle, etc.)
            was_paused = False
            # Time spent paused, excluded from the duration limit: a run
            # paused across its own deadline should still get the measuring
            # time it was asked for.
            paused_total = 0.0
            pause_began = 0.0
            max_retries = 5
            consecutive_errors = 0

            while self.running:
                if self.paused:
                    if not was_paused:
                        was_paused = True
                        pause_began = time.time()
                        self._events.emit('paused', {'reason': 'user'})
                    time.sleep(0.1)
                    continue
                if was_paused:
                    was_paused = False
                    paused_total += time.time() - pause_began
                    self._events.emit('resumed', {'reason': 'user'})
                now = time.time()
                if now - last_measurement_time >= sample_interval:
                    reading_str = None
                    read_success = False

                    # Delta mode: alternating +I/-I for 4PP thermoelectric cancellation
                    use_delta = (self.mode == 'four_point' and
                                 self._mode_state.delta_mode and
                                 self.keithley is not None)

                    if use_delta:
                        for retry in range(max_retries):
                            try:
                                reading_str = self._read_delta()
                                last_measurement_time = time.time()
                                read_success = True
                                if retry > 0:
                                    self._events.log('recovered', f"Delta read recovered after {retry} retries")
                                consecutive_errors = 0
                                break
                            except RunStopped:
                                raise
                            except Exception as e:
                                consecutive_errors += 1
                                if retry < max_retries - 1:
                                    delay = 0.1 * (2 ** retry)
                                    self._events.warn('retry', 
                                        f"Delta read error (retry {retry + 1}/{max_retries}): {str(e)[:50]}... "
                                        f"Retrying in {delay:.1f}s"
                                    )
                                    self._control.sleep(delay)
                                    try:
                                        self.keithley.write("*CLS")
                                    except Exception:
                                        pass
                                else:
                                    self._events.error('read_error', 'smu', 
                                        f"Delta read error after {max_retries} retries: {str(e)}. Stopping."
                                    )
                    else:
                        for retry in range(max_retries):
                            try:
                                reading_str = self.keithley.query(":READ?").strip()
                                last_measurement_time = time.time()
                                read_success = True
                                if retry > 0:
                                    self._events.log('recovered', f"Communication recovered after {retry} retries")
                                consecutive_errors = 0
                                break
                            except pyvisa.errors.VisaIOError as e:
                                consecutive_errors += 1
                                if retry < max_retries - 1:
                                    delay = 0.1 * (2 ** retry)
                                    self._events.warn('retry', 
                                        f"VISA error (retry {retry + 1}/{max_retries}): {str(e)[:50]}... "
                                        f"Retrying in {delay:.1f}s"
                                    )
                                    self._control.sleep(delay)
                                    try:
                                        self.keithley.write("*CLS")
                                    except Exception:
                                        pass
                                else:
                                    self._events.error('read_error', 'smu', 
                                        f"VISA Read Error after {max_retries} retries: {str(e)}. Stopping."
                                    )
                            except Exception as e:
                                self._events.error('read_error', 'smu', f"Unexpected Read Error: {str(e)}. Stopping.")
                                break

                    if self._control.stopped():
                        break
                    if not read_success:
                        self._control.finish('read_error')
                        break

                    elapsed_time = now - self.start_time
                    compliance_status = 'OK'
                    compliance_type = None
                    data_dict: Dict[str, float] = {}

                    # Parse reading — all modes include STAT as last element
                    # Fixed element order: RES,STAT or VOLT,CURR,STAT
                    parts = [p.strip() for p in reading_str.split(',') if p.strip()]

                    # Extract status word (last element) for compliance detection
                    stat_word = 0
                    try:
                        stat_word = int(float(parts[-1]))
                    except (ValueError, IndexError):
                        pass
                    hw_compliance = bool(stat_word & _STAT_BIT_COMPLIANCE)

                    if self.mode == 'resistance':
                        parsed = parse_resistance(
                            parts, stat_word, hw_compliance, measurement_settings, nplc,
                            self._model_name, self._mode_state, self._events, reading_str)
                    elif self.mode == 'source_v':
                        parsed = parse_source_v(
                            parts, stat_word, hw_compliance, measurement_settings, nplc,
                            self._model_name, self._mode_state, self._events)
                    elif self.mode == 'source_i':
                        parsed = parse_source_i(
                            parts, stat_word, hw_compliance, measurement_settings, nplc,
                            self._model_name, self._mode_state, self._events)
                    elif self.mode == 'four_point':
                        parsed = parse_four_point(
                            parts, stat_word, hw_compliance, measurement_settings, nplc,
                            self._model_name, self._mode_state, self._events)
                    else:
                        parsed = ({}, 'OK', None)
                    data_dict, compliance_status, compliance_type = parsed

                    # Auxiliary-sensor sample at the measurement instant —
                    # shared across every mode that opened a sensor
                    # (Keithley-centric co-logging). read_latest() is a
                    # non-blocking cache read (the driver's reader thread does
                    # the waiting), so the acquisition loop never stalls on
                    # the sensor. Channel values are PRESERVED even when
                    # flagged; provenance rides in the aux_fault column
                    # (sensors.reading_to_columns). A stale/missing cache
                    # records NaN + 'read_error' and the run continues.
                    if self._aux_sensor is not None:
                        try:
                            aux_cols = reading_to_columns(self._aux_sensor.read_latest())
                        except Exception:
                            aux_cols = {name: float('nan') for name in self._aux_columns}
                            aux_cols['aux_fault'] = 'read_error'
                        data_dict.update(aux_cols)
                        self._aux_row_values = [
                            aux_cols.get(name, float('nan')) for name in self._aux_columns
                        ]
                        fault = aux_cols.get('aux_fault', '0')
                        if fault != self._aux_last_fault:
                            if fault != '0':
                                self._events.warn('aux_fault', f"Warning: Auxiliary sensor: {fault}")
                            self._aux_last_fault = fault

                    stop_on_comp = bool(measurement_settings.get('stop_on_compliance', False))
                    if compliance_status != 'OK' and compliance_type:
                        try:
                            self._events.emit('compliance', {
                                'kind': compliance_type,
                                'stop_on_compliance': stop_on_comp,
                            })
                            self._events.warn('compliance', f"Warning: {compliance_type} Compliance Hit!")
                        except Exception:
                            pass
                        if stop_on_comp:
                            self._events.log('compliance_stop', "Stopping due to compliance (per settings).")
                            self._control.finish('compliance_stop')

                    if self.mode == 'four_point':
                        self._warn_once_if_ratio_negative(data_dict, compliance_status)

                    # 4PP probe-safety runtime check: measured V*I against the
                    # configured warn / hard-stop thresholds. Hard stop also
                    # turns the output off on the worker side as a defense in
                    # depth — _cleanup will run :OUTP OFF too on exit.
                    if self.mode == 'four_point':
                        v_meas = data_dict.get('voltage', float('nan'))
                        i_meas = data_dict.get('current', float('nan'))
                        if np.isfinite(v_meas) and np.isfinite(i_meas):
                            measured_power = abs(v_meas * i_meas)
                            stop_w = self._mode_state.power_stop_w
                            warn_w = self._mode_state.power_warn_w
                            if (measured_power > stop_w
                                    and self._mode_state.stop_on_overpower):
                                if not self._fpp_overpower_emitted:
                                    self._fpp_overpower_emitted = True
                                    try:
                                        self._events.emit('overpower_trip', {
                                            'measured_w': measured_power, 'stop_w': stop_w})
                                    except Exception:
                                        pass
                                self._events.error('overpower', 'run', 
                                    f"4PP overpower: {format_power(measured_power)} "
                                    f"exceeds hard stop {format_power(stop_w)}. "
                                    f"Stopping to protect probe and sample."
                                )
                                try:
                                    self.keithley.write(":OUTP OFF")
                                except Exception:
                                    pass
                                self._control.finish('overpower')
                            elif measured_power > warn_w:
                                self._events.warn('power_envelope', 
                                    f"Warning: 4PP power {format_power(measured_power)} above "
                                    f"warn threshold {format_power(warn_w)}"
                                )

                    # Atomically get and clear event marker (thread-safe)
                    event_marker = self.get_and_clear_event_marker()
                    if event_marker:
                        self._events.log('event_marked', f"Event marked at {elapsed_time:.3f}s: {event_marker}")

                    row_data, derived = build_row(
                        self.mode, elapsed_time, data_dict, compliance_status, event_marker,
                        measurement_settings, nplc, use_delta, self._model_name,
                        self._last_delta)

                    # Splice auxiliary-sensor values (+ aux_fault) for any mode
                    # that opened a sensor — after any delta columns, before
                    # compliance/event, mirroring get_column_config()'s splice.
                    if self._aux_sensor is not None and self._aux_columns:
                        row_data = splice_before_tail(row_data, self._aux_row_values)

                    # Write to exporter (handles both JSON and CSV)
                    try:
                        self.exporter.write_row(row_data)
                        self._csv_error_count = 0  # Reset error count on success
                    except Exception as e:
                        self._csv_error_count += 1
                        error_msg = f"Error writing data ({self._csv_error_count}/{self._max_csv_errors}): {str(e)}"
                        self._events.warn('write_failed', f"Warning: {error_msg}")

                        if self._csv_error_count >= self._max_csv_errors:
                            # Escalate: too many consecutive write failures (likely disk full)
                            self._events.error('write_failed', 'file', 
                                f"CRITICAL: {self._csv_error_count} consecutive write failures. "
                                f"Possible disk full or write permission issue. Stopping measurement to prevent data loss."
                            )
                            self._control.finish('write_error')
                            break

                    self._keep_for_statistics(data_dict, derived, compliance_status)

                    sample_payload = {
                        't_unix': now,
                        'elapsed_s': elapsed_time,
                        'compliance': compliance_status,
                        'event_marker': event_marker,
                        'values': data_dict,
                    }
                    if derived:
                        sample_payload['derived'] = derived
                    if use_delta and self._last_delta:
                        sample_payload['delta'] = dict(self._last_delta)
                    self._events.emit('sample', sample_payload)

                    # Increment sample count for 4PP and stop if target reached
                    if self.mode == 'four_point':
                        sample_count += 1
                        if target_samples > 0 and sample_count >= target_samples:
                            self._events.log('target_reached', f"Reached target samples: {target_samples}. Stopping.")
                            self._control.finish('target_samples')

                    if now - last_save >= auto_save_interval:
                        try:
                            if self.exporter:
                                self.exporter.flush()
                            last_save = now
                        except Exception as e:
                            self._events.warn('autosave_failed', f"Warning: Auto-save failed - {str(e)}")

                    # Periodic instrument health check
                    self._periodic_health_check(now)

                    elapsed_time_formatted = time.strftime("%H:%M:%S", time.gmtime(elapsed_time))
                    status_msg = f"Running {self._mode_name}: {elapsed_time_formatted}"
                    if self.mode == 'resistance':
                        rv = data_dict.get('resistance', float('nan'))
                        status_msg += f" | R: {rv:.4f} Ω" if np.isfinite(rv) else " | R: Invalid"
                    elif self.mode == 'source_v':
                        cv = data_dict.get('current', float('nan'))
                        vv = data_dict.get('voltage', float('nan'))
                        status_msg += (f" | I: {cv:.4e} A" if np.isfinite(cv) else " | I: Invalid")
                        status_msg += (f" | V: {vv:.4e} V" if np.isfinite(vv) else " | V: Invalid")
                    else:
                        vv = data_dict.get('voltage', float('nan'))
                        iv = data_dict.get('current', float('nan'))
                        status_msg += (f" | V: {vv:.4e} V" if np.isfinite(vv) else " | V: Invalid")
                        status_msg += (f" | I: {iv:.4e} A" if np.isfinite(iv) else " | I: Invalid")
                    if compliance_status != 'OK':
                        status_msg += f" ({compliance_status})"
                    self._events.log('progress', status_msg)

                time.sleep(0.01 if sample_interval <= 0.001 else max(0.001, sample_interval / 10.0))

                if end_time is not None and time.time() - paused_total >= end_time:
                    self._events.log('duration_reached', "Reached configured duration. Stopping.")
                    self._control.finish('duration')

            self._shut_down(instrument_ready, file_ready, nplc)

        except RunStopped:
            # A stop landed during a settle or a retry backoff. Listed first:
            # RunStopped is an Exception, and the handler below would
            # otherwise report a stop as a fault.
            #
            # The exception carried control past the shutdown at the end of
            # the try block, so it is run here. Without it _cleanup still
            # turned the output off and closed the file, but the file had no
            # footer and the log never said the output was off or where the
            # data went: a stop in the settle looked like a crash.
            try:
                self._shut_down(instrument_ready, file_ready, nplc)
            except Exception as e:
                self._control.finish('worker_error')
                self._events.error('worker_error', 'run', f"Unexpected Worker Error ({self._mode_name}): {str(e)}")
        except Exception as e:
            self._control.finish('worker_error')
            self._events.error('worker_error', 'run', f"Unexpected Worker Error ({self._mode_name}): {str(e)}")
            # Whatever went wrong, a file that was opened still gets its
            # footer. A no-op when the fault came after the shutdown began.
            if file_ready:
                try:
                    self._shut_down(instrument_ready, file_ready, nplc)
                except Exception as shutdown_error:
                    logger.warning(f"shutdown after a worker error failed: {shutdown_error}")
        finally:
            # Read the counters before cleanup releases the exporter.
            samples = self.exporter.row_count if self.exporter else 0
            self._cleanup()
            self._write_pending_map_summary()
            reason = self._control.finish_reason or 'completed'
            self._events.emit('run_ended', {
                'reason': reason,
                'ok': (not refused and
                       reason in ('completed', 'target_samples', 'duration', 'user_stop')),
                'samples': samples,
                'duration_s': time.time() - self.start_time if self.start_time else 0.0,
                'path': self.filename or None,
            })
            self.running = False

    def _shut_down(self, instrument_ready, file_ready, nplc):
        """The end of a run that got as far as its instrument: output off,
        the file's footer, the closing log line.

        ``_cleanup`` runs after this on every exit and would also turn the
        output off and close the file, but silently and with no footer; it
        is the backstop, this is the record.

        Runs once: every way out of execute() after the file is open comes
        through here, and a fault inside the shutdown itself must not send
        the run round it a second time.
        """
        if self._shut_down_started:
            return
        self._shut_down_started = True
        if instrument_ready and self.keithley:
            try:
                self.keithley.write(":OUTP OFF")
                self._events.log('output_off', "Output turned OFF.")
            except Exception as e:
                self._events.warn('output_off_failed', f"Warning: Could not turn off output - {str(e)}")

        final_message = f"Measurement ({self._mode_name}) stopped."
        if file_ready and self.exporter:
            try:
                end_time = datetime.now()
                end_metadata = {
                    'ended_at': end_time.isoformat(),
                    'total_samples': self.exporter.row_count,
                    'duration_s': time.time() - self.start_time
                }
                spot_stats = self._spot_statistics(nplc)
                if spot_stats is not None:
                    end_metadata['spot_stats'] = spot_stats
                self.exporter.finalize(end_metadata)
                self._events.emit('file_finalized', {
                    'path': self.filename, 'end_metadata': end_metadata})
            except Exception as e:
                spot_stats = None
                self._events.warn('finalize_failed', f"Warning: Error finalizing export - {str(e)}")
            if spot_stats is not None:
                self._announce_spot(spot_stats)
            final_message = f"Measurement ({self._mode_name}) completed! Data saved to: {self.filename}"
        self._events.log('completed', final_message)
        self._events.emit('acquisition_finished', {'mode': self.mode})

    def _warn_once_if_ratio_negative(self, data_dict, compliance_status):
        """Say so, once, when a four-point sample's V/I is negative.

        A passive sample cannot have a negative resistance. With the sense
        leads open the voltmeter floats (the bench saw about -0.95 V) and the
        run recorded -43 kΩ/sq, in compliance with nothing, with no word from
        the log. Swapped sense leads give the same sign.

        A warning only: the sample, the row and the run are left exactly as
        they were. Delta mode is excluded because its reading is the
        half-difference of two polarities, where an offset of either sign
        cancels by design and the sign test means something else.
        """
        if self._fpp_negative_ratio_warned or self._mode_state.delta_mode:
            return
        if compliance_status != 'OK':
            return
        voltage = data_dict.get('voltage', float('nan'))
        current = data_dict.get('current', float('nan'))
        if not (np.isfinite(voltage) and np.isfinite(current)):
            return
        if voltage * current < 0:
            self._fpp_negative_ratio_warned = True
            self._events.warn('fpp_negative_ratio',
                f"Warning: four-point V/I is negative (V = {voltage:.4g} V at "
                f"I = {current:.4g} A). The sense leads are probably open or swapped."
            )

    def _keep_for_statistics(self, data_dict, derived, compliance_status):
        """Keep a written four-point row's values for the end-of-run statistics.

        Its own guard, outside the one around the file write: a failure here
        is not a write failure and must not count towards the three that stop
        a run. The write's error count is zero exactly when this sample's row
        reached the file, so the statistics cover the rows the file holds.
        """
        if self._spot_samples is None or self._csv_error_count != 0:
            return
        try:
            self._spot_samples.add(data_dict.get('voltage'), data_dict.get('current'),
                                   derived, compliance_status)
        except Exception as e:
            if not self._spot_sample_warned:
                self._spot_sample_warned = True
                self._events.warn('spot_sample_failed',
                    f"Warning: A sample could not be kept for the spot statistics - {str(e)}")

    def _spot_statistics(self, nplc):
        """The four-point statistics for the file footer; None for other modes.

        A failure here is reported and swallowed: the rows are the data, and
        the file must still be finalized when its summary cannot be computed.
        """
        if self._spot_samples is None:
            return None
        try:
            stats = spot_statistics(self._spot_samples, model=self._model_name, nplc=nplc)
            # The same expression run_ended uses. It lets a map tell a spot
            # that was stopped early from one that ran its course; what a map
            # should do about it is not decided here.
            stats['end_reason'] = self._control.finish_reason or 'completed'
            return stats
        except Exception as e:
            self._events.warn('spot_stats_failed', f"Warning: Could not compute spot statistics - {str(e)}")
            return None

    def _announce_spot(self, spot_stats):
        """Emit spot_complete for a file that has just been finalized.

        Outside the guard around the finalize, with a code of its own: by now
        the file is closed and whole, and a failure to announce it must not be
        reported as a failure to finalize it.
        """
        record = self._spot_record
        try:
            self._events.emit('spot_complete', {
                'spot': record.spot.model_dump() if record else None,
                'path': self.filename,
                'stats': spot_stats,
            })
        except Exception as e:
            self._events.warn('spot_complete_failed',
                f"Warning: The data file is complete, but its statistics could not be reported - {str(e)}")
        if record is not None:
            # Written later, by _write_pending_map_summary.
            self._pending_map_id = record.spot.map_id

    def _write_pending_map_summary(self):
        """Refresh ``<map_id>_map.json`` beside this run's file, if it has a spot.

        Called after _cleanup and before run_ended. After _cleanup, because
        assembling a map reads every four-point file in the directory and the
        directory may be a slow network share: the instrument is closed and
        its lock released first, so nothing waits on the share but this run's
        own last event. Before run_ended, because run_ended is promised to be
        the last event of a run and the map_summary log line is one.

        The summary is derived from the run files and can be rebuilt at any
        time, so failing to write it is a warning and never the run's failure.
        """
        map_id, self._pending_map_id = self._pending_map_id, None
        if map_id is None:
            return
        try:
            path = write_map_summary(Path(self.filename).parent, map_id)
            self._events.log('map_summary', f"Map summary: {path.name}")
        except Exception as e:
            try:
                self._events.warn('map_summary_failed', f"Warning: Could not write the map summary - {str(e)}")
            except Exception:
                logger.warning("could not report a failed map summary", exc_info=True)

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

    def mark_event(self, name: str = "MARK") -> None:
        self._control.mark_event(name)

    def pause_measurement(self) -> None:
        if self.running:
            self.paused = True
            self._events.log('paused', f"Measurement ({self._mode_name}) paused")

    def resume_measurement(self) -> None:
        if self.running:
            self.paused = False
            self._events.log('resumed', f"Measurement ({self._mode_name}) resumed")

    def stop_measurement(self) -> None:
        self._events.emit('stopping', {'reason': 'user_stop'})
        self._events.log('stopping', f"Stopping measurement ({self._mode_name})...")
        self._control.finish('user_stop')

    def _release_instrument_lock(self) -> None:
        held = getattr(self, '_instrument_lock', None)
        if held is not None:
            self._instrument_lock = None
            try:
                held.release()
            except Exception:
                logger.warning("failed to release the instrument lock", exc_info=True)

    def _cleanup(self) -> None:
        try:
            # Re-enable system sleep
            self._sleep_inhibitor.uninhibit()

            if self.keithley:
                try:
                    self.keithley.write(":OUTP OFF")
                    self.keithley.close()
                    self._events.log('cleanup', "Instrument disconnected.")
                except Exception as e:
                    self._events.warn('cleanup', f"Warning: Error during instrument cleanup: {str(e)}")
                finally:
                    self.keithley = None
            if self._aux_sensor is not None:
                try:
                    self._aux_sensor.close()
                except Exception as e:
                    self._events.warn('cleanup', f"Warning: Error during aux-sensor cleanup: {str(e)}")
                finally:
                    self._aux_sensor = None
            if self.exporter:
                try:
                    # Ensure exporter is finalized if not already
                    self.exporter.finalize()
                except Exception as e:
                    logger.warning(f"Error finalizing exporter during cleanup: {e}")
                finally:
                    self.exporter = None
        finally:
            # Released last, after the instrument and aux ports are closed:
            # on a failure exit the :OUTP OFF above is the only one, and a
            # process that got the address before it landed could have its
            # own output turned off under it, or on before ours was off.
            self._release_instrument_lock()

    def _check_instrument_errors(self) -> Optional[str]:
        """Check instrument error queue and return any errors.

        Returns:
            Error message if instrument has errors, None otherwise.
        """
        if not self.keithley:
            return None

        try:
            # Query error queue - format: error_code,"error_message"
            response = self.keithley.query(":SYST:ERR?").strip()
            if response:
                parts = response.split(',', 1)
                error_code = int(parts[0])
                if error_code != 0:
                    error_msg = parts[1].strip('"') if len(parts) > 1 else "Unknown error"
                    return f"Instrument error {error_code}: {error_msg}"
        except Exception as e:
            logger.debug(f"Error checking instrument status: {e}")

        return None

    def _periodic_health_check(self, now: float) -> None:
        """Perform periodic instrument health check.

        Args:
            now: Current timestamp
        """
        if now - self._last_error_check >= self._error_check_interval:
            self._last_error_check = now
            error = self._check_instrument_errors()
            if error:
                self._events.warn('instrument_error_queue', f"Warning: {error}")
                logger.warning(f"Instrument error during measurement: {error}")

    def _read_delta(self) -> str:
        """Perform a current-reversal (delta) measurement for 4PP.

        Takes two readings at +I and -I, computes V_delta = (V+ - V-) / 2
        to cancel thermoelectric EMF. Returns a synthetic reading string
        in the same format as a normal :READ? response (VOLT,CURR,STAT).

        Side effect: stashes the raw V+, V- and the derived R_f, R_r on
        self._last_delta so the main loop can log per-polarity values per
        F84 §13.1 (forward/reverse resistances kept separate).
        """
        i_mag = abs(self._mode_state.source_current)
        settling = self._mode_state.delta_settling

        # +I reading
        self.keithley.write(f":SOUR:CURR {i_mag}")
        self._control.sleep(settling)
        raw_plus = self.keithley.query(":READ?").strip()
        parts_plus = [p.strip() for p in raw_plus.split(',')]
        v_plus = float(parts_plus[0])
        stat_plus = int(float(parts_plus[-1]))

        # -I reading
        self.keithley.write(f":SOUR:CURR {-i_mag}")
        self._control.sleep(settling)
        raw_minus = self.keithley.query(":READ?").strip()
        parts_minus = [p.strip() for p in raw_minus.split(',')]
        v_minus = float(parts_minus[0])
        stat_minus = int(float(parts_minus[-1]))

        # Restore positive polarity for next cycle
        self.keithley.write(f":SOUR:CURR {i_mag}")

        # Delta calculation: V_delta = (V+ - V-) / 2
        v_delta = (v_plus - v_minus) / 2.0
        # Per-polarity resistances per F84 §13.1. R_r negates I and V_minus
        # so a symmetric DUT gives R_f ≈ R_r > 0; thermal offset shows up
        # as a difference between them.
        try:
            r_f = v_plus / i_mag if i_mag != 0 else float('nan')
            r_r = (-v_minus) / i_mag if i_mag != 0 else float('nan')
        except Exception:
            r_f = float('nan')
            r_r = float('nan')
        self._last_delta = {
            'v_plus': v_plus, 'v_minus': v_minus,
            'r_f': r_f, 'r_r': r_r,
        }
        # Compliance: OR of both readings
        stat_combined = stat_plus | stat_minus

        # Return synthetic reading string matching VOLT,CURR,STAT format
        return f"{v_delta},{i_mag},{stat_combined}"

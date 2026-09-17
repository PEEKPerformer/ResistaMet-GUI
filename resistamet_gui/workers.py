import logging
import os
import re
import time
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pyvisa
from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)

from .constants import (
    __version__,
    __original_version__,
    __author__,
    AUX_READY_TIMEOUT_S,
)

# Keithley 2400 series STATUS word bit masks (24-bit)
# Bit 3: Compliance — source is in real compliance
_STAT_BIT_COMPLIANCE = 1 << 3
from .accuracy import (
    current_source_uncertainty, current_uncertainty,
    resistance_uncertainty, voltage_source_uncertainty, voltage_uncertainty,
)
from .data_export import (
    AUX_LOG_MODES, build_metadata, get_column_config, make_exporter,
    splice_before_tail,
)
from .instrument import Keithley2400, humanize_connection_error
from .session.configure import (
    FourPointState, ResistanceState, SweepState,
    configure_four_point, configure_resistance, configure_source_i,
    configure_source_v, configure_sweep,
)
from .session.control import RunControl
from .session.run_files import create_base_path, open_exporter, sanitize_path_component
from .session.samples import (
    build_row, parse_four_point, parse_resistance, parse_source_i, parse_source_v,
)
from .sensors import aux_column_names, make_sensor, reading_to_columns
from .system_utils import SleepInhibitor


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


class ContinuousRun:
    """One continuous-mode or sweep run, start to finalize.

    Owns the instrument session, the exporter and the acquisition loop. It
    reports through an outputs facade and reads stop/pause from a RunControl,
    so the same procedure serves the QThread adapter below and the headless
    session. No Qt here.
    """

    def __init__(self, mode, sample_name, username, settings, control, out):
        if mode not in ['resistance', 'source_v', 'source_i', 'four_point', 'sweep']:
            raise ValueError(f"Invalid measurement mode: {mode}")
        self.mode = mode
        self.sample_name = sample_name
        self.username = username
        self.settings = settings
        self._out = out
        # Set by the configure step: the frozen per-mode state the loop reads.
        self._mode_state = None
        # Set by each delta read: the per-polarity values the row builder logs.
        self._last_delta = None

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
                self._out.status_update(
                    f"Connecting to auxiliary sensor ({driver}) at {aux_address}..."
                )
                self._aux_sensor = make_sensor(driver, aux_address).open()
                if hasattr(self._aux_sensor, 'wait_ready'):
                    self._aux_sensor.wait_ready(AUX_READY_TIMEOUT_S)
                self._aux_channels = self._aux_sensor.channels()
                self._aux_columns = aux_column_names(self._aux_sensor)
                self._aux_units = [ch.unit for ch in self._aux_channels] + ['']
                self._out.status_update(
                    "Auxiliary sensor ready: "
                    + ", ".join(f"{ch.label} ({ch.unit})" for ch in self._aux_channels)
                )
            except Exception as e:
                self._out.error_occurred(
                    "Auxiliary sensor: " + humanize_connection_error(e, aux_address)
                )
                return
        return True

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
            )
            names = ", ".join(p.name for p in self.exporter.output_paths)
            self._out.status_update(f"Data file: {names}")
        except Exception as e:
            self._out.error_occurred(f"Error creating output files: {str(e)}")
            return
        return True

    def _run_sweep(self):
        """Run the instrument's own sweep engine, write the points, report them."""
        # Sweep mode: single atomic operation, then done
        if self.mode == 'sweep':
            self._out.status_update(f"Running I-V sweep ({self._mode_state.points} points)...")
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

                # For up_down: run reverse sweep
                if self._mode_state.up_down:
                    self._out.status_update("Running reverse sweep...")
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
                    # Emit both sweeps
                    self._out.sweep_complete(voltages, currents, comp_list)
                    self._out.sweep_complete(rev_v, rev_i, rev_comp)
                else:
                    self._out.sweep_complete(voltages, currents, comp_list)

                self._out.status_update(f"Sweep complete: {len(voltages)} points acquired")
            except Exception as e:
                self._out.error_occurred(f"Sweep error: {str(e)}")
            # Sweep is done — skip to finalization
            self.running = False
            # Fall through to cleanup below

    def execute(self):
        self.running = True
        self.paused = False
        instrument_ready = False
        file_ready = False

        try:
            measurement_settings = self.settings['measurement']
            file_settings = self.settings['file']

            sampling_rate = measurement_settings['sampling_rate']
            nplc = measurement_settings['nplc']
            settling_time = measurement_settings['settling_time']
            gpib_address = measurement_settings['gpib_address']
            auto_save_interval = file_settings['auto_save_interval']

            sample_interval = 1.0 / sampling_rate if sampling_rate > 0 else 0.1

            # Connect instrument
            try:
                self._out.status_update(f"Connecting to instrument at {gpib_address}...")
                self.keithley = Keithley2400(gpib_address).connect()
                self._instrument_idn = self.keithley.query("*IDN?").strip()
                self._out.status_update(f"Connected to: {self._instrument_idn}")
                # Identify model and surface its limits — informational only;
                # the instrument enforces its own ranges via SCPI errors.
                self._model_spec = self.keithley.detect_model()
                # Short model string ("2400", "2410", ...) for accuracy.py
                # lookups. Falls back to "2400" if IDN parsing failed; that's
                # the most conservative baseline.
                self._model_name = self._model_spec.model if self._model_spec else "2400"
                try:
                    self._out.instrument_identified(self._model_name)
                except Exception:
                    pass
                if self._model_spec is not None:
                    spec = self._model_spec
                    self._out.status_update(
                        f"Detected: Keithley {spec.model} — "
                        f"max {spec.max_source_v:g}V / {spec.max_source_i:g}A / "
                        f"{spec.max_power_w:g}W"
                    )
                else:
                    self._out.status_update(
                        "Warning: instrument model not in known table — proceeding with defaults"
                    )
                try:
                    line_freq = float(self.keithley.query(":SYST:LFR?"))
                except Exception:
                    line_freq = 50.0
                    self._out.status_update("Warning: Could not query line frequency. Assuming 50Hz.")
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
                self._out.error_occurred(humanize_connection_error(e, gpib_address))
                return

            # Configure instrument
            self._out.status_update(f"Configuring instrument for {self.mode} mode...")
            metadata = {}
            csv_headers = []
            source_value_str = ""

            try:
                if self.mode == 'resistance':
                    configured = configure_resistance(self.keithley, self._out, measurement_settings, nplc)
                elif self.mode == 'source_v':
                    configured = configure_source_v(self.keithley, self._out, measurement_settings, nplc)
                elif self.mode == 'source_i':
                    configured = configure_source_i(self.keithley, self._out, measurement_settings, nplc)
                elif self.mode == 'four_point':
                    configured = configure_four_point(self.keithley, self._out, measurement_settings, nplc)
                    if configured is None:
                        return  # pre-flight refused the power envelope
                    self._fpp_overpower_emitted = False  # debounce: emit once
                elif self.mode == 'sweep':
                    configured = configure_sweep(self.keithley, self._out, measurement_settings, nplc)
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
                    self._out.status_update(f"Hardware filter: {ftype} x{fcount}")

                self.keithley.write(":TRIG:DEL 0")
                self.keithley.write(":SOUR:DEL:AUTO ON")
            except Exception as e:
                self._out.error_occurred(f"Error configuring instrument: {str(e)}")
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
                self._out.status_update("Starting measurement...")
                try:
                    self.keithley.write(":OUTP ON")
                    self._out.status_update(f"Waiting for settling time ({settling_time}s)...")
                    time.sleep(settling_time)
                except Exception as e:
                    self._out.error_occurred(f"Error turning on output: {str(e)}")
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
            max_retries = 5
            consecutive_errors = 0

            while self.running:
                if self.paused:
                    time.sleep(0.1)
                    continue
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
                                    self._out.status_update(f"Delta read recovered after {retry} retries")
                                consecutive_errors = 0
                                break
                            except Exception as e:
                                consecutive_errors += 1
                                if retry < max_retries - 1:
                                    delay = 0.1 * (2 ** retry)
                                    self._out.status_update(
                                        f"Delta read error (retry {retry + 1}/{max_retries}): {str(e)[:50]}... "
                                        f"Retrying in {delay:.1f}s"
                                    )
                                    time.sleep(delay)
                                    try:
                                        self.keithley.write("*CLS")
                                    except Exception:
                                        pass
                                else:
                                    self._out.error_occurred(
                                        f"Delta read error after {max_retries} retries: {str(e)}. Stopping."
                                    )
                    else:
                        for retry in range(max_retries):
                            try:
                                reading_str = self.keithley.query(":READ?").strip()
                                last_measurement_time = time.time()
                                read_success = True
                                if retry > 0:
                                    self._out.status_update(f"Communication recovered after {retry} retries")
                                consecutive_errors = 0
                                break
                            except pyvisa.errors.VisaIOError as e:
                                consecutive_errors += 1
                                if retry < max_retries - 1:
                                    delay = 0.1 * (2 ** retry)
                                    self._out.status_update(
                                        f"VISA error (retry {retry + 1}/{max_retries}): {str(e)[:50]}... "
                                        f"Retrying in {delay:.1f}s"
                                    )
                                    time.sleep(delay)
                                    try:
                                        self.keithley.write("*CLS")
                                    except Exception:
                                        pass
                                else:
                                    self._out.error_occurred(
                                        f"VISA Read Error after {max_retries} retries: {str(e)}. Stopping."
                                    )
                            except Exception as e:
                                self._out.error_occurred(f"Unexpected Read Error: {str(e)}. Stopping.")
                                break

                    if not read_success:
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
                            self._model_name, self._mode_state, self._out)
                    elif self.mode == 'source_v':
                        parsed = parse_source_v(
                            parts, stat_word, hw_compliance, measurement_settings, nplc,
                            self._model_name, self._mode_state, self._out)
                    elif self.mode == 'source_i':
                        parsed = parse_source_i(
                            parts, stat_word, hw_compliance, measurement_settings, nplc,
                            self._model_name, self._mode_state, self._out)
                    elif self.mode == 'four_point':
                        parsed = parse_four_point(
                            parts, stat_word, hw_compliance, measurement_settings, nplc,
                            self._model_name, self._mode_state, self._out)
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
                                self._out.status_update(f"⚠️ Auxiliary sensor: {fault}")
                            self._aux_last_fault = fault

                    stop_on_comp = bool(measurement_settings.get('stop_on_compliance', False))
                    if compliance_status != 'OK' and compliance_type:
                        try:
                            self._out.compliance_hit(compliance_type)
                            self._out.status_update(f"⚠️ {compliance_type} Compliance Hit!")
                        except Exception:
                            pass
                        if stop_on_comp:
                            self._out.status_update("Stopping due to compliance (per settings).")
                            self.running = False

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
                                        self._out.overpower_hit(measured_power, stop_w)
                                    except Exception:
                                        pass
                                self._out.error_occurred(
                                    f"4PP overpower: {measured_power*1e3:.1f} mW "
                                    f"exceeds hard stop {stop_w*1e3:.0f} mW. "
                                    f"Stopping to protect probe and sample."
                                )
                                try:
                                    self.keithley.write(":OUTP OFF")
                                except Exception:
                                    pass
                                self.running = False
                            elif measured_power > warn_w:
                                self._out.status_update(
                                    f"⚠️ 4PP power {measured_power*1e3:.1f} mW above "
                                    f"warn threshold {warn_w*1e3:.0f} mW"
                                )

                    # Atomically get and clear event marker (thread-safe)
                    event_marker = self.get_and_clear_event_marker()
                    if event_marker:
                        self._out.status_update(f"Event marked at {elapsed_time:.3f}s: {event_marker}")

                    row_data = build_row(
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
                        self._out.status_update(f"Warning: {error_msg}")

                        if self._csv_error_count >= self._max_csv_errors:
                            # Escalate: too many consecutive write failures (likely disk full)
                            self._out.error_occurred(
                                f"CRITICAL: {self._csv_error_count} consecutive write failures. "
                                f"Possible disk full or write permission issue. Stopping measurement to prevent data loss."
                            )
                            self.running = False
                            break

                    self._out.data_point(now, data_dict, compliance_status, event_marker)

                    # Increment sample count for 4PP and stop if target reached
                    if self.mode == 'four_point':
                        sample_count += 1
                        if target_samples > 0 and sample_count >= target_samples:
                            self._out.status_update(f"Reached target samples: {target_samples}. Stopping.")
                            self.running = False

                    if now - last_save >= auto_save_interval:
                        try:
                            if self.exporter:
                                self.exporter.flush()
                            last_save = now
                        except Exception as e:
                            self._out.status_update(f"Warning: Auto-save failed - {str(e)}")

                    # Periodic instrument health check
                    self._periodic_health_check(now)

                    elapsed_time_formatted = time.strftime("%H:%M:%S", time.gmtime(elapsed_time))
                    status_msg = f"Running {self.mode}: {elapsed_time_formatted}"
                    if self.mode == 'resistance':
                        rv = data_dict.get('resistance', float('nan'))
                        status_msg += f" | R: {rv:.4f} Ohms" if np.isfinite(rv) else " | R: Invalid"
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
                    self._out.status_update(status_msg)

                time.sleep(0.01 if sample_interval <= 0.001 else max(0.001, sample_interval / 10.0))

                if end_time is not None and time.time() >= end_time:
                    self._out.status_update("Reached configured duration. Stopping.")
                    self.running = False

            if instrument_ready and self.keithley:
                try:
                    self.keithley.write(":OUTP OFF")
                    self._out.status_update("Output turned OFF.")
                except Exception as e:
                    self._out.status_update(f"Warning: Could not turn off output - {str(e)}")

            final_message = f"Measurement ({self.mode}) stopped."
            if file_ready and self.exporter:
                try:
                    end_time = datetime.now()
                    end_metadata = {
                        'ended_at': end_time.isoformat(),
                        'total_samples': self.exporter.row_count,
                        'duration_s': time.time() - self.start_time
                    }
                    self.exporter.finalize(end_metadata)
                except Exception as e:
                    self._out.status_update(f"Warning: Error finalizing export - {str(e)}")
                final_message = f"Measurement ({self.mode}) completed! Data saved to: {self.filename}"
            self._out.status_update(final_message)
            self._out.measurement_complete(self.mode)

        except Exception as e:
            self._out.error_occurred(f"Unexpected Worker Error ({self.mode}): {str(e)}")
        finally:
            self._cleanup()
            self.running = False

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

    def mark_event(self, name: str = "MARK") -> None:
        self._control.mark_event(name)

    def pause_measurement(self) -> None:
        if self.running:
            self.paused = True
            self._out.status_update(f"Measurement ({self.mode}) paused")

    def resume_measurement(self) -> None:
        if self.running:
            self.paused = False
            self._out.status_update(f"Measurement ({self.mode}) resumed")

    def stop_measurement(self) -> None:
        self._out.status_update(f"Stopping measurement ({self.mode})...")
        self.running = False

    def _cleanup(self) -> None:
        # Re-enable system sleep
        self._sleep_inhibitor.uninhibit()

        if self.keithley:
            try:
                self.keithley.write(":OUTP OFF")
                self.keithley.close()
                self._out.status_update("Instrument disconnected.")
            except Exception as e:
                self._out.status_update(f"Warning: Error during instrument cleanup: {str(e)}")
            finally:
                self.keithley = None
        if self._aux_sensor is not None:
            try:
                self._aux_sensor.close()
            except Exception as e:
                self._out.status_update(f"Warning: Error during aux-sensor cleanup: {str(e)}")
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
                self._out.status_update(f"Warning: {error}")
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
        time.sleep(settling)
        raw_plus = self.keithley.query(":READ?").strip()
        parts_plus = [p.strip() for p in raw_plus.split(',')]
        v_plus = float(parts_plus[0])
        stat_plus = int(float(parts_plus[-1]))

        # -I reading
        self.keithley.write(f":SOUR:CURR {-i_mag}")
        time.sleep(settling)
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
        self.sample_name = sample_name
        self.username = username
        self.settings = settings
        self._out = _QtOutputs(self)
        self._control = RunControl()
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

    def run(self) -> None:
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

import logging
import os
import re
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pyvisa
from PyQt5.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)

from .constants import (
    __version__,
    __original_version__,
    __author__,
)

# Keithley 2400 series STATUS word bit masks (24-bit)
# Bit 3: Compliance — source is in real compliance
_STAT_BIT_COMPLIANCE = 1 << 3
from .data_export import DualExporter, get_column_config, build_metadata
from .instrument import Keithley2400
from .system_utils import SleepInhibitor


class MeasurementWorker(QThread):
    """Worker thread for running measurements in different modes."""
    data_point = pyqtSignal(float, dict, str, str)  # timestamp, data dict, compliance, event
    status_update = pyqtSignal(str)
    measurement_complete = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    compliance_hit = pyqtSignal(str)  # 'Voltage' or 'Current'
    overpower_hit = pyqtSignal(float, float)  # measured_power_w, hard_stop_w (4PP only)
    sweep_complete = pyqtSignal(list, list, list)  # voltages, currents, compliance_list

    def __init__(self, mode, sample_name, username, settings, parent=None):
        super().__init__(parent)
        if mode not in ['resistance', 'source_v', 'source_i', 'four_point', 'sweep']:
            raise ValueError(f"Invalid measurement mode: {mode}")
        self.mode = mode
        self.sample_name = sample_name
        self.username = username
        self.settings = settings

        # Thread-safe state management
        self._state_lock = threading.Lock()
        self._running = False
        self._paused = False
        self._event_marker = ""
        self._csv_error_count = 0  # Track consecutive CSV write failures
        self._max_csv_errors = 3   # Max consecutive errors before escalation

        self.keithley = None
        self.exporter: Optional[DualExporter] = None
        self.start_time = 0
        self.filename = ""
        self._instrument_idn = ""

        # System sleep prevention
        self._sleep_inhibitor = SleepInhibitor()

        # Instrument health monitoring
        self._last_error_check = 0
        self._error_check_interval = 30.0  # Check instrument errors every 30 seconds

    @property
    def running(self) -> bool:
        """Thread-safe access to running state."""
        with self._state_lock:
            return self._running

    @running.setter
    def running(self, value: bool) -> None:
        """Thread-safe setter for running state."""
        with self._state_lock:
            self._running = value

    @property
    def paused(self) -> bool:
        """Thread-safe access to paused state."""
        with self._state_lock:
            return self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        """Thread-safe setter for paused state."""
        with self._state_lock:
            self._paused = value

    @property
    def event_marker(self) -> str:
        """Thread-safe access to event marker."""
        with self._state_lock:
            return self._event_marker

    @event_marker.setter
    def event_marker(self, value: str) -> None:
        """Thread-safe setter for event marker."""
        with self._state_lock:
            self._event_marker = value

    def get_and_clear_event_marker(self) -> str:
        """Atomically get and clear the event marker."""
        with self._state_lock:
            marker = self._event_marker
            self._event_marker = ""
            return marker

    def run(self):
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
                self.status_update.emit(f"Connecting to instrument at {gpib_address}...")
                self.keithley = Keithley2400(gpib_address).connect()
                self._instrument_idn = self.keithley.query("*IDN?").strip()
                self.status_update.emit(f"Connected to: {self._instrument_idn}")
                # Identify model and surface its limits — informational only;
                # the instrument enforces its own ranges via SCPI errors.
                self._model_spec = self.keithley.detect_model()
                if self._model_spec is not None:
                    spec = self._model_spec
                    self.status_update.emit(
                        f"Detected: Keithley {spec.model} — "
                        f"max {spec.max_source_v:g}V / {spec.max_source_i:g}A / "
                        f"{spec.max_power_w:g}W"
                    )
                else:
                    self.status_update.emit(
                        "Warning: instrument model not in known table — proceeding with defaults"
                    )
                try:
                    line_freq = float(self.keithley.query(":SYST:LFR?"))
                except Exception:
                    line_freq = 50.0
                    self.status_update.emit("Warning: Could not query line frequency. Assuming 50Hz.")
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
                self.error_occurred.emit(f"Error connecting to instrument: {str(e)}")
                return

            # Configure instrument
            self.status_update.emit(f"Configuring instrument for {self.mode} mode...")
            metadata = {}
            csv_headers = []
            source_value_str = ""

            try:
                if self.mode == 'resistance':
                    test_current = measurement_settings['res_test_current']
                    voltage_compliance = measurement_settings['res_voltage_compliance']
                    measurement_type = measurement_settings['res_measurement_type']
                    auto_range = measurement_settings['res_auto_range']

                    self.keithley.write(":SYST:RSEN ON" if measurement_type == "4-wire" else ":SYST:RSEN OFF")
                    self.keithley.write(":SENS:FUNC 'RES'")
                    # Disable auto-ohms before configuring source/compliance
                    # (auto-ohms is ON by default after selecting RES function
                    # and rejects :SOUR:CURR:RANG, :SOUR:CURR, :SENS:VOLT:PROT)
                    self.keithley.write(":SENS:RES:MODE MAN")
                    self.keithley.write(":SOUR:FUNC CURR")
                    self.keithley.write(f":SOUR:CURR:RANG {abs(test_current)}")
                    self.keithley.write(f":SOUR:CURR {test_current}")
                    self.keithley.write(f":SENS:VOLT:PROT {voltage_compliance}")
                    self.keithley.write(f":SENS:RES:NPLC {nplc}")
                    if auto_range:
                        self.keithley.write(":SENS:RES:MODE AUTO")
                    else:
                        max_r = voltage_compliance / abs(test_current) if abs(test_current) > 0 else 210e6
                        self.keithley.write(f":SENS:RES:RANG {max_r}")
                    # Offset-compensated ohms: cancels thermoelectric EMF
                    if measurement_settings.get('res_offset_comp', False):
                        self.keithley.write(":SENS:RES:OCOM ON")
                    # Cable null: software subtraction (2400 series lacks :SENS:RES:REL)
                    self._cable_null = float(measurement_settings.get('res_cable_null', 0.0))
                    # Include STAT for hardware compliance detection (bit 3)
                    # Fixed element order: RES, STAT
                    self.keithley.write(":FORM:ELEM RES,STAT")

                    metadata = {
                        'Mode': 'Resistance Measurement',
                        'Test Current (A)': test_current,
                        'Voltage Compliance (V)': voltage_compliance,
                        'Measurement Type': measurement_type,
                        'Resistance Auto Range': 'ON' if auto_range else 'OFF',
                    }
                    csv_headers = ['Timestamp (Unix)', 'Elapsed Time (s)', 'Resistance (Ohms)', 'Compliance Status', 'Event']
                    source_value_str = f"{test_current*1000:.2f}mA"

                elif self.mode == 'source_v':
                    source_voltage = measurement_settings['vsource_voltage']
                    current_compliance = measurement_settings['vsource_current_compliance']
                    auto_range_curr = measurement_settings['vsource_current_range_auto']

                    self.keithley.write(":SYST:RSEN OFF")
                    self.keithley.write(":SENS:FUNC 'CURR:DC'")
                    self.keithley.write(":SOUR:FUNC VOLT")
                    self.keithley.write(f":SOUR:VOLT:RANG {abs(source_voltage)}")
                    self.keithley.write(f":SOUR:VOLT {source_voltage}")
                    self.keithley.write(f":SENS:CURR:PROT {current_compliance}")
                    self.keithley.write(":SENS:CURR:RANG:AUTO ON" if auto_range_curr else ":SENS:CURR:RANG:AUTO OFF")
                    if not auto_range_curr:
                        self.keithley.write(f":SENS:CURR:RANG {current_compliance}")
                    self.keithley.write(f":SENS:CURR:NPLC {nplc}")
                    # Keithley 2400 series always returns elements in fixed order:
                    # VOLT, CURR, RES, TIME, STAT — regardless of FORM:ELEM argument order
                    # Include STAT for hardware compliance detection (bit 3)
                    self.keithley.write(":FORM:ELEM VOLT,CURR,STAT")

                    metadata = {
                        'Mode': 'Voltage Source',
                        'Source Voltage (V)': source_voltage,
                        'Current Compliance (A)': current_compliance,
                        'Current Auto Range': 'ON' if auto_range_curr else 'OFF',
                    }
                    csv_headers = ['Timestamp (Unix)', 'Elapsed Time (s)', 'Voltage (V)', 'Current (A)', 'Resistance (Ohms)', 'Compliance Status', 'Event']
                    source_value_str = f"{source_voltage:.3f}V"

                elif self.mode == 'source_i':
                    source_current = measurement_settings['isource_current']
                    voltage_compliance = measurement_settings['isource_voltage_compliance']
                    auto_range_volt = measurement_settings['isource_voltage_range_auto']

                    self.keithley.write(":SYST:RSEN OFF")
                    self.keithley.write(":SENS:FUNC 'VOLT:DC'")
                    self.keithley.write(":SOUR:FUNC CURR")
                    self.keithley.write(f":SOUR:CURR:RANG {abs(source_current)}")
                    self.keithley.write(f":SOUR:CURR {source_current}")
                    self.keithley.write(f":SENS:VOLT:PROT {voltage_compliance}")
                    self.keithley.write(":SENS:VOLT:RANG:AUTO ON" if auto_range_volt else ":SENS:VOLT:RANG:AUTO OFF")
                    if not auto_range_volt:
                        self.keithley.write(f":SENS:VOLT:RANG {voltage_compliance}")
                    self.keithley.write(f":SENS:VOLT:NPLC {nplc}")
                    self.keithley.write(":FORM:ELEM VOLT,CURR,STAT")

                    metadata = {
                        'Mode': 'Current Source',
                        'Source Current (A)': source_current,
                        'Voltage Compliance (V)': voltage_compliance,
                        'Voltage Auto Range': 'ON' if auto_range_volt else 'OFF',
                    }
                    csv_headers = ['Timestamp (Unix)', 'Elapsed Time (s)', 'Voltage (V)', 'Current (A)', 'Resistance (Ohms)', 'Compliance Status', 'Event']
                    source_value_str = f"{source_current*1000:.2f}mA"

                elif self.mode == 'four_point':
                    # Use I-source and measure V (like source_i), but compute derived quantities for 4-pt probe
                    source_current = measurement_settings.get('fpp_current')
                    voltage_compliance = measurement_settings.get('fpp_voltage_compliance')
                    auto_range_volt = measurement_settings.get('fpp_voltage_range_auto')

                    # 4-wire (remote sense) is REQUIRED for a real 4-point probe measurement.
                    # The probe head wires outer tips to Force HI/LO and inner tips to Sense
                    # HI/LO (Signatone S-302 manual, page 6). With RSEN OFF the voltmeter
                    # routes back to the Force terminals, measuring across the current-
                    # carrying outer pair — i.e. a 2-wire measurement that includes contact
                    # and spreading resistance.
                    self.keithley.write(":SYST:RSEN ON")
                    self.keithley.write(":SENS:FUNC 'VOLT:DC'")
                    self.keithley.write(":SOUR:FUNC CURR")
                    self.keithley.write(f":SOUR:CURR:RANG {abs(source_current)}")
                    self.keithley.write(f":SOUR:CURR {source_current}")
                    self.keithley.write(f":SENS:VOLT:PROT {voltage_compliance}")
                    self.keithley.write(":SENS:VOLT:RANG:AUTO ON" if auto_range_volt else ":SENS:VOLT:RANG:AUTO OFF")
                    if not auto_range_volt:
                        self.keithley.write(f":SENS:VOLT:RANG {voltage_compliance}")
                    self.keithley.write(f":SENS:VOLT:NPLC {nplc}")
                    self.keithley.write(":FORM:ELEM VOLT,CURR,STAT")

                    # Delta mode settings
                    self._fpp_delta_mode = bool(measurement_settings.get('fpp_delta_mode', False))
                    self._fpp_delta_settling = float(measurement_settings.get('fpp_delta_settling', 0.1))
                    self._fpp_source_current = source_current

                    # Probe-safety thresholds (4PP only). The pre-flight check
                    # below uses the configured worst-case I*V_compliance; the
                    # runtime monitor uses measured V*I per sample.
                    self._fpp_power_warn_w = float(
                        measurement_settings.get('fpp_power_warn_w', 1.0e-2)
                    )
                    self._fpp_power_stop_w = float(
                        measurement_settings.get('fpp_power_stop_w', 1.0e-1)
                    )
                    self._fpp_stop_on_overpower = bool(
                        measurement_settings.get('fpp_stop_on_overpower', True)
                    )
                    self._fpp_overpower_emitted = False  # debounce: emit once

                    # Pre-flight power envelope check: worst case is the user
                    # asking for the full source current at the full compliance
                    # voltage, i.e. probe sees I_source * V_compliance.
                    worst_case_power = abs(source_current) * abs(voltage_compliance)
                    if worst_case_power > self._fpp_power_stop_w:
                        self.error_occurred.emit(
                            f"Configured 4PP power ({worst_case_power*1e3:.1f} mW = "
                            f"{abs(source_current)*1e3:.3g} mA × {abs(voltage_compliance):.3g} V) "
                            f"exceeds the probe-safety hard stop "
                            f"({self._fpp_power_stop_w*1e3:.0f} mW). Lower the source "
                            f"current or the voltage compliance, or raise fpp_power_stop_w "
                            f"in settings if you've reviewed the probe spec."
                        )
                        return
                    if worst_case_power > self._fpp_power_warn_w:
                        self.status_update.emit(
                            f"⚠️ 4PP power envelope: up to {worst_case_power*1e3:.1f} mW "
                            f"(I × V_comp). Above warning threshold "
                            f"{self._fpp_power_warn_w*1e3:.0f} mW — proceed with care."
                        )

                    metadata = {
                        'Mode': 'Four-Point Probe',
                        'Source Current (A)': source_current,
                        'Voltage Compliance (V)': voltage_compliance,
                        'Spacing s (cm)': measurement_settings.get('fpp_spacing_cm'),
                        'Thickness t (µm)': measurement_settings.get('fpp_thickness_um'),
                        'Alpha': measurement_settings.get('fpp_alpha'),
                        'K Factor': measurement_settings.get('fpp_k_factor'),
                        'Model': measurement_settings.get('fpp_model'),
                        'Delta Mode': self._fpp_delta_mode,
                    }
                    csv_headers = ['Timestamp (Unix)', 'Elapsed Time (s)', 'Voltage (V)', 'Current (A)', 'V/I (Ohms)', 'Sheet Rs (Ohms/sq)', 'Resistivity (Ohm*cm)', 'Conductivity (S/cm)', 'Compliance Status', 'Event']
                    source_value_str = f"{source_current*1000:.2f}mA"
                    if self._fpp_delta_mode:
                        source_value_str += "_delta"

                elif self.mode == 'sweep':
                    sweep_source = measurement_settings.get('sweep_source', 'voltage')
                    sweep_start = float(measurement_settings.get('sweep_start', 0.0))
                    sweep_stop = float(measurement_settings.get('sweep_stop', 1.0))
                    sweep_step = float(measurement_settings.get('sweep_step', 0.05))
                    sweep_compliance = float(measurement_settings.get('sweep_compliance', 0.1))
                    sweep_delay = float(measurement_settings.get('sweep_delay', 0.01))
                    sweep_direction = measurement_settings.get('sweep_direction', 'up')

                    src_func = 'VOLT' if sweep_source == 'voltage' else 'CURR'
                    # For down direction, swap start/stop
                    if sweep_direction == 'down':
                        sweep_start, sweep_stop = sweep_stop, sweep_start

                    self._sweep_points = self.keithley.setup_sweep(
                        src_func, sweep_start, sweep_stop, sweep_step,
                        sweep_compliance, nplc, sweep_delay
                    )
                    self._sweep_source = src_func
                    self._sweep_direction = sweep_direction
                    # For up_down: double the points (forward + reverse)
                    if sweep_direction == 'up_down':
                        self.keithley.write(":SOUR:SWE:DIR UP")
                        # We'll do two separate sweeps
                        self._sweep_up_down = True
                    else:
                        self._sweep_up_down = False

                    metadata = {
                        'Mode': 'I-V Sweep',
                        'Source Function': sweep_source,
                        'Start': sweep_start,
                        'Stop': sweep_stop,
                        'Step': sweep_step,
                        'Compliance': sweep_compliance,
                        'Delay (s)': sweep_delay,
                        'Direction': sweep_direction,
                        'Points': self._sweep_points,
                    }
                    csv_headers = ['Point', 'Voltage (V)', 'Current (A)', 'Compliance Status']
                    source_value_str = f"sweep_{sweep_start}to{sweep_stop}"

                # Hardware averaging filter (2400 series uses :SENS:AVER, not per-function paths)
                if measurement_settings.get('filter_enabled', False):
                    ftype = str(measurement_settings.get('filter_type', 'repeat')).upper()[:3]
                    fcount = int(measurement_settings.get('filter_count', 10))
                    self.keithley.write(f":SENS:AVER:TCON {ftype}")
                    self.keithley.write(f":SENS:AVER:COUN {fcount}")
                    self.keithley.write(":SENS:AVER ON")
                    self.status_update.emit(f"Hardware filter: {ftype} x{fcount}")

                self.keithley.write(":TRIG:DEL 0")
                self.keithley.write(":SOUR:DEL:AUTO ON")
            except Exception as e:
                self.error_occurred.emit(f"Error configuring instrument: {str(e)}")
                return

            # File setup with dual export (JSON + CSV)
            self.start_time = time.time()
            try:
                base_path = self._create_base_path(source_value_str)
                self.filename = str(base_path.with_suffix('.json'))  # Primary is JSON

                # Get column configuration for this mode
                columns, units = get_column_config(self.mode, measurement_settings)

                # Build metadata
                export_metadata = build_metadata(
                    user=self.username,
                    sample_name=self.sample_name,
                    mode=self.mode,
                    settings=self.settings,
                    instrument_idn=self._instrument_idn,
                    start_time=datetime.fromtimestamp(self.start_time)
                )

                # Initialize dual exporter
                self.exporter = DualExporter(
                    base_path=base_path,
                    metadata=export_metadata,
                    columns=columns,
                    units=units
                )
                file_ready = True
                self.status_update.emit(f"Data files: {base_path.name}.json/.csv")
            except Exception as e:
                self.error_occurred.emit(f"Error creating output files: {str(e)}")
                return

            # Prevent system sleep during measurement
            self._sleep_inhibitor.inhibit(f"ResistaMet: {self.mode} measurement on {self.sample_name}")

            # Sweep mode: single atomic operation, then done
            if self.mode == 'sweep':
                self.status_update.emit(f"Running I-V sweep ({self._sweep_points} points)...")
                try:
                    self.keithley.write(":OUTP ON")
                    # Increase timeout for long sweeps
                    if self.keithley.dev:
                        self.keithley.dev.timeout = max(10000, self._sweep_points * 1000)
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
                    if getattr(self, '_sweep_up_down', False):
                        self.status_update.emit("Running reverse sweep...")
                        # Swap start/stop for reverse
                        if self._sweep_source == 'VOLT':
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
                        self.sweep_complete.emit(voltages, currents, comp_list)
                        self.sweep_complete.emit(rev_v, rev_i, rev_comp)
                    else:
                        self.sweep_complete.emit(voltages, currents, comp_list)

                    self.status_update.emit(f"Sweep complete: {len(voltages)} points acquired")
                except Exception as e:
                    self.error_occurred.emit(f"Sweep error: {str(e)}")
                # Sweep is done — skip to finalization
                self.running = False
                # Fall through to cleanup below

            # For sweep mode, self.running is already False — skip the polling loop
            if self.mode != 'sweep':
                # Continuous measurement modes: turn on output and enter polling loop
                self.status_update.emit("Starting measurement...")
                try:
                    self.keithley.write(":OUTP ON")
                    self.status_update.emit(f"Waiting for settling time ({settling_time}s)...")
                    time.sleep(settling_time)
                except Exception as e:
                    self.error_occurred.emit(f"Error turning on output: {str(e)}")
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
                                 getattr(self, '_fpp_delta_mode', False) and
                                 self.keithley is not None)

                    if use_delta:
                        try:
                            reading_str = self._read_delta()
                            last_measurement_time = time.time()
                            read_success = True
                            consecutive_errors = 0
                        except Exception as e:
                            consecutive_errors += 1
                            if consecutive_errors >= max_retries:
                                self.error_occurred.emit(f"Delta read error after {consecutive_errors} failures: {str(e)}. Stopping.")
                            else:
                                self.status_update.emit(f"Delta read error (attempt {consecutive_errors}): {str(e)[:50]}")
                                try:
                                    self.keithley.write("*CLS")
                                except Exception:
                                    pass
                    else:
                        for retry in range(max_retries):
                            try:
                                reading_str = self.keithley.query(":READ?").strip()
                                last_measurement_time = time.time()
                                read_success = True
                                if retry > 0:
                                    self.status_update.emit(f"Communication recovered after {retry} retries")
                                consecutive_errors = 0
                                break
                            except pyvisa.errors.VisaIOError as e:
                                consecutive_errors += 1
                                if retry < max_retries - 1:
                                    delay = 0.1 * (2 ** retry)
                                    self.status_update.emit(
                                        f"VISA error (retry {retry + 1}/{max_retries}): {str(e)[:50]}... "
                                        f"Retrying in {delay:.1f}s"
                                    )
                                    time.sleep(delay)
                                    try:
                                        self.keithley.write("*CLS")
                                    except Exception:
                                        pass
                                else:
                                    self.error_occurred.emit(
                                        f"VISA Read Error after {max_retries} retries: {str(e)}. Stopping."
                                    )
                            except Exception as e:
                                self.error_occurred.emit(f"Unexpected Read Error: {str(e)}. Stopping.")
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
                        try:
                            value = float(parts[0])
                        except Exception:
                            value = float('nan')
                        compliance_type = 'Voltage'
                        if hw_compliance:
                            compliance_status = 'V_COMP'
                        if not np.isfinite(value):
                            value = float('nan')
                            self.status_update.emit(f"Invalid value detected ({reading_str})")
                        # Apply software cable null if set
                        cable_null = getattr(self, '_cable_null', 0.0)
                        if cable_null != 0.0 and np.isfinite(value):
                            value -= cable_null
                        data_dict = {'resistance': value}
                    elif self.mode == 'source_v':
                        # Keithley 2400 series returns elements in fixed order:
                        # VOLT, CURR, STAT
                        try:
                            voltage = float(parts[0])
                            current = float(parts[1]) if len(parts) > 1 else float('nan')
                        except Exception:
                            voltage = float('nan'); current = float('nan')
                        compliance_type = 'Current'
                        comp_limit_i = measurement_settings.get('vsource_current_compliance')
                        if hw_compliance or (np.isfinite(current) and abs(current) >= comp_limit_i * 0.99):
                            compliance_status = 'I_COMP'
                        data_dict = {'current': current, 'voltage': voltage}
                    elif self.mode == 'source_i':
                        try:
                            voltage = float(parts[0])
                            current = float(parts[1]) if len(parts) > 1 else float('nan')
                        except Exception:
                            voltage = float('nan'); current = float('nan')
                        compliance_type = 'Voltage'
                        comp_limit_v = measurement_settings.get('isource_voltage_compliance')
                        if hw_compliance or (np.isfinite(voltage) and abs(voltage) >= comp_limit_v * 0.99):
                            compliance_status = 'V_COMP'
                        data_dict = {'voltage': voltage, 'current': current}
                    elif self.mode == 'four_point':
                        try:
                            voltage = float(parts[0])
                            current = float(parts[1]) if len(parts) > 1 else float('nan')
                        except Exception:
                            voltage = float('nan'); current = float('nan')
                        compliance_type = 'Voltage'
                        comp_limit_v = measurement_settings.get('fpp_voltage_compliance')
                        if hw_compliance or (np.isfinite(voltage) and abs(voltage) >= comp_limit_v * 0.99):
                            compliance_status = 'V_COMP'
                        data_dict = {'voltage': voltage, 'current': current}

                    stop_on_comp = bool(measurement_settings.get('stop_on_compliance', False))
                    if compliance_status != 'OK' and compliance_type:
                        try:
                            self.compliance_hit.emit(compliance_type)
                            self.status_update.emit(f"⚠️ {compliance_type} Compliance Hit!")
                        except Exception:
                            pass
                        if stop_on_comp:
                            self.status_update.emit("Stopping due to compliance (per settings).")
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
                            stop_w = getattr(self, '_fpp_power_stop_w', 1.0e-1)
                            warn_w = getattr(self, '_fpp_power_warn_w', 1.0e-2)
                            if (measured_power > stop_w
                                    and getattr(self, '_fpp_stop_on_overpower', True)):
                                if not self._fpp_overpower_emitted:
                                    self._fpp_overpower_emitted = True
                                    try:
                                        self.overpower_hit.emit(measured_power, stop_w)
                                    except Exception:
                                        pass
                                self.error_occurred.emit(
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
                                self.status_update.emit(
                                    f"⚠️ 4PP power {measured_power*1e3:.1f} mW above "
                                    f"warn threshold {warn_w*1e3:.0f} mW"
                                )

                    # Atomically get and clear event marker (thread-safe)
                    event_marker = self.get_and_clear_event_marker()
                    if event_marker:
                        self.status_update.emit(f"Event marked at {elapsed_time:.3f}s: {event_marker}")

                    # Build row data with raw values (exporter handles formatting)
                    if self.mode == 'resistance':
                        r = data_dict.get('resistance', float('nan'))
                        row_data = [elapsed_time, r, compliance_status, event_marker]
                    elif self.mode == 'four_point':
                        v = data_dict.get('voltage', float('nan'))
                        i = data_dict.get('current', float('nan'))

                        # Decide F84-decomposed path vs legacy K*alpha path.
                        # F84 is selected if the user supplied any F84-only
                        # input (finite D, non-circle geometry, or a T+dopant
                        # combo). Defaults keep the legacy path active so
                        # existing config.json users see identical numbers.
                        diameter_cm = float(measurement_settings.get('fpp_diameter_cm') or 0.0)
                        geometry = str(measurement_settings.get('fpp_geometry') or 'circle')
                        temp_raw = measurement_settings.get('fpp_temperature_c')
                        try:
                            temp_c_val: Optional[float] = float(temp_raw)
                            if not np.isfinite(temp_c_val):
                                temp_c_val = None
                        except (TypeError, ValueError):
                            temp_c_val = None
                        dopant = str(measurement_settings.get('fpp_dopant_type') or 'none').lower()
                        use_f84 = (diameter_cm > 0
                                   or geometry != 'circle'
                                   or (temp_c_val is not None and dopant in ('n', 'p')))

                        # Common kwargs for the legacy path.
                        fpp_kwargs = dict(
                            spacing_cm=float(measurement_settings.get('fpp_spacing_cm') or 0.1016),
                            thickness_um=float(measurement_settings.get('fpp_thickness_um') or 0.0),
                            k_factor=float(measurement_settings.get('fpp_k_factor') or 4.532),
                            alpha=float(measurement_settings.get('fpp_alpha') or 1.0),
                            model=str(measurement_settings.get('fpp_model') or 'thin_film'),
                        )

                        if use_f84:
                            # F84 path: F2·w·F(w/S)·F_sp [·F_T].
                            from .calculations import (
                                calculate_four_point_probe_f84, calculate_conductivity,
                                calculate_ratio, estimate_current_floor,
                            )
                            spacing_cm = fpp_kwargs['spacing_cm']
                            thickness_um = fpp_kwargs['thickness_um']
                            thickness_cm = thickness_um * 1e-4
                            v_for_calc = v
                            i_for_calc = i
                            ratio_for_calc = calculate_ratio(v, i)
                            if compliance_status != 'OK':
                                src_i = float(measurement_settings.get('fpp_current') or 1e-3)
                                v_comp = float(measurement_settings.get('fpp_voltage_compliance') or 5.0)
                                i_floor = estimate_current_floor(src_i)
                                i_eff = max(abs(i), i_floor) if np.isfinite(i) else i_floor
                                ratio_for_calc = abs(v_comp) / i_eff if i_eff > 0 else float('nan')
                                v_for_calc = abs(v_comp)
                                i_for_calc = i_eff
                            f84 = calculate_four_point_probe_f84(
                                voltage=v_for_calc, current=i_for_calc,
                                spacing_cm=spacing_cm, thickness_um=thickness_um,
                                diameter_cm=diameter_cm if diameter_cm > 0 else None,
                                geometry=geometry,
                                temperature_c=temp_c_val,
                                dopant_type=dopant if dopant in ('n', 'p') else None,
                            )
                            rs_val = (
                                f84.rho_T / thickness_cm
                                if (thickness_cm > 0 and np.isfinite(f84.rho_T))
                                else float('nan')
                            )
                            # Use rho_23 when available; otherwise rho_T.
                            rho_report = f84.rho_23 if f84.rho_23 is not None else f84.rho_T
                            sigma = calculate_conductivity(rho_report)
                            row_data = [
                                elapsed_time, v, i,
                                ratio_for_calc, rs_val,
                                rho_report, sigma,
                                compliance_status, event_marker
                            ]
                        else:
                            # Legacy path: K * alpha * t * (V/I).
                            if compliance_status != 'OK':
                                from .calculations import calculate_four_point_probe_bound
                                result = calculate_four_point_probe_bound(
                                    v_compliance=float(measurement_settings.get('fpp_voltage_compliance') or 5.0),
                                    measured_current=i,
                                    source_current=float(measurement_settings.get('fpp_current') or 1e-3),
                                    **fpp_kwargs,
                                )
                            else:
                                from .calculations import calculate_four_point_probe
                                result = calculate_four_point_probe(
                                    voltage=v, current=i, **fpp_kwargs,
                                )
                            row_data = [
                                elapsed_time, v, i,
                                result.ratio, result.sheet_resistance,
                                result.resistivity, result.conductivity,
                                compliance_status, event_marker
                            ]

                        # Splice per-polarity columns when delta mode produced
                        # the reading. Position: just before compliance/event,
                        # mirroring get_column_config()'s splice.
                        if use_delta and getattr(self, '_last_delta', None):
                            ld = self._last_delta
                            insert_at = len(row_data) - 2  # before compliance, event
                            row_data = (
                                row_data[:insert_at]
                                + [ld['v_plus'], ld['v_minus'], ld['r_f'], ld['r_r']]
                                + row_data[insert_at:]
                            )
                    else:
                        # source_v or source_i
                        v = data_dict.get('voltage', float('nan'))
                        i = data_dict.get('current', float('nan'))
                        r = (v / i) if (np.isfinite(v) and np.isfinite(i) and i != 0) else float('nan')
                        if self.mode == 'source_v':
                            row_data = [elapsed_time, v, i, r, compliance_status, event_marker]
                        else:
                            row_data = [elapsed_time, v, i, r, compliance_status, event_marker]

                    # Write to exporter (handles both JSON and CSV)
                    try:
                        self.exporter.write_row(row_data)
                        self._csv_error_count = 0  # Reset error count on success
                    except Exception as e:
                        self._csv_error_count += 1
                        error_msg = f"Error writing data ({self._csv_error_count}/{self._max_csv_errors}): {str(e)}"
                        self.status_update.emit(f"Warning: {error_msg}")

                        if self._csv_error_count >= self._max_csv_errors:
                            # Escalate: too many consecutive write failures (likely disk full)
                            self.error_occurred.emit(
                                f"CRITICAL: {self._csv_error_count} consecutive write failures. "
                                f"Possible disk full or write permission issue. Stopping measurement to prevent data loss."
                            )
                            self.running = False
                            break

                    self.data_point.emit(now, data_dict, compliance_status, event_marker)

                    # Increment sample count for 4PP and stop if target reached
                    if self.mode == 'four_point':
                        sample_count += 1
                        if target_samples > 0 and sample_count >= target_samples:
                            self.status_update.emit(f"Reached target samples: {target_samples}. Stopping.")
                            self.running = False

                    if now - last_save >= auto_save_interval:
                        try:
                            if self.exporter:
                                self.exporter.flush()
                            last_save = now
                        except Exception as e:
                            self.status_update.emit(f"Warning: Auto-save failed - {str(e)}")

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
                    self.status_update.emit(status_msg)

                time.sleep(0.01 if sample_interval <= 0.001 else max(0.001, sample_interval / 10.0))

                if end_time is not None and time.time() >= end_time:
                    self.status_update.emit("Reached configured duration. Stopping.")
                    self.running = False

            if instrument_ready and self.keithley:
                try:
                    self.keithley.write(":OUTP OFF")
                    self.status_update.emit("Output turned OFF.")
                except Exception as e:
                    self.status_update.emit(f"Warning: Could not turn off output - {str(e)}")

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
                    self.status_update.emit(f"Warning: Error finalizing export - {str(e)}")
                final_message = f"Measurement ({self.mode}) completed! Data saved to: {self.filename}"
            self.status_update.emit(final_message)
            self.measurement_complete.emit(self.mode)

        except Exception as e:
            self.error_occurred.emit(f"Unexpected Worker Error ({self.mode}): {str(e)}")
        finally:
            self._cleanup()
            self.running = False

    def _sanitize_path_component(self, name: str) -> str:
        """Sanitize a string for safe use in file paths.

        Removes path traversal characters and special characters that could
        cause security issues or file system problems.
        """
        # Remove path traversal sequences
        sanitized = re.sub(r'\.\.+', '', name)
        sanitized = re.sub(r'[/\\]', '', sanitized)
        # Replace non-alphanumeric characters with underscores
        sanitized = ''.join(c if c.isalnum() or c in '-_' else '_' for c in sanitized)
        # Remove leading/trailing underscores and collapse multiple underscores
        sanitized = re.sub(r'_+', '_', sanitized).strip('_')
        # Ensure non-empty result
        return sanitized if sanitized else 'unnamed'

    def _create_base_path(self, source_value_str: str) -> Path:
        """Create a safe base path for measurement data (without extension).

        Sanitizes username and sample name to prevent path traversal attacks
        and ensure cross-platform compatibility.

        Returns:
            Path object without extension (DualExporter adds .json and .csv)
        """
        base_dir = Path(self.settings['file']['data_directory'])
        base_dir.mkdir(parents=True, exist_ok=True)

        # Sanitize username to prevent path traversal (e.g., "../" attacks)
        sanitized_username = self._sanitize_path_component(self.username)
        user_dir = base_dir / sanitized_username
        user_dir.mkdir(exist_ok=True)

        timestamp = int(time.time())
        sanitized_name = self._sanitize_path_component(self.sample_name)

        mode_tags = {
            'resistance': 'R',
            'source_v': 'VSRC',
            'source_i': 'ISRC',
            'four_point': '4PP'
        }
        mode_tag = mode_tags.get(self.mode, 'DATA')
        base_name = f"{timestamp}_{sanitized_name}_{mode_tag}_{source_value_str}"
        return user_dir / base_name

    def mark_event(self, name: str = "MARK") -> None:
        self.event_marker = name

    def pause_measurement(self) -> None:
        if self.running:
            self.paused = True
            self.status_update.emit(f"Measurement ({self.mode}) paused")

    def resume_measurement(self) -> None:
        if self.running:
            self.paused = False
            self.status_update.emit(f"Measurement ({self.mode}) resumed")

    def stop_measurement(self) -> None:
        self.status_update.emit(f"Stopping measurement ({self.mode})...")
        self.running = False

    def _cleanup(self) -> None:
        # Re-enable system sleep
        self._sleep_inhibitor.uninhibit()

        if self.keithley:
            try:
                self.keithley.write(":OUTP OFF")
                self.keithley.close()
                self.status_update.emit("Instrument disconnected.")
            except Exception as e:
                self.status_update.emit(f"Warning: Error during instrument cleanup: {str(e)}")
            finally:
                self.keithley = None
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
                self.status_update.emit(f"Warning: {error}")
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
        i_mag = abs(self._fpp_source_current)
        settling = self._fpp_delta_settling

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


class _VdpAborted(Exception):
    """Internal: worker was stopped via stop_measurement()."""


def _sanitize_for_path(name: str) -> str:
    """Path-safe name; mirrors MeasurementWorker._sanitize_path_component."""
    sanitized = re.sub(r'\.\.+', '', name)
    sanitized = re.sub(r'[/\\]', '', sanitized)
    sanitized = ''.join(c if c.isalnum() or c in '-_' else '_' for c in sanitized)
    sanitized = re.sub(r'_+', '_', sanitized).strip('_')
    return sanitized if sanitized else 'unnamed'


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

    geometry_ready = pyqtSignal(int, dict)
    geometry_complete = pyqtSignal(int, dict)
    vdp_complete = pyqtSignal(dict)
    status_update = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    compliance_hit = pyqtSignal(str)

    MODE = 'vdp'

    def __init__(self, sample_name, username, settings, parent=None):
        super().__init__(parent)
        self.sample_name = sample_name
        self.username = username
        self.settings = settings
        self._state_lock = threading.Lock()
        self._running = False
        self._proceed_event = threading.Event()
        self._voltages: Dict[str, float] = {}
        self.keithley = None
        self.exporter: Optional[DualExporter] = None
        self._instrument_idn = ""
        self._sleep_inhibitor = SleepInhibitor()
        self._start_time = 0.0
        self._i_mag = 0.0
        self.filename = ""

    @property
    def running(self) -> bool:
        with self._state_lock:
            return self._running

    @running.setter
    def running(self, value: bool) -> None:
        with self._state_lock:
            self._running = value

    def proceed(self) -> None:
        """UI slot: user has reconnected leads; take this geometry's reading."""
        self._proceed_event.set()

    def stop_measurement(self) -> None:
        self.status_update.emit("Stopping vdP measurement...")
        self.running = False
        # Unblock any wait_for_user pause.
        self._proceed_event.set()

    def run(self) -> None:
        self.running = True
        try:
            self._connect_and_configure()
            self._run_geometries()
            self._compute_and_emit_result()
        except _VdpAborted:
            self.status_update.emit("vdP measurement aborted by user")
        except Exception as e:
            logger.exception("vdP measurement failed")
            self.error_occurred.emit(f"vdP error: {e}")
        finally:
            self.running = False
            self._cleanup()

    def _connect_and_configure(self) -> None:
        # Lazy imports to avoid a Qt-load-time cost when vdP isn't used.
        from .calculations_vdp import f76_geometries  # noqa: F401  (touched in _run_geometries)

        measurement = self.settings['measurement']
        gpib = measurement['gpib_address']
        self.status_update.emit(f"Connecting to instrument at {gpib}...")
        self.keithley = Keithley2400(gpib).connect()
        self._instrument_idn = self.keithley.query("*IDN?").strip()
        self.status_update.emit(f"Connected to: {self._instrument_idn}")

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

        # Output data file
        base_dir = Path(self.settings['file']['data_directory'])
        base_dir.mkdir(parents=True, exist_ok=True)
        user_dir = base_dir / _sanitize_for_path(self.username)
        user_dir.mkdir(exist_ok=True)
        timestamp = int(time.time())
        sample = _sanitize_for_path(self.sample_name)
        base_name = f"{timestamp}_{sample}_vdP_{i_mag*1000:.2f}mA"
        base_path = user_dir / base_name
        self.filename = str(base_path.with_suffix('.json'))

        columns, units = get_column_config(self.MODE, measurement)
        export_metadata = build_metadata(
            user=self.username,
            sample_name=self.sample_name,
            mode=self.MODE,
            settings=self.settings,
            instrument_idn=self._instrument_idn,
            start_time=datetime.fromtimestamp(time.time()),
        )
        self.exporter = DualExporter(
            base_path=base_path, metadata=export_metadata,
            columns=columns, units=units,
        )
        self.status_update.emit(f"Data files: {base_path.name}.json/.csv")

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

            self._proceed_event.clear()
            self.geometry_ready.emit(idx, {
                'name': geom.name,
                'source_high': geom.source_high,
                'source_low': geom.source_low,
                'sense_high': geom.sense_high,
                'sense_low': geom.sense_low,
                'label_pos': geom.label_pos,
                'label_neg': geom.label_neg,
                'group': geom.group,
            })
            self.status_update.emit(
                f"{geom.name}: connect Force HI->C{geom.source_high}, "
                f"Force LO->C{geom.source_low}, "
                f"Sense HI->C{geom.sense_high}, "
                f"Sense LO->C{geom.sense_low}; press Measure."
            )
            self._proceed_event.wait()
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
                self.compliance_hit.emit("Voltage")

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

            self.geometry_complete.emit(idx, {
                'name': geom.name,
                'label_pos': geom.label_pos, 'v_pos': v_pos,
                'label_neg': geom.label_neg, 'v_neg': v_neg,
                'current_a': self._i_mag,
                'group': geom.group,
            })

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
        }
        self.vdp_complete.emit(result_dict)
        self.status_update.emit(
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
                self.status_update.emit("Instrument disconnected.")
            except Exception as e:
                self.status_update.emit(f"Warning: cleanup error: {e}")
            finally:
                self.keithley = None
        if self.exporter:
            try:
                # finalize() is idempotent on already-finalized exporters.
                self.exporter.finalize()
            except Exception:
                pass
            self.exporter = None

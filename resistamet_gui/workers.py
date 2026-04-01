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
                    # Re-apply cable null if previously set
                    cable_null = float(measurement_settings.get('res_cable_null', 0.0))
                    if cable_null != 0.0:
                        self.keithley.write(f":SENS:RES:REL {cable_null}")
                        self.keithley.write(":SENS:RES:REL:STAT ON")
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

                    # Delta mode settings
                    self._fpp_delta_mode = bool(measurement_settings.get('fpp_delta_mode', False))
                    self._fpp_delta_settling = float(measurement_settings.get('fpp_delta_settling', 0.1))
                    self._fpp_source_current = source_current

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

                # Hardware averaging filter
                if measurement_settings.get('filter_enabled', False):
                    ftype = str(measurement_settings.get('filter_type', 'repeat')).upper()[:3]
                    fcount = int(measurement_settings.get('filter_count', 10))
                    sense_func = {'resistance': 'RES', 'source_v': 'CURR', 'source_i': 'VOLT', 'four_point': 'VOLT'}.get(self.mode, 'VOLT')
                    self.keithley.write(f":SENS:{sense_func}:AVER:TCON {ftype}")
                    self.keithley.write(f":SENS:{sense_func}:AVER:COUN {fcount}")
                    self.keithley.write(f":SENS:{sense_func}:AVER ON")
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
                columns, units = get_column_config(self.mode)

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
                        # Use calculations module for 4PP
                        from .calculations import calculate_four_point_probe
                        result = calculate_four_point_probe(
                            voltage=v,
                            current=i,
                            spacing_cm=float(measurement_settings.get('fpp_spacing_cm') or 0.1016),
                            thickness_um=float(measurement_settings.get('fpp_thickness_um') or 0.0),
                            k_factor=float(measurement_settings.get('fpp_k_factor') or 4.532),
                            alpha=float(measurement_settings.get('fpp_alpha') or 1.0),
                            model=str(measurement_settings.get('fpp_model') or 'thin_film')
                        )
                        row_data = [
                            elapsed_time, v, i,
                            result.ratio, result.sheet_resistance,
                            result.resistivity, result.conductivity,
                            compliance_status, event_marker
                        ]
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
        # Compliance: OR of both readings
        stat_combined = stat_plus | stat_minus

        # Return synthetic reading string matching VOLT,CURR,STAT format
        return f"{v_delta},{i_mag},{stat_combined}"

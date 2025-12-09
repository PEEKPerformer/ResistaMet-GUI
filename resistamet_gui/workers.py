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
    KEITHLEY_COMPLIANCE_MAGIC_NUMBER,
    COMPLIANCE_THRESHOLD_FACTOR,
)
from .data_export import DualExporter, get_column_config, build_metadata
from .instrument import Keithley2400


class MeasurementWorker(QThread):
    """Worker thread for running measurements in different modes."""
    data_point = pyqtSignal(float, dict, str, str)  # timestamp, data dict, compliance, event
    status_update = pyqtSignal(str)
    measurement_complete = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    compliance_hit = pyqtSignal(str)  # 'Voltage' or 'Current'

    def __init__(self, mode, sample_name, username, settings, parent=None):
        super().__init__(parent)
        if mode not in ['resistance', 'source_v', 'source_i', 'four_point']:
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
                self.keithley.write(":SYST:AZER:STAT ON")
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

                    # Delegate setup to instrument helper
                    try:
                        from .instrument import Keithley2400 as _K
                    except Exception:
                        pass
                    # Use direct writes if helper import fails
                    try:
                        self.keithley.write(":SYST:RSEN ON" if measurement_type == "4-wire" else ":SYST:RSEN OFF")
                        self.keithley.write(":SENS:FUNC 'RES'")
                        self.keithley.write(":SOUR:FUNC CURR")
                        self.keithley.write(f":SOUR:CURR:MODE FIX")
                        self.keithley.write(f":SOUR:CURR:RANG {abs(test_current)}")
                        self.keithley.write(f":SOUR:CURR {test_current}")
                        self.keithley.write(f":SENS:VOLT:PROT {voltage_compliance}")
                        self.keithley.write(":SENS:RES:MODE AUTO" if auto_range else ":SENS:RES:MODE MAN")
                        if not auto_range:
                            max_r = voltage_compliance / abs(test_current) if abs(test_current) > 0 else 210e6
                            self.keithley.write(f":SENS:RES:RANG {max_r}")
                        self.keithley.write(f":SENS:RES:NPLC {nplc}")
                        self.keithley.write(":FORM:ELEM RES")
                    except Exception as _:
                        raise

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
                    self.keithley.write(f":SOUR:VOLT:MODE FIX")
                    self.keithley.write(f":SOUR:VOLT:RANG {abs(source_voltage)}")
                    self.keithley.write(f":SOUR:VOLT {source_voltage}")
                    self.keithley.write(f":SENS:CURR:PROT {current_compliance}")
                    self.keithley.write(":SENS:CURR:RANG:AUTO ON" if auto_range_curr else ":SENS:CURR:RANG:AUTO OFF")
                    if not auto_range_curr:
                        self.keithley.write(f":SENS:CURR:RANG {current_compliance}")
                    self.keithley.write(f":SENS:CURR:NPLC {nplc}")
                    self.keithley.write(":FORM:ELEM CURR,VOLT")
                    self.keithley.write(":TRIG:COUN 1")
                    self.keithley.write(":INIT:CONT ON")

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

                    # Use remote sense (Kelvin) so inner probes connect to sense
                    self.keithley.write(":SYST:RSEN ON")
                    self.keithley.write(":SENS:FUNC 'VOLT:DC'")
                    self.keithley.write(":SOUR:FUNC CURR")
                    self.keithley.write(f":SOUR:CURR:MODE FIX")
                    self.keithley.write(f":SOUR:CURR:RANG {abs(source_current)}")
                    self.keithley.write(f":SOUR:CURR {source_current}")
                    self.keithley.write(f":SENS:VOLT:PROT {voltage_compliance}")
                    self.keithley.write(":SENS:VOLT:RANG:AUTO ON" if auto_range_volt else ":SENS:VOLT:RANG:AUTO OFF")
                    if not auto_range_volt:
                        self.keithley.write(f":SENS:VOLT:RANG {voltage_compliance}")
                    self.keithley.write(f":SENS:VOLT:NPLC {nplc}")
                    self.keithley.write(":FORM:ELEM VOLT,CURR")
                    self.keithley.write(":TRIG:COUN 1")
                    self.keithley.write(":INIT:CONT ON")

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
                    self.keithley.write(f":SOUR:CURR:MODE FIX")
                    self.keithley.write(f":SOUR:CURR:RANG {abs(source_current)}")
                    self.keithley.write(f":SOUR:CURR {source_current}")
                    self.keithley.write(f":SENS:VOLT:PROT {voltage_compliance}")
                    self.keithley.write(":SENS:VOLT:RANG:AUTO ON" if auto_range_volt else ":SENS:VOLT:RANG:AUTO OFF")
                    if not auto_range_volt:
                        self.keithley.write(f":SENS:VOLT:RANG {voltage_compliance}")
                    self.keithley.write(f":SENS:VOLT:NPLC {nplc}")
                    self.keithley.write(":FORM:ELEM VOLT,CURR")
                    self.keithley.write(":TRIG:COUN 1")
                    self.keithley.write(":INIT:CONT ON")

                    metadata = {
                        'Mode': 'Four-Point Probe',
                        'Source Current (A)': source_current,
                        'Voltage Compliance (V)': voltage_compliance,
                        'Spacing s (cm)': measurement_settings.get('fpp_spacing_cm'),
                        'Thickness t (µm)': measurement_settings.get('fpp_thickness_um'),
                        'Alpha': measurement_settings.get('fpp_alpha'),
                        'K Factor': measurement_settings.get('fpp_k_factor'),
                        'Model': measurement_settings.get('fpp_model'),
                    }
                    csv_headers = ['Timestamp (Unix)', 'Elapsed Time (s)', 'Voltage (V)', 'Current (A)', 'V/I (Ohms)', 'Sheet Rs (Ohms/sq)', 'Resistivity (Ohm*cm)', 'Conductivity (S/cm)', 'Compliance Status', 'Event']
                    source_value_str = f"{source_current*1000:.2f}mA"

                self.keithley.write(":TRIG:DEL 0")
                self.keithley.write(":SOUR:DEL 0")
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

            # Loop
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

            while self.running:
                if self.paused:
                    time.sleep(0.1)
                    continue
                now = time.time()
                if now - last_measurement_time >= sample_interval:
                    try:
                        reading_str = self.keithley.query(":READ?").strip()
                        last_measurement_time = now
                    except pyvisa.errors.VisaIOError as e:
                        self.error_occurred.emit(f"VISA Read Error: {str(e)}. Stopping.")
                        break
                    except Exception as e:
                        self.error_occurred.emit(f"Unexpected Read Error: {str(e)}. Stopping.")
                        break

                    elapsed_time = now - self.start_time
                    compliance_status = 'OK'
                    compliance_type = None
                    data_dict: Dict[str, float] = {}

                    if self.mode == 'resistance':
                        try:
                            value = float(reading_str)
                        except Exception:
                            value = float('nan')
                        compliance_type = 'Voltage'
                        if np.isfinite(value) and value > KEITHLEY_COMPLIANCE_MAGIC_NUMBER * 0.9:
                            compliance_status = 'V_COMP'
                        if not np.isfinite(value) or value < 0:
                            value = float('nan')
                            self.status_update.emit(f"Invalid value detected ({reading_str})")
                        data_dict = {'resistance': value}
                    elif self.mode == 'source_v':
                        parts = [p for p in reading_str.split(',') if p.strip()]
                        try:
                            current = float(parts[0])
                            voltage = float(parts[1]) if len(parts) > 1 else float('nan')
                        except Exception:
                            current = float('nan'); voltage = float('nan')
                        comp_limit_i = measurement_settings.get('vsource_current_compliance')
                        compliance_type = 'Current'
                        if np.isfinite(current) and abs(current) >= comp_limit_i * COMPLIANCE_THRESHOLD_FACTOR:
                            compliance_status = 'I_COMP'
                        if np.isfinite(current) and abs(current) > KEITHLEY_COMPLIANCE_MAGIC_NUMBER * 0.9:
                            compliance_status = 'I_COMP'
                        data_dict = {'current': current, 'voltage': voltage}
                    elif self.mode == 'source_i':
                        parts = [p for p in reading_str.split(',') if p.strip()]
                        try:
                            voltage = float(parts[0])
                            current = float(parts[1]) if len(parts) > 1 else float('nan')
                        except Exception:
                            voltage = float('nan'); current = float('nan')
                        comp_limit_v = measurement_settings.get('isource_voltage_compliance')
                        compliance_type = 'Voltage'
                        if np.isfinite(voltage) and abs(voltage) >= comp_limit_v * COMPLIANCE_THRESHOLD_FACTOR:
                            compliance_status = 'V_COMP'
                        if np.isfinite(voltage) and abs(voltage) > KEITHLEY_COMPLIANCE_MAGIC_NUMBER * 0.9:
                            compliance_status = 'V_COMP'
                        data_dict = {'voltage': voltage, 'current': current}
                    elif self.mode == 'four_point':
                        # Configured like source_i (VOLT,CURR)
                        parts = [p for p in reading_str.split(',') if p.strip()]
                        try:
                            voltage = float(parts[0])
                            current = float(parts[1]) if len(parts) > 1 else float('nan')
                        except Exception:
                            voltage = float('nan'); current = float('nan')
                        comp_limit_v = measurement_settings.get('fpp_voltage_compliance')
                        compliance_type = 'Voltage'
                        if np.isfinite(voltage) and abs(voltage) >= comp_limit_v * COMPLIANCE_THRESHOLD_FACTOR:
                            compliance_status = 'V_COMP'
                        if np.isfinite(voltage) and abs(voltage) > KEITHLEY_COMPLIANCE_MAGIC_NUMBER * 0.9:
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

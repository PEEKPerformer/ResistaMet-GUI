import csv
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict

import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal

from .constants import (
    __version__,
    __original_version__,
    __author__,
    KEITHLEY_COMPLIANCE_MAGIC_NUMBER,
    COMPLIANCE_THRESHOLD_FACTOR,
)
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
        self.running = False
        self.paused = False
        self.event_marker = ""
        self.keithley = None
        self.csvfile = None
        self.writer = None
        self.start_time = 0
        self.filename = ""

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
                idn = self.keithley.query("*IDN?").strip()
                self.status_update.emit(f"Connected to: {idn}")
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

            # File setup
            try:
                self.filename = self._create_filename(source_value_str)
                self.csvfile = open(self.filename, 'w', newline='')
                self.writer = csv.writer(self.csvfile)
                file_ready = True
            except Exception as e:
                self.error_occurred.emit(f"Error creating output file: {str(e)}")
                return

            # Metadata
            self.start_time = time.time()
            start_unix_time = int(self.start_time)
            full_metadata = {
                'User': self.username,
                'Sample Name': self.sample_name,
                'Start Time (Unix)': start_unix_time,
                'Start Time (Human Readable)': datetime.fromtimestamp(start_unix_time).isoformat(),
                'Software Version': __version__,
                'Original Script Version': __original_version__,
                'Author': __author__,
                'GPIB Address': gpib_address,
                'Sampling Rate (Hz)': sampling_rate,
                'NPLC': nplc,
                'Settling Time (s)': settling_time,
                **metadata
            }
            self._write_metadata(full_metadata)
            self.writer.writerow(csv_headers)
            self.csvfile.flush()

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

                    event_marker = ""
                    if self.event_marker:
                        event_marker = self.event_marker
                        self.event_marker = ""
                        self.status_update.emit(f"⭐ Event marked at {elapsed_time:.3f}s ⭐")

                    now_unix = int(now)
                    if self.mode == 'resistance':
                        r = data_dict.get('resistance', float('nan'))
                        row_data = [now_unix, f"{elapsed_time:.3f}", f"{r:.6e}" if np.isfinite(r) else "NaN", compliance_status, event_marker]
                    else:
                        v = data_dict.get('voltage', float('nan'))
                        i = data_dict.get('current', float('nan'))
                        r = (v / i) if (np.isfinite(v) and np.isfinite(i) and i != 0) else float('nan')
                        if self.mode == 'four_point':
                            # compute derived per 4-pt probe
                            s = float(measurement_settings.get('fpp_spacing_cm') or 0.0)
                            tum = float(measurement_settings.get('fpp_thickness_um') or 0.0)
                            tcm = tum * 1e-4
                            alpha = float(measurement_settings.get('fpp_alpha') or 1.0)
                            kfac = float(measurement_settings.get('fpp_k_factor') or 4.532)
                            model = str(measurement_settings.get('fpp_model') or 'thin_film')
                            ratio = r
                            # Apply alpha only in thin_film for Rs if provided
                            k_eff = kfac * (alpha if (model == 'thin_film' and alpha and alpha != 1.0) else 1.0)
                            Rs = k_eff * ratio if np.isfinite(ratio) else float('nan')
                            if model == 'semi_infinite':
                                rho = 2*np.pi*s*ratio if np.isfinite(ratio) else float('nan')
                            elif model in ('thin_film','finite_thin'):
                                rho = (kfac * (alpha if (model == 'thin_film' and alpha and alpha != 1.0) else 1.0)) * tcm * ratio if np.isfinite(ratio) else float('nan')
                            else:
                                rho = alpha * 2*np.pi*s*ratio if np.isfinite(ratio) else float('nan')
                            sigma = (1.0/rho) if (np.isfinite(rho) and rho != 0) else float('nan')
                            row_data = [
                                now_unix,
                                f"{elapsed_time:.3f}",
                                f"{v:.6e}" if np.isfinite(v) else "NaN",
                                f"{i:.6e}" if np.isfinite(i) else "NaN",
                                f"{ratio:.6e}" if np.isfinite(ratio) else "NaN",
                                f"{Rs:.6e}" if np.isfinite(Rs) else "NaN",
                                f"{rho:.6e}" if np.isfinite(rho) else "NaN",
                                f"{sigma:.6e}" if np.isfinite(sigma) else "NaN",
                                compliance_status,
                                event_marker,
                            ]
                        else:
                            row_data = [
                                now_unix,
                                f"{elapsed_time:.3f}",
                                f"{v:.6e}" if np.isfinite(v) else "NaN",
                                f"{i:.6e}" if np.isfinite(i) else "NaN",
                                f"{r:.6e}" if np.isfinite(r) else "NaN",
                                compliance_status,
                                event_marker,
                            ]
                    try:
                        self.writer.writerow(row_data)
                    except Exception as e:
                        self.error_occurred.emit(f"Error writing to CSV: {str(e)}")
                        self.status_update.emit("Warning: Failed to write data point to CSV.")

                    self.data_point.emit(now, data_dict, compliance_status, event_marker)

                    if now - last_save >= auto_save_interval:
                        try:
                            self.csvfile.flush()
                            os.fsync(self.csvfile.fileno())
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
            if file_ready and self.filename:
                try:
                    self.writer.writerow([])
                    end_unix_time = int(time.time())
                    full_metadata['End Time (Unix)'] = end_unix_time
                    full_metadata['End Time (Human Readable)'] = datetime.fromtimestamp(end_unix_time).isoformat()
                    self._write_metadata(full_metadata)
                except Exception as e:
                    self.status_update.emit(f"Warning: Error writing final metadata - {str(e)}")
                final_message = f"Measurement ({self.mode}) completed! Data saved to: {self.filename}"
            self.status_update.emit(final_message)
            self.measurement_complete.emit(self.mode)

        except Exception as e:
            self.error_occurred.emit(f"Unexpected Worker Error ({self.mode}): {str(e)}")
        finally:
            self._cleanup()
            self.running = False

    def _create_filename(self, source_value_str: str) -> str:
        base_dir = Path(self.settings['file']['data_directory'])
        base_dir.mkdir(parents=True, exist_ok=True)
        user_dir = base_dir / self.username
        user_dir.mkdir(exist_ok=True)
        timestamp = int(time.time())
        sanitized_name = ''.join(c if c.isalnum() else '_' for c in self.sample_name)
        mode_tag = "R" if self.mode == 'resistance' else ("VSRC" if self.mode == 'source_v' else "ISRC")
        filename = f"{timestamp}_{sanitized_name}_{mode_tag}_{source_value_str}.csv"
        return user_dir / filename

    def _write_metadata(self, params: Dict) -> None:
        if not self.writer:
            return
        try:
            self.writer.writerow(['### METADATA START ###'])
            for key, value in params.items():
                self.writer.writerow([f'# {key}', value])
            self.writer.writerow(['### METADATA END ###'])
            self.writer.writerow([])
        except Exception as e:
            self.status_update.emit(f"Warning: Failed to write metadata - {str(e)}")

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
        if self.csvfile:
            try:
                self.csvfile.flush()
                self.csvfile.close()
            except Exception as e:
                self.status_update.emit(f"Warning: Error closing CSV file: {str(e)}")
            finally:
                self.csvfile = None
        self.writer = None

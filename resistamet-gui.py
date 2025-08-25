#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ResistaMet GUI: Resistance Measurement System with Graphical User Interface

This implementation adds a PyQt-based GUI with tabbed functionality for
Resistance Measurement, Voltage Source, and Current Source modes.

Version: 1.1.0
"""

import sys
import os
import time
import csv
import json
import signal
import numpy as np
import threading
from datetime import datetime
from pathlib import Path
from collections import deque
from typing import List, Dict, Tuple, Optional, Union, Any

import pyvisa
# import matplotlib # Ensure matplotlib is imported if needed directly
# matplotlib.use('Qt5Agg') # Set backend if necessary, usually handled by FigureCanvasQTAgg
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QLineEdit, QTabWidget,
    QGroupBox, QFormLayout, QCheckBox, QSpinBox, QDoubleSpinBox,
    QMessageBox, QFileDialog, QStatusBar, QAction, QMenu,
    QDialog, QRadioButton, QButtonGroup, QFrame, QSplitter,
    QTableWidget, QTableWidgetItem, QHeaderView, QTextEdit,
    QSizePolicy, QShortcut
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread, QObject # Added QObject
from PyQt5.QtGui import QIcon, QFont, QColor, QPalette

# Script version and metadata
__version__ = "1.1.0"  # GUI version with tabs
__original_version__ = "0.9.2"  # Original script version
__author__ = "Brenden Ferland"

# Configuration file
CONFIG_FILE = "config.json"

# Default settings (updated for new modes)
DEFAULT_SETTINGS = {
    "measurement": {
        # Resistance Mode (Source I, Measure R)
        "res_test_current": 1.0e-3,          # Test current in Amperes for R mode
        "res_voltage_compliance": 5.0,       # Voltage compliance in Volts for R mode
        "res_measurement_type": "2-wire",    # Measurement type (2-wire or 4-wire) for R mode
        "res_auto_range": True,              # Enable resistance auto-ranging for R mode
        # Voltage Source Mode (Source V, Measure I)
        "vsource_voltage": 1.0,              # Source voltage in Volts
        "vsource_current_compliance": 0.1,   # Current compliance in Amperes for V source mode
        "vsource_current_range_auto": True,  # Auto range for current measurement
        "vsource_duration_hours": 1.0,       # Duration to apply voltage in hours
        # Current Source Mode (Source I, Measure V) - Can reuse some R settings
        "isource_current": 1.0e-3,           # Source current in Amperes
        "isource_voltage_compliance": 5.0,   # Voltage compliance in Volts for I source mode
        "isource_voltage_range_auto": True,  # Auto range for voltage measurement
        "isource_duration_hours": 1.0,       # Duration to apply current in hours
        # General
        "sampling_rate": 10.0,               # Sampling rate in Hz (shared for now)
        "nplc": 1,                           # Number of power line cycles (shared)
        "settling_time": 0.2,                # Settling time in seconds (shared)
        "gpib_address": "GPIB0::24::INSTR"   # GPIB address of the instrument (CHECK YOUR ADDRESS)
    },
    "display": {
        "enable_plot": True,
        "plot_update_interval": 200,         # Plot update interval in milliseconds
        "plot_color_r": "red",               # Plot line color for Resistance
        "plot_color_v": "blue",              # Plot line color for Voltage Source (Current)
        "plot_color_i": "green",             # Plot line color for Current Source (Voltage)
        "plot_figsize": [8, 5],              # Plot figure size [width, height]
        "buffer_size": 1000                  # Data buffer size (points, 0 or None = unlimited)
    },
    "file": {
        "auto_save_interval": 60,            # Auto-save interval in seconds
        "data_directory": "measurement_data" # Base directory for data storage
    },
    "users": [],
    "last_user": None
}

# --- Constants for Keithley Compliance (Example for 2400, check your manual!) ---
# Keithley 2400 might return 9.91e+37 or +/- 1.05 * compliance limit
KEITHLEY_COMPLIANCE_MAGIC_NUMBER = 9.9e37 # Or None if using status bits
COMPLIANCE_THRESHOLD_FACTOR = 1.0 # Check reading against (compliance * factor)

# ----- Enhanced Data Buffer (Multi-channel) -----
class EnhancedDataBuffer:
    """Stores timestamps, resistance, voltage, current, events, and compliance per point.

    Provides per-channel statistics and plotting data selection.
    """
    def __init__(self, size: Optional[int] = None):
        self._max_len = size if size is not None and size > 0 else None
        self.reset()

    def reset(self):
        self.timestamps = deque(maxlen=self._max_len)
        self.resistance = deque(maxlen=self._max_len)
        self.voltage = deque(maxlen=self._max_len)
        self.current = deque(maxlen=self._max_len)
        self.events = deque(maxlen=self._max_len)
        self.compliance_status = deque(maxlen=self._max_len)  # 'OK', 'V_COMP', 'I_COMP'

        # Stats per channel
        self.stats = {
            'resistance': {'min': float('inf'), 'max': float('-inf'), 'avg': 0.0, 'count': 0},
            'voltage': {'min': float('inf'), 'max': float('-inf'), 'avg': 0.0, 'count': 0},
            'current': {'min': float('inf'), 'max': float('-inf'), 'avg': 0.0, 'count': 0},
        }
        self.last_compliance_hit = None

    @property
    def size(self):
        return self._max_len

    def _update_stat(self, key: str, value: float):
        if np.isfinite(value):
            st = self.stats[key]
            st['count'] += 1
            if value < st['min']:
                st['min'] = value
            if value > st['max']:
                st['max'] = value
            # Running average
            if st['count'] == 1:
                st['avg'] = value
            else:
                st['avg'] += (value - st['avg']) / st['count']

    def add_resistance(self, timestamp: float, resistance: float, compliance: str = 'OK', event: str = "") -> None:
        self.timestamps.append(timestamp)
        self.resistance.append(resistance if np.isfinite(resistance) and resistance >= 0 else float('nan'))
        self.voltage.append(None)
        self.current.append(None)
        self.events.append(event)
        self.compliance_status.append(compliance)
        if compliance != 'OK':
            self.last_compliance_hit = compliance
        if np.isfinite(resistance) and resistance >= 0:
            self._update_stat('resistance', resistance)

    def add_voltage_current(self, timestamp: float, voltage: float, current: float, compliance: str = 'OK', event: str = "") -> None:
        self.timestamps.append(timestamp)
        v = voltage if np.isfinite(voltage) else float('nan')
        i = current if np.isfinite(current) else float('nan')
        self.voltage.append(v)
        self.current.append(i)
        # Calculate resistance if possible
        r = (v / i) if (np.isfinite(v) and np.isfinite(i) and i != 0) else float('nan')
        self.resistance.append(r)
        self.events.append(event)
        self.compliance_status.append(compliance)
        if compliance != 'OK':
            self.last_compliance_hit = compliance
        if np.isfinite(v):
            self._update_stat('voltage', v)
        if np.isfinite(i):
            self._update_stat('current', i)
        if np.isfinite(r) and r >= 0:
            self._update_stat('resistance', r)

    def get_data_for_plot(self, data_type: str = 'resistance') -> Tuple[List[float], List[float], List[str]]:
        ts = list(self.timestamps)
        if not ts:
            return [], [], []
        # elapsed times relative to first timestamp
        elapsed = [t - ts[0] for t in ts]
        if data_type == 'resistance':
            values = [x if x is not None else float('nan') for x in list(self.resistance)]
        elif data_type == 'voltage':
            values = [x if x is not None else float('nan') for x in list(self.voltage)]
        elif data_type == 'current':
            values = [x if x is not None else float('nan') for x in list(self.current)]
        else:
            values = []
        return elapsed, values, list(self.compliance_status)

    def get_statistics(self, data_type: str = 'resistance') -> Dict[str, float]:
        st = self.stats.get(data_type, None)
        if not st:
            return {'min': float('inf'), 'max': float('-inf'), 'avg': 0.0}
        # Return compatible structure for canvas
        return {'min': st['min'], 'max': st['max'], 'avg': st['avg']}

    def clear(self) -> None:
        self.reset()

# ----- Config Manager Class (Mostly Unchanged) -----
class ConfigManager:
    def __init__(self, config_file: str = CONFIG_FILE):
        self.config_file = config_file
        self.config = self.load_config()

    def load_config(self) -> Dict:
        """Load configuration from file or create with defaults if not exists."""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    loaded_config = json.load(f)

                # Merge with defaults to ensure all keys exist
                config = dict(DEFAULT_SETTINGS)
                for section, defaults in DEFAULT_SETTINGS.items():
                    if section in loaded_config:
                        if isinstance(defaults, dict):
                           config[section].update(loaded_config[section])
                        else:
                           config[section] = loaded_config[section] # For lists like users
                    # Keep default section if not in loaded config

                # Ensure nested defaults are present
                for section, defaults in DEFAULT_SETTINGS.items():
                    if isinstance(defaults, dict):
                         for key, value in defaults.items():
                              if key not in config[section]:
                                   config[section][key] = value

                return config
            except Exception as e:
                print(f"Error loading configuration file '{self.config_file}': {str(e)}. Using defaults.")
                return dict(DEFAULT_SETTINGS) # Return a deep copy
        else:
            print(f"Configuration file '{self.config_file}' not found. Creating with defaults.")
            new_config = dict(DEFAULT_SETTINGS) # Return a deep copy
            self.config = new_config # Set self.config before saving
            self.save_config()
            return new_config

    def save_config(self) -> None:
        """Save configuration to file."""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=4, sort_keys=True)
        except Exception as e:
            print(f"Error saving configuration: {str(e)}")

    def get_user_settings(self, username: str) -> Dict:
        user_settings = {}
        for section in ['measurement', 'display', 'file']:
            user_settings[section] = dict(self.config[section]) # Start with global

        if 'user_settings' in self.config and username in self.config['user_settings']:
            user_specific = self.config['user_settings'][username]
            for section, settings in user_specific.items():
                if section in user_settings and isinstance(user_settings[section], dict):
                    user_settings[section].update(settings) # Override with user-specific
        return user_settings

    def update_user_settings(self, username: str, settings: Dict) -> None:
        if 'user_settings' not in self.config:
            self.config['user_settings'] = {}
        if username not in self.config['user_settings']:
            self.config['user_settings'][username] = {}

        for section, section_settings in settings.items():
            if section in ['measurement', 'display', 'file']:
                if section not in self.config['user_settings'][username]:
                    self.config['user_settings'][username][section] = {}
                # Only save differences from global defaults? Or save all? Save all is simpler.
                self.config['user_settings'][username][section] = dict(section_settings) # Store a copy

        self.save_config()

    def update_global_settings(self, settings: Dict) -> None:
        for section, section_settings in settings.items():
            if section in ['measurement', 'display', 'file'] and isinstance(self.config[section], dict):
                self.config[section].update(section_settings)
        self.save_config()

    def get_users(self) -> List[str]:
        return self.config.get('users', [])

    def get_last_user(self) -> Optional[str]:
        return self.config.get('last_user')

    def add_user(self, username: str) -> None:
        username = username.strip()
        if username and username not in self.config.get('users', []):
            if 'users' not in self.config: self.config['users'] = []
            self.config['users'].append(username)
            self.config['users'].sort() # Keep sorted
            self.save_config()

    def set_last_user(self, username: str) -> None:
        if username in self.config.get('users', []):
            self.config['last_user'] = username
            self.save_config()

# ----- Measurement Worker Thread (Generalized) -----
class MeasurementWorker(QThread):
    """Worker thread for running measurements in different modes."""
    # Signal: timestamp, data_dict (may contain resistance/voltage/current), compliance_status, event_marker
    data_point = pyqtSignal(float, dict, str, str)
    status_update = pyqtSignal(str)
    measurement_complete = pyqtSignal(str) # Pass mode on complete
    error_occurred = pyqtSignal(str)
    compliance_hit = pyqtSignal(str) # 'Voltage' or 'Current'

    def __init__(self, mode, sample_name, username, settings, parent=None):
        super().__init__(parent)
        if mode not in ['resistance', 'source_v', 'source_i']:
            raise ValueError(f"Invalid measurement mode: {mode}")
        self.mode = mode
        self.sample_name = sample_name
        self.username = username
        self.settings = settings # Expects combined user/global settings
        self.running = False
        self.paused = False
        self.event_marker = ""
        self.keithley = None
        self.csvfile = None
        self.writer = None
        self.start_time = 0
        self.filename = "" # Store filename

    def run(self):
        self.running = True
        self.paused = False
        instrument_ready = False
        file_ready = False

        try:
            # Extract relevant settings based on mode
            measurement_settings = self.settings['measurement']
            file_settings = self.settings['file']
            display_settings = self.settings['display']

            sampling_rate = measurement_settings['sampling_rate']
            nplc = measurement_settings['nplc']
            settling_time = measurement_settings['settling_time']
            gpib_address = measurement_settings['gpib_address']
            auto_save_interval = file_settings['auto_save_interval']

            sample_interval = 1.0 / sampling_rate if sampling_rate > 0 else 0.1

            # --- Instrument Connection ---
            try:
                self.status_update.emit(f"Connecting to instrument at {gpib_address}...")
                rm = pyvisa.ResourceManager()
                resources = rm.list_resources()
                if not resources:
                    self.error_occurred.emit("No VISA instruments detected!")
                    return
                if gpib_address not in resources:
                    self.error_occurred.emit(f"Instrument at '{gpib_address}' not found. Available: {', '.join(resources)}")
                    return

                self.keithley = rm.open_resource(gpib_address)
                self.keithley.timeout = 5000 # ms
                # self.keithley.read_termination = '\n' # Check if needed
                # self.keithley.write_termination = '\n' # Check if needed

                idn = self.keithley.query("*IDN?").strip()
                self.status_update.emit(f"Connected to: {idn}")

                line_freq = 50.0 # Default
                try:
                    line_freq = float(self.keithley.query(":SYST:LFR?"))
                except Exception:
                     self.status_update.emit("Warning: Could not query line frequency. Assuming 50Hz.")

                self.keithley.write("*RST") # Reset instrument
                time.sleep(0.5) # Wait for reset
                self.keithley.write("*CLS") # Clear status registers
                self.keithley.write(":SYST:AZER:STAT ON") # Enable autozero

                instrument_ready = True
            except pyvisa.errors.VisaIOError as e:
                 self.error_occurred.emit(f"VISA Error connecting: {str(e)}")
                 return
            except Exception as e:
                self.error_occurred.emit(f"Error connecting to instrument: {str(e)}")
                return

            # --- Instrument Configuration based on Mode ---
            self.status_update.emit(f"Configuring instrument for {self.mode} mode...")
            metadata = {} # Store metadata for saving
            csv_headers = []
            source_value_str = "" # For filename/metadata

            try:
                if self.mode == 'resistance':
                    # --- Resistance Mode ---
                    test_current = measurement_settings['res_test_current']
                    voltage_compliance = measurement_settings['res_voltage_compliance']
                    measurement_type = measurement_settings['res_measurement_type']
                    auto_range = measurement_settings['res_auto_range']

                    if measurement_type == "4-wire":
                        self.keithley.write(":SYST:RSEN ON") # 4-wire
                    else:
                        self.keithley.write(":SYST:RSEN OFF") # 2-wire (default)

                    self.keithley.write(":SENS:FUNC 'RES'") # Sense Resistance
                    self.keithley.write(":SOUR:FUNC CURR") # Source Current
                    self.keithley.write(f":SOUR:CURR:MODE FIX") # Fixed source mode
                    self.keithley.write(f":SOUR:CURR:RANG {abs(test_current)}") # Set source range
                    self.keithley.write(f":SOUR:CURR {test_current}") # Set source level
                    self.keithley.write(f":SENS:VOLT:PROT {voltage_compliance}") # Voltage compliance

                    if auto_range:
                        self.keithley.write(":SENS:RES:MODE AUTO") # Auto range for resistance
                    else:
                        self.keithley.write(":SENS:RES:MODE MAN")
                        # Estimate range based on compliance V and test I
                        max_r = voltage_compliance / abs(test_current) if abs(test_current) > 0 else 210e6
                        self.keithley.write(f":SENS:RES:RANG {max_r}") # Manual range

                    self.keithley.write(f":SENS:RES:NPLC {nplc}")
                    self.keithley.write(":FORM:ELEM RES") # Read only resistance

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
                    # --- Voltage Source Mode ---
                    source_voltage = measurement_settings['vsource_voltage']
                    current_compliance = measurement_settings['vsource_current_compliance']
                    auto_range_curr = measurement_settings['vsource_current_range_auto']
                    duration_hours = max(0.0, float(measurement_settings.get('vsource_duration_hours', 0.0)))

                    self.keithley.write(":SYST:RSEN OFF") # Usually 2-wire for V source
                    self.keithley.write(":SENS:FUNC 'CURR:DC'") # Measure Current (voltage still available in elements)
                    self.keithley.write(":SOUR:FUNC VOLT") # Source Voltage
                    self.keithley.write(f":SOUR:VOLT:MODE FIX") # Fixed source mode
                    self.keithley.write(f":SOUR:VOLT:RANG {abs(source_voltage)}") # Set source range (or AUTO?)
                    self.keithley.write(f":SOUR:VOLT {source_voltage}") # Set source level
                    self.keithley.write(f":SENS:CURR:PROT {current_compliance}") # Current compliance

                    if auto_range_curr:
                        self.keithley.write(":SENS:CURR:RANG:AUTO ON") # Auto range for current
                    else:
                        self.keithley.write(":SENS:CURR:RANG:AUTO OFF")
                        self.keithley.write(f":SENS:CURR:RANG {current_compliance}") # Manual range based on compliance

                    self.keithley.write(f":SENS:CURR:NPLC {nplc}")
                    # Read current and voltage in one query (current, voltage)
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
                    # --- Current Source Mode ---
                    source_current = measurement_settings['isource_current']
                    voltage_compliance = measurement_settings['isource_voltage_compliance']
                    auto_range_volt = measurement_settings['isource_voltage_range_auto']
                    duration_hours = max(0.0, float(measurement_settings.get('isource_duration_hours', 0.0)))

                    # Sensor type (2/4 wire) might matter depending on setup
                    self.keithley.write(":SYST:RSEN OFF") # Assume 2-wire for voltage measure

                    self.keithley.write(":SENS:FUNC 'VOLT:DC'") # Measure Voltage (current also available via elements)
                    self.keithley.write(":SOUR:FUNC CURR") # Source Current
                    self.keithley.write(f":SOUR:CURR:MODE FIX") # Fixed source mode
                    self.keithley.write(f":SOUR:CURR:RANG {abs(source_current)}") # Set source range
                    self.keithley.write(f":SOUR:CURR {source_current}") # Set source level
                    self.keithley.write(f":SENS:VOLT:PROT {voltage_compliance}") # Voltage compliance

                    if auto_range_volt:
                        self.keithley.write(":SENS:VOLT:RANG:AUTO ON") # Auto range for voltage
                    else:
                        self.keithley.write(":SENS:VOLT:RANG:AUTO OFF")
                        self.keithley.write(f":SENS:VOLT:RANG {voltage_compliance}") # Manual range based on compliance

                    self.keithley.write(f":SENS:VOLT:NPLC {nplc}")
                    # Read voltage and current in one query (voltage, current)
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

                # Common settings after mode specifics
                # self.keithley.write(":TRIG:COUN 1") # Single trigger per read
                # self.keithley.write(":INIT:CONT OFF") # Ensure INITiate happens on READ?
                self.keithley.write(":TRIG:DEL 0") # No trigger delay
                self.keithley.write(":SOUR:DEL 0") # No source delay

            except pyvisa.errors.VisaIOError as e:
                 self.error_occurred.emit(f"VISA Error configuring instrument: {str(e)}")
                 return
            except Exception as e:
                self.error_occurred.emit(f"Error configuring instrument: {str(e)}")
                return

            # --- File Setup ---
            try:
                self.filename = self._create_filename(source_value_str)
                self.csvfile = open(self.filename, 'w', newline='')
                self.writer = csv.writer(self.csvfile)
                file_ready = True
            except Exception as e:
                self.error_occurred.emit(f"Error creating output file: {str(e)}")
                return

            # Write metadata
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
                'Line Frequency (Hz)': line_freq,
                'Settling Time (s)': settling_time,
                **metadata # Add mode-specific metadata
            }
            self._write_metadata(full_metadata)
            self.writer.writerow(csv_headers)
            self.csvfile.flush()

            # --- Measurement Loop ---
            self.status_update.emit("Starting measurement...")
            try:
                self.keithley.write(":OUTP ON") # Turn on output
                self.status_update.emit(f"Waiting for settling time ({settling_time}s)...")
                time.sleep(settling_time) # Wait for settling
            except Exception as e:
                 self.error_occurred.emit(f"Error turning on output: {str(e)}")
                 return

            last_save = self.start_time
            last_measurement_time = 0
            loop_count = 0
            # End time for source modes if duration provided (>0)
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
                time_since_last = now - last_measurement_time

                if time_since_last >= sample_interval:
                    try:
                        reading_str = self.keithley.query(":READ?").strip()
                        last_measurement_time = now # Update time after successful read
                    except pyvisa.errors.VisaIOError as e:
                        self.error_occurred.emit(f"VISA Read Error: {str(e)}. Stopping.")
                        break # Stop on read error
                    except ValueError:
                        self.status_update.emit(f"Warning: Invalid reading received '{reading_str}'. Skipping.")
                        time.sleep(0.01)
                        continue
                    except Exception as e:
                        self.error_occurred.emit(f"Unexpected Read Error: {str(e)}. Stopping.")
                        break # Stop on other errors

                    elapsed_time = now - self.start_time

                    # Parse readings and compliance per mode
                    compliance_status = 'OK'
                    compliance_type = None
                    data_dict: Dict[str, float] = {}
                    if self.mode == 'resistance':
                        # Expect a single resistance value
                        try:
                            value = float(reading_str)
                        except Exception:
                            value = float('nan')
                        # Compliance heuristic for resistance
                        comp_limit_v = measurement_settings.get('res_voltage_compliance')
                        compliance_type = 'Voltage'
                        if np.isfinite(value) and value > KEITHLEY_COMPLIANCE_MAGIC_NUMBER * 0.9:
                            compliance_status = 'V_COMP'
                        if not np.isfinite(value) or value < 0:
                            value = float('nan')
                            self.status_update.emit(f"Invalid value detected ({reading_str})")
                        data_dict = {'resistance': value}
                    elif self.mode == 'source_v':
                        # Expect "current,voltage"
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
                        # Expect "voltage,current"
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

                    # Notify UI on compliance
                    if compliance_status != 'OK' and compliance_type:
                        try:
                            self.compliance_hit.emit(compliance_type)
                            self.status_update.emit(f"⚠️ {compliance_type} Compliance Hit!")
                        except Exception:
                            pass

                    # Event Marker
                    event_marker = ""
                    if self.event_marker:
                        event_marker = self.event_marker
                        self.event_marker = ""
                        self.status_update.emit(f"⭐ Event marked at {elapsed_time:.3f}s ⭐")

                    # Prepare data row for CSV
                    now_unix = int(now)
                    if self.mode == 'resistance':
                        r = data_dict.get('resistance', float('nan'))
                        row_data = [
                            now_unix,
                            f"{elapsed_time:.3f}",
                            f"{r:.6e}" if np.isfinite(r) else "NaN",
                            compliance_status,
                            event_marker
                        ]
                    else:
                        v = data_dict.get('voltage', float('nan'))
                        i = data_dict.get('current', float('nan'))
                        r = (v / i) if (np.isfinite(v) and np.isfinite(i) and i != 0) else float('nan')
                        row_data = [
                            now_unix,
                            f"{elapsed_time:.3f}",
                            f"{v:.6e}" if np.isfinite(v) else "NaN",
                            f"{i:.6e}" if np.isfinite(i) else "NaN",
                            f"{r:.6e}" if np.isfinite(r) else "NaN",
                            compliance_status,
                            event_marker
                        ]

                    # Write to CSV
                    try:
                        self.writer.writerow(row_data)
                    except Exception as e:
                         self.error_occurred.emit(f"Error writing to CSV: {str(e)}")
                         self.status_update.emit("Warning: Failed to write data point to CSV.")

                    # Emit signal with the data for plotting/UI update
                    self.data_point.emit(now, data_dict, compliance_status, event_marker)

                    # Auto-save
                    if now - last_save >= auto_save_interval:
                        try:
                            self.csvfile.flush()
                            os.fsync(self.csvfile.fileno()) # Force write to disk
                            last_save = now
                        except Exception as e:
                             self.status_update.emit(f"Warning: Auto-save failed - {str(e)}")


                    # Status update for the main window status bar
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

                    loop_count += 1

                # Prevent busy-waiting if sample interval is very short or zero
                if sample_interval > 0.001:
                     # Sleep for a fraction of the interval, but check running flag often
                     sleep_duration = max(0.001, sample_interval / 10.0)
                     for _ in range(10):
                          if not self.running: break
                          time.sleep(sleep_duration / 10.0)
                else:
                     # Very high rate or zero interval, just yield
                     time.sleep(0.001)
                     if not self.running: break

                # Check duration end for source modes
                if end_time is not None and time.time() >= end_time:
                    self.status_update.emit("Reached configured duration. Stopping.")
                    self.running = False

            # --- End of Measurement Loop ---
            if instrument_ready and self.keithley:
                 try:
                      self.keithley.write(":OUTP OFF") # Turn off output
                      self.status_update.emit("Output turned OFF.")
                 except Exception as e:
                      self.status_update.emit(f"Warning: Could not turn off output - {str(e)}")


            final_message = f"Measurement ({self.mode}) stopped."
            if file_ready and self.filename:
                 try:
                     # Write end metadata
                     self.writer.writerow([])
                     end_unix_time = int(time.time())
                     full_metadata['End Time (Unix)'] = end_unix_time
                     full_metadata['End Time (Human Readable)'] = datetime.fromtimestamp(end_unix_time).isoformat()
                     self._write_metadata(full_metadata) # Write metadata again with end times
                 except Exception as e:
                     self.status_update.emit(f"Warning: Error writing final metadata - {str(e)}")

                 final_message = f"Measurement ({self.mode}) completed! Data saved to: {self.filename}"

            self.status_update.emit(final_message)
            self.measurement_complete.emit(self.mode) # Signal completion with mode

        except Exception as e:
            # Catch any unexpected errors during setup or loop
            self.error_occurred.emit(f"Unexpected Worker Error ({self.mode}): {str(e)}")

        finally:
            self._cleanup()
            self.running = False # Ensure flag is false

    def _create_filename(self, source_value_str: str) -> str:
        """Create filename for data storage, including mode."""
        base_dir = Path(self.settings['file']['data_directory'])
        base_dir.mkdir(parents=True, exist_ok=True) # Ensure base dir exists

        user_dir = base_dir / self.username
        user_dir.mkdir(exist_ok=True) # Ensure user dir exists

        timestamp = int(time.time())
        sanitized_name = ''.join(c if c.isalnum() else '_' for c in self.sample_name)

        mode_tag = "R"
        if self.mode == 'source_v':
            mode_tag = "VSRC"
        elif self.mode == 'source_i':
            mode_tag = "ISRC"

        filename = f"{timestamp}_{sanitized_name}_{mode_tag}_{source_value_str}.csv"
        return user_dir / filename

    def _write_metadata(self, params: Dict) -> None:
        """Write metadata dictionary to CSV file."""
        if not self.writer: return
        try:
            self.writer.writerow(['### METADATA START ###'])
            for key, value in params.items():
                self.writer.writerow([f'# {key}', value])
            self.writer.writerow(['### METADATA END ###'])
            self.writer.writerow([]) # Empty row for separation
        except Exception as e:
            self.status_update.emit(f"Warning: Failed to write metadata - {str(e)}")


    def mark_event(self, name: str = "MARK") -> None:
        """Mark a generic event to be recorded next data point."""
        self.event_marker = name

    def pause_measurement(self) -> None:
        """Pause the measurement."""
        if self.running:
            self.paused = True
            self.status_update.emit(f"Measurement ({self.mode}) paused")

    def resume_measurement(self) -> None:
        """Resume the measurement."""
        if self.running:
            self.paused = False
            self.status_update.emit(f"Measurement ({self.mode}) resumed")

    def stop_measurement(self) -> None:
        """Stop the measurement thread."""
        self.status_update.emit(f"Stopping measurement ({self.mode})...")
        self.running = False # Signal the loop to exit

    def _cleanup(self) -> None:
        """Clean up resources."""
        if self.keithley:
            try:
                # Try to turn off output and reset, but don't crash if it fails
                self.keithley.write(":OUTP OFF")
                # Optional: Reset? Might interfere if user wants to check settings after error
                # self.keithley.write("*RST")
                self.keithley.close()
                self.status_update.emit("Instrument disconnected.")
            except Exception as e:
                 # Log error during cleanup but don't raise
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

# ----- Custom Matplotlib Canvas (Modified for Flexibility) -----
class MplCanvas(FigureCanvas):
    """Matplotlib canvas for embedding in Qt."""
    def __init__(self, parent=None, width=8, height=5, dpi=100):
        self.fig = Figure(figsize=(width, height), dpi=dpi)
        self.axes = self.fig.add_subplot(111)
        self.axes.ticklabel_format(useOffset=False, style='plain') # Prevent scientific notation on axes
        super().__init__(self.fig)
        self.parent = parent # Store parent if needed

        # Create initial empty plot elements
        self.line, = self.axes.plot([], [], 'r-', label='Measurement') # Generic label initially
        # self.compliance_markers, = self.axes.plot([], [], 'kx', markersize=8, label='Compliance Hit') # Optional markers
        self.min_text = self.axes.text(0.02, 0.95, '', transform=self.axes.transAxes, ha='left', va='top', fontsize=9)
        self.max_text = self.axes.text(0.02, 0.90, '', transform=self.axes.transAxes, ha='left', va='top', fontsize=9)
        self.avg_text = self.axes.text(0.02, 0.85, '', transform=self.axes.transAxes, ha='left', va='top', fontsize=9)
        self.info_text = self.axes.text(0.98, 0.95, '', transform=self.axes.transAxes, ha='right', va='top', fontsize=9)
        self.compliance_indicator = self.axes.text(0.5, 1.02, '', transform=self.axes.transAxes, ha='center', va='bottom', fontsize=10, color='red', weight='bold')


        # Add bbox to text for visibility
        bbox_props = dict(boxstyle="round,pad=0.3", fc="white", ec="grey", alpha=0.7)
        self.min_text.set_bbox(bbox_props)
        self.max_text.set_bbox(bbox_props)
        self.avg_text.set_bbox(bbox_props)
        self.info_text.set_bbox(bbox_props)

        self.axes.legend(loc='upper right')
        self.fig.tight_layout(rect=[0, 0, 1, 0.95]) # Adjust layout slightly for title/compliance indicator

        self.set_plot_properties('Time (s)', 'Value', 'Measurement') # Set defaults

    def set_plot_properties(self, xlabel, ylabel, title, color='blue'):
        """Configure plot labels, title, and line color."""
        self.axes.set_xlabel(xlabel)
        self.axes.set_ylabel(ylabel)
        self.axes.set_title(title)
        self.line.set_label(title) # Update legend label
        self.line.set_color(color)
        self.axes.legend(loc='upper right') # Redraw legend
        self.axes.grid(True)
        self.draw_idle() # Use draw_idle for efficiency

    def update_plot(self, timestamps, values, compliance_list, stats, username, sample_name):
        """Update the plot with new data."""
        if not timestamps:
            self.clear_plot()
            return

        # Use elapsed time relative to the first point for x-axis
        start_time = timestamps[0]
        elapsed_times = [t - start_time for t in timestamps]

        # Filter out NaNs for plotting line, keep for stats/buffer
        valid_indices = [i for i, v in enumerate(values) if np.isfinite(v)]
        if not valid_indices: # No valid data to plot
             self.line.set_data([], [])
        else:
             plot_times = [elapsed_times[i] for i in valid_indices]
             plot_values = [values[i] for i in valid_indices]
             self.line.set_data(plot_times, plot_values)


        # Update axis limits
        self.axes.relim()
        self.axes.autoscale_view(True, True, True) # Autoscale both axes

        # Update statistics text (use current ylabel unit)
        unit = self.axes.get_ylabel()
        unit = unit.split('(')[-1].split(')')[0] if '(' in unit else '' # Extract unit like Ohms, V, A

        min_val = stats.get('min', float('inf'))
        max_val = stats.get('max', float('-inf'))
        avg_val = stats.get('avg', 0)

        self.min_text.set_text(f'Min: {min_val:.3f} {unit}' if np.isfinite(min_val) else 'Min: --')
        self.max_text.set_text(f'Max: {max_val:.3f} {unit}' if np.isfinite(max_val) else 'Max: --')
        self.avg_text.set_text(f'Avg: {avg_val:.3f} {unit}' if np.isfinite(avg_val) else 'Avg: --')
        self.info_text.set_text(f'User: {username}\nSample: {sample_name}')

        # Update compliance indicator
        last_compliance = compliance_list[-1] if compliance_list else 'OK'
        comp_text = ""
        if last_compliance == 'V_COMP':
             comp_text = "VOLTAGE COMPLIANCE HIT!"
        elif last_compliance == 'I_COMP':
             comp_text = "CURRENT COMPLIANCE HIT!"
        self.compliance_indicator.set_text(comp_text)

        # Redraw the canvas
        try:
             self.draw_idle() # More efficient than draw() for frequent updates
        except Exception as e:
             print(f"Error drawing plot: {e}")


    def clear_plot(self):
        """Clear the plot and reset annotations."""
        self.line.set_data([], [])
        # self.compliance_markers.set_data([], [])
        self.min_text.set_text('Min: --')
        self.max_text.set_text('Max: --')
        self.avg_text.set_text('Avg: --')
        self.info_text.set_text('User: --\nSample: --')
        self.compliance_indicator.set_text('')
        self.axes.relim()
        self.axes.autoscale_view(True, True, True)
        self.draw_idle()

# ----- Settings Dialog (Updated) -----
class SettingsDialog(QDialog):
    """Dialog for editing configuration settings."""
    def __init__(self, config_manager, username=None, parent=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self.username = username

        if username:
            # Load user settings, ensuring all keys exist by merging with defaults
            global_settings = config_manager.config
            user_settings_raw = config_manager.get_user_settings(username)
            self.settings = {
                 'measurement': {**global_settings['measurement'], **user_settings_raw.get('measurement', {})},
                 'display': {**global_settings['display'], **user_settings_raw.get('display', {})},
                 'file': {**global_settings['file'], **user_settings_raw.get('file', {})},
            }
            self.setWindowTitle(f"Settings for {username}")
        else:
            # Global settings are just the current config
            self.settings = {
                'measurement': dict(config_manager.config['measurement']),
                'display': dict(config_manager.config['display']),
                'file': dict(config_manager.config['file'])
            }
            self.setWindowTitle("Global Settings")

        self.init_ui()
        self.load_settings() # Load data into widgets

    def init_ui(self):
        self.setMinimumWidth(600)
        self.tabs = QTabWidget()
        self.measurement_tab = self.create_measurement_tab()
        self.display_tab = self.create_display_tab()
        self.file_tab = self.create_file_tab()

        self.tabs.addTab(self.measurement_tab, "Measurement")
        self.tabs.addTab(self.display_tab, "Display")
        self.tabs.addTab(self.file_tab, "File")

        self.save_button = QPushButton(QIcon.fromTheme("document-save"), "Save")
        self.cancel_button = QPushButton(QIcon.fromTheme("dialog-cancel"), "Cancel")
        self.save_button.clicked.connect(self.save_settings)
        self.cancel_button.clicked.connect(self.reject)

        button_layout = QHBoxLayout()
        button_layout.addStretch()
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.cancel_button)

        main_layout = QVBoxLayout()
        main_layout.addWidget(self.tabs)
        main_layout.addLayout(button_layout)
        self.setLayout(main_layout)

    def create_measurement_tab(self):
        tab = QWidget()
        main_layout = QVBoxLayout()

        # --- General Settings ---
        general_group = QGroupBox("General Instrument Settings")
        form_layout = QFormLayout()
        self.gpib_address = QLineEdit()
        self.detect_gpib_button = QPushButton("Detect Devices")
        self.detect_gpib_button.clicked.connect(self.detect_gpib_devices)
        gpib_layout = QHBoxLayout()
        gpib_layout.addWidget(self.gpib_address)
        gpib_layout.addWidget(self.detect_gpib_button)
        form_layout.addRow("GPIB Address:", gpib_layout)

        self.sampling_rate = QDoubleSpinBox(decimals=1, minimum=0.1, maximum=100.0, singleStep=1.0, suffix=" Hz")
        form_layout.addRow("Sampling Rate:", self.sampling_rate)
        self.nplc = QDoubleSpinBox(decimals=2, minimum=0.01, maximum=10.0, singleStep=0.1)
        form_layout.addRow("NPLC:", self.nplc)
        self.settling_time = QDoubleSpinBox(decimals=2, minimum=0.0, maximum=10.0, singleStep=0.1, suffix=" s")
        form_layout.addRow("Settling Time:", self.settling_time)
        general_group.setLayout(form_layout)
        main_layout.addWidget(general_group)

        # --- Resistance Mode Settings ---
        res_group = QGroupBox("Resistance Mode (Source I, Measure R)")
        form_layout = QFormLayout()
        self.res_test_current = QDoubleSpinBox(decimals=6, minimum=1e-7, maximum=1.0, singleStep=1e-3, suffix=" A")
        form_layout.addRow("Test Current:", self.res_test_current)
        self.res_voltage_compliance = QDoubleSpinBox(decimals=2, minimum=0.1, maximum=100.0, singleStep=0.1, suffix=" V")
        form_layout.addRow("Voltage Compliance:", self.res_voltage_compliance)
        self.res_measurement_type = QComboBox()
        self.res_measurement_type.addItems(["2-wire", "4-wire"])
        form_layout.addRow("Measurement Type:", self.res_measurement_type)
        self.res_auto_range = QCheckBox("Auto Range Resistance")
        form_layout.addRow(self.res_auto_range)
        res_group.setLayout(form_layout)
        main_layout.addWidget(res_group)

        # --- Voltage Source Mode Settings ---
        vsrc_group = QGroupBox("Voltage Source Mode (Source V, Measure I)")
        form_layout = QFormLayout()
        self.vsource_voltage = QDoubleSpinBox(decimals=3, minimum=-100.0, maximum=100.0, singleStep=0.1, suffix=" V")
        form_layout.addRow("Source Voltage:", self.vsource_voltage)
        self.vsource_current_compliance = QDoubleSpinBox(decimals=6, minimum=1e-7, maximum=1.0, singleStep=1e-3, suffix=" A")
        form_layout.addRow("Current Compliance:", self.vsource_current_compliance)
        self.vsource_current_range_auto = QCheckBox("Auto Range Current Measurement")
        form_layout.addRow(self.vsource_current_range_auto)
        self.vsource_duration_hours = QDoubleSpinBox(decimals=2, minimum=0.0, maximum=168.0, singleStep=0.5, suffix=" h")
        form_layout.addRow("Duration (hours):", self.vsource_duration_hours)
        vsrc_group.setLayout(form_layout)
        main_layout.addWidget(vsrc_group)

        # --- Current Source Mode Settings ---
        isrc_group = QGroupBox("Current Source Mode (Source I, Measure V)")
        form_layout = QFormLayout()
        self.isource_current = QDoubleSpinBox(decimals=6, minimum=-1.0, maximum=1.0, singleStep=1e-3, suffix=" A")
        form_layout.addRow("Source Current:", self.isource_current)
        self.isource_voltage_compliance = QDoubleSpinBox(decimals=2, minimum=0.1, maximum=100.0, singleStep=0.1, suffix=" V")
        form_layout.addRow("Voltage Compliance:", self.isource_voltage_compliance)
        self.isource_voltage_range_auto = QCheckBox("Auto Range Voltage Measurement")
        form_layout.addRow(self.isource_voltage_range_auto)
        self.isource_duration_hours = QDoubleSpinBox(decimals=2, minimum=0.0, maximum=168.0, singleStep=0.5, suffix=" h")
        form_layout.addRow("Duration (hours):", self.isource_duration_hours)
        isrc_group.setLayout(form_layout)
        main_layout.addWidget(isrc_group)

        main_layout.addStretch() # Push groups to top
        tab.setLayout(main_layout)
        return tab

    def create_display_tab(self):
        tab = QWidget()
        layout = QFormLayout()

        self.enable_plot = QCheckBox()
        layout.addRow("Enable Real-time Plots:", self.enable_plot)

        self.plot_update_interval = QSpinBox(minimum=50, maximum=5000, singleStep=50, suffix=" ms")
        layout.addRow("Plot Update Interval:", self.plot_update_interval)

        # Plot colors
        colors = ["red", "blue", "green", "black", "purple", "orange", "cyan", "magenta"]
        self.plot_color_r = QComboBox()
        self.plot_color_r.addItems(colors)
        layout.addRow("Resistance Plot Color:", self.plot_color_r)
        self.plot_color_v = QComboBox()
        self.plot_color_v.addItems(colors)
        layout.addRow("V Source Plot Color:", self.plot_color_v)
        self.plot_color_i = QComboBox()
        self.plot_color_i.addItems(colors)
        layout.addRow("I Source Plot Color:", self.plot_color_i)

        # Plot figure size (simplified)
        figsize_layout = QHBoxLayout()
        self.plot_width = QDoubleSpinBox(decimals=1, minimum=4, maximum=20, singleStep=0.5)
        self.plot_height = QDoubleSpinBox(decimals=1, minimum=3, maximum=15, singleStep=0.5)
        figsize_layout.addWidget(QLabel("Width:"))
        figsize_layout.addWidget(self.plot_width)
        figsize_layout.addSpacing(20)
        figsize_layout.addWidget(QLabel("Height:"))
        figsize_layout.addWidget(self.plot_height)
        layout.addRow("Plot Figure Size (inches):", figsize_layout)

        # Buffer size
        self.buffer_size = QSpinBox(minimum=0, maximum=1000000, singleStep=100)
        self.buffer_size.setSpecialValueText("Unlimited (Use with caution!)")
        layout.addRow("Data Buffer Size (points):", self.buffer_size)

        tab.setLayout(layout)
        return tab

    def create_file_tab(self):
        tab = QWidget()
        layout = QFormLayout()

        self.auto_save_interval = QSpinBox(minimum=1, maximum=3600, singleStep=10, suffix=" s")
        layout.addRow("Auto-save Interval:", self.auto_save_interval)

        self.data_directory = QLineEdit()
        self.browse_button = QPushButton(QIcon.fromTheme("folder-open"), "Browse...")
        self.browse_button.clicked.connect(self.browse_directory)
        dir_layout = QHBoxLayout()
        dir_layout.addWidget(self.data_directory)
        dir_layout.addWidget(self.browse_button)
        layout.addRow("Data Directory:", dir_layout)

        tab.setLayout(layout)
        return tab

    def load_settings(self):
        """Load current settings into the UI widgets."""
        # Measurement Tab
        m_cfg = self.settings['measurement']
        self.gpib_address.setText(m_cfg['gpib_address'])
        self.sampling_rate.setValue(m_cfg['sampling_rate'])
        self.nplc.setValue(m_cfg['nplc'])
        self.settling_time.setValue(m_cfg['settling_time'])

        self.res_test_current.setValue(m_cfg['res_test_current'])
        self.res_voltage_compliance.setValue(m_cfg['res_voltage_compliance'])
        self.res_measurement_type.setCurrentText(m_cfg['res_measurement_type'])
        self.res_auto_range.setChecked(m_cfg['res_auto_range'])

        self.vsource_voltage.setValue(m_cfg['vsource_voltage'])
        self.vsource_current_compliance.setValue(m_cfg['vsource_current_compliance'])
        self.vsource_current_range_auto.setChecked(m_cfg['vsource_current_range_auto'])
        if hasattr(self, 'vsource_duration_hours'):
            self.vsource_duration_hours.setValue(m_cfg.get('vsource_duration_hours', 0.0))

        self.isource_current.setValue(m_cfg['isource_current'])
        self.isource_voltage_compliance.setValue(m_cfg['isource_voltage_compliance'])
        self.isource_voltage_range_auto.setChecked(m_cfg['isource_voltage_range_auto'])
        if hasattr(self, 'isource_duration_hours'):
            self.isource_duration_hours.setValue(m_cfg.get('isource_duration_hours', 0.0))

        # Display Tab
        d_cfg = self.settings['display']
        self.enable_plot.setChecked(d_cfg['enable_plot'])
        self.plot_update_interval.setValue(d_cfg['plot_update_interval'])
        self.plot_color_r.setCurrentText(d_cfg['plot_color_r'])
        self.plot_color_v.setCurrentText(d_cfg['plot_color_v'])
        self.plot_color_i.setCurrentText(d_cfg['plot_color_i'])
        self.plot_width.setValue(d_cfg['plot_figsize'][0])
        self.plot_height.setValue(d_cfg['plot_figsize'][1])
        buffer_size = d_cfg['buffer_size']
        self.buffer_size.setValue(0 if buffer_size is None or buffer_size <= 0 else buffer_size)

        # File Tab
        f_cfg = self.settings['file']
        self.auto_save_interval.setValue(f_cfg['auto_save_interval'])
        self.data_directory.setText(f_cfg['data_directory'])

    def save_settings(self):
        """Gather settings from UI and save them."""
        # Measurement Tab
        m_cfg = self.settings['measurement'] # Get reference to modify
        m_cfg['gpib_address'] = self.gpib_address.text()
        m_cfg['sampling_rate'] = self.sampling_rate.value()
        m_cfg['nplc'] = self.nplc.value()
        m_cfg['settling_time'] = self.settling_time.value()

        m_cfg['res_test_current'] = self.res_test_current.value()
        m_cfg['res_voltage_compliance'] = self.res_voltage_compliance.value()
        m_cfg['res_measurement_type'] = self.res_measurement_type.currentText()
        m_cfg['res_auto_range'] = self.res_auto_range.isChecked()

        m_cfg['vsource_voltage'] = self.vsource_voltage.value()
        m_cfg['vsource_current_compliance'] = self.vsource_current_compliance.value()
        m_cfg['vsource_current_range_auto'] = self.vsource_current_range_auto.isChecked()
        if hasattr(self, 'vsource_duration_hours'):
            m_cfg['vsource_duration_hours'] = self.vsource_duration_hours.value()

        m_cfg['isource_current'] = self.isource_current.value()
        m_cfg['isource_voltage_compliance'] = self.isource_voltage_compliance.value()
        m_cfg['isource_voltage_range_auto'] = self.isource_voltage_range_auto.isChecked()
        if hasattr(self, 'isource_duration_hours'):
            m_cfg['isource_duration_hours'] = self.isource_duration_hours.value()

        # Display Tab
        d_cfg = self.settings['display']
        d_cfg['enable_plot'] = self.enable_plot.isChecked()
        d_cfg['plot_update_interval'] = self.plot_update_interval.value()
        d_cfg['plot_color_r'] = self.plot_color_r.currentText()
        d_cfg['plot_color_v'] = self.plot_color_v.currentText()
        d_cfg['plot_color_i'] = self.plot_color_i.currentText()
        d_cfg['plot_figsize'] = [self.plot_width.value(), self.plot_height.value()]
        buffer_val = self.buffer_size.value()
        d_cfg['buffer_size'] = None if buffer_val == 0 else buffer_val

        # File Tab
        f_cfg = self.settings['file']
        f_cfg['auto_save_interval'] = self.auto_save_interval.value()
        f_cfg['data_directory'] = self.data_directory.text()

        # Save using ConfigManager
        if self.username:
            self.config_manager.update_user_settings(self.username, self.settings)
        else:
            self.config_manager.update_global_settings(self.settings)

        QMessageBox.information(self, "Settings Saved", "Settings have been updated successfully.")
        self.accept() # Close dialog

    def browse_directory(self):
        current_dir = self.data_directory.text()
        if not os.path.isdir(current_dir):
             current_dir = os.path.expanduser("~") # Default to home dir

        directory = QFileDialog.getExistingDirectory(
            self, "Select Data Directory", current_dir
        )
        if directory:
            self.data_directory.setText(directory)

    def detect_gpib_devices(self):
        try:
            self.setEnabled(False) # Disable dialog during scan
            QApplication.setOverrideCursor(Qt.WaitCursor)
            rm = pyvisa.ResourceManager()
            resources = rm.list_resources()
            QApplication.restoreOverrideCursor()
            self.setEnabled(True)

            if not resources:
                QMessageBox.information(self, "GPIB Detection", "No VISA instruments detected.")
                return

            # Simple selection dialog
            dialog = QDialog(self)
            dialog.setWindowTitle("Select GPIB Device")
            layout = QVBoxLayout()
            list_widget = QComboBox(dialog)
            list_widget.addItems(resources)
            current_addr = self.gpib_address.text()
            if current_addr in resources:
                 list_widget.setCurrentText(current_addr)

            layout.addWidget(QLabel("Select the instrument address:"))
            layout.addWidget(list_widget)

            button_box = QHBoxLayout()
            select_button = QPushButton("Select")
            cancel_button = QPushButton("Cancel")
            select_button.clicked.connect(dialog.accept)
            cancel_button.clicked.connect(dialog.reject)
            button_box.addStretch()
            button_box.addWidget(select_button)
            button_box.addWidget(cancel_button)
            layout.addLayout(button_box)
            dialog.setLayout(layout)

            if dialog.exec_():
                self.gpib_address.setText(list_widget.currentText())

        except Exception as e:
            QApplication.restoreOverrideCursor()
            self.setEnabled(True)
            QMessageBox.critical(self, "Error", f"Error detecting GPIB devices: {str(e)}")

# ----- User Selection Dialog (Unchanged) -----
class UserSelectionDialog(QDialog):
    """Dialog for selecting or creating a user."""
    def __init__(self, config_manager, parent=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self.selected_user = None

        self.setWindowTitle("User Selection")
        self.init_ui()

    def init_ui(self):
        """Initialize the UI."""
        layout = QVBoxLayout()

        # Get users and last user
        users = self.config_manager.get_users()
        last_user = self.config_manager.get_last_user()

        # Create user list
        self.user_combo = QComboBox()
        if users:
            self.user_combo.addItems(users)
            if last_user and last_user in users:
                self.user_combo.setCurrentText(last_user)
        else:
             layout.addWidget(QLabel("No users found. Please create one."))

        # Create new user section
        new_user_group = QGroupBox("Create New User")
        new_user_layout = QHBoxLayout()
        self.new_user_input = QLineEdit()
        self.new_user_input.setPlaceholderText("Enter new username")
        self.create_user_button = QPushButton("Create && Select")
        self.create_user_button.clicked.connect(self.create_new_user)

        new_user_layout.addWidget(QLabel("Name:"))
        new_user_layout.addWidget(self.new_user_input)
        new_user_layout.addWidget(self.create_user_button)
        new_user_group.setLayout(new_user_layout)

        # Create buttons
        self.select_button = QPushButton("Select Existing User")
        self.select_button.clicked.connect(self.select_user)
        self.select_button.setEnabled(len(users) > 0)

        self.settings_button = QPushButton("Global Settings...")
        self.settings_button.clicked.connect(self.open_global_settings)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)

        # Create button layout
        button_layout = QHBoxLayout()
        button_layout.addWidget(self.settings_button)
        button_layout.addStretch()
        button_layout.addWidget(self.select_button)
        button_layout.addWidget(self.cancel_button)

        # Add widgets to layout
        if users:
            layout.addWidget(QLabel("Select Existing User:"))
            layout.addWidget(self.user_combo)

        layout.addWidget(new_user_group)
        layout.addLayout(button_layout)

        self.setLayout(layout)
        self.new_user_input.setFocus() # Focus on new user input initially

    def create_new_user(self):
        """Create a new user."""
        username = self.new_user_input.text().strip()

        if not username:
            QMessageBox.warning(self, "Invalid Username", "Please enter a valid username.")
            return

        if username in self.config_manager.get_users():
             QMessageBox.warning(self, "User Exists", f"User '{username}' already exists. Please choose a different name or select the existing user.")
             return

        # Add user to config
        self.config_manager.add_user(username)
        self.selected_user = username
        self.config_manager.set_last_user(self.selected_user) # Set as last user

        QMessageBox.information(self, "User Created", f"User '{username}' created and selected.")
        self.accept() # Accept dialog

    def select_user(self):
        """Select an existing user."""
        if self.user_combo.count() == 0:
            QMessageBox.warning(self, "No Users", "No existing users to select.")
            return

        self.selected_user = self.user_combo.currentText()
        self.config_manager.set_last_user(self.selected_user)

        self.accept() # Accept dialog

    def open_global_settings(self):
        """Open the global settings dialog."""
        # This dialog is modal, so it blocks the user selection dialog
        dialog = SettingsDialog(self.config_manager, parent=self)
        dialog.exec_()
        # No action needed after global settings close here


# ----- Main Application Window (Modified for Tabs) -----
class ResistanceMeterApp(QMainWindow):
    """Main application window for ResistaMet with multiple modes."""
    def __init__(self):
        super().__init__()

        self.config_manager = ConfigManager()
        self.data_buffers = { # Separate buffer for each mode (multi-channel capable)
             'resistance': EnhancedDataBuffer(),
             'source_v': EnhancedDataBuffer(),
             'source_i': EnhancedDataBuffer()
        }
        self.measurement_worker = None
        self.plot_timer = QTimer(self)
        self.plot_timer.timeout.connect(self.update_active_plot)

        self.current_user = None
        self.user_settings = None
        self.measurement_running = False
        self.active_mode = None # Track which mode's worker is running

        self.setWindowTitle(f"ResistaMet GUI v{__version__}")
        self.setMinimumSize(900, 700)
        self.setWindowIcon(QIcon.fromTheme("accessories-voltmeter")) # Use a standard icon

        self.init_ui()
        self.select_user() # Prompt for user on startup

    def init_ui(self):
        """Initialize the UI."""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # --- Top Section: User and Sample ---
        top_panel = QHBoxLayout()
        user_group = QGroupBox("User")
        user_layout = QHBoxLayout()
        self.user_label = QLabel("User: <None Selected>")
        self.change_user_button = QPushButton(QIcon.fromTheme("system-users"), "Change User")
        self.change_user_button.clicked.connect(self.select_user)
        user_layout.addWidget(self.user_label)
        user_layout.addWidget(self.change_user_button)
        user_group.setLayout(user_layout)

        sample_group = QGroupBox("Sample")
        sample_layout = QHBoxLayout()
        self.sample_input = QLineEdit()
        self.sample_input.setPlaceholderText("Enter sample name before starting")
        sample_layout.addWidget(self.sample_input)
        sample_group.setLayout(sample_layout)

        top_panel.addWidget(user_group)
        top_panel.addWidget(sample_group, 1) # Allow sample group to stretch
        main_layout.addLayout(top_panel)

        # --- Main Tabbed Interface ---
        self.main_tabs = QTabWidget()
        self.main_tabs.currentChanged.connect(self.handle_tab_change)

        # Create tabs
        self.tab_resistance = self.create_resistance_tab()
        self.tab_voltage_source = self.create_voltage_source_tab()
        self.tab_current_source = self.create_current_source_tab()

        self.main_tabs.addTab(self.tab_resistance, "Resistance Measurement")
        self.main_tabs.addTab(self.tab_voltage_source, "Voltage Source")
        self.main_tabs.addTab(self.tab_current_source, "Current Source")

        main_layout.addWidget(self.main_tabs, 1) # Allow tabs to stretch vertically

        # --- Status Display ---
        status_group = QGroupBox("Status Log")
        status_layout = QVBoxLayout()
        self.status_display = QTextEdit()
        self.status_display.setReadOnly(True)
        self.status_display.setAcceptRichText(True) # Allow basic HTML/colors
        self.status_display.setMaximumHeight(150) # Limit height
        status_layout.addWidget(self.status_display)
        status_group.setLayout(status_layout)
        main_layout.addWidget(status_group)

        # --- Status Bar ---
        self.statusBar().showMessage("Ready")

        # --- Menu Bar ---
        self.create_menus()

        # --- Keyboard Shortcuts ---
        self.shortcut_mark = QShortcut(Qt.Key_M, self)
        self.shortcut_mark.activated.connect(self.mark_event_shortcut)
        self.shortcut_mark.setEnabled(False) # Disabled initially

    def create_tab_widget(self, mode: str) -> QWidget:
        """Helper to create the basic structure of a measurement tab."""
        tab_widget = QWidget()
        tab_layout = QVBoxLayout(tab_widget)

        # --- Parameter Inputs ---
        param_group = QGroupBox("Parameters")
        param_layout = QFormLayout()
        param_group.setLayout(param_layout)
        # Widgets will be added by specific create_*_tab methods

        # --- Plot Area ---
        plot_group = QGroupBox("Real-time Data")
        plot_layout = QVBoxLayout()
        canvas = MplCanvas(self, width=8, height=5, dpi=90) # Create canvas instance
        toolbar = NavigationToolbar(canvas, self)
        plot_layout.addWidget(toolbar)
        plot_layout.addWidget(canvas)
        plot_group.setLayout(plot_layout)

        # --- Controls ---
        control_group = QGroupBox("Control")
        control_layout = QHBoxLayout()
        start_button = QPushButton(QIcon.fromTheme("media-playback-start"), "Start")
        stop_button = QPushButton(QIcon.fromTheme("media-playback-stop"), "Stop")
        stop_button.setEnabled(False)
        pause_button = QPushButton(QIcon.fromTheme("media-playback-pause"), "Pause")
        pause_button.setEnabled(False)
        pause_button.setCheckable(True)
        status_label = QLabel("Status: Idle") # Compliance/status indicator for the tab
        status_label.setStyleSheet("font-weight: bold;")

        control_layout.addWidget(start_button)
        control_layout.addWidget(stop_button)
        control_layout.addWidget(pause_button)
        control_layout.addStretch()
        control_layout.addWidget(status_label)
        control_group.setLayout(control_layout)


        tab_layout.addWidget(param_group)
        tab_layout.addWidget(plot_group, 1) # Allow plot to stretch
        tab_layout.addWidget(control_group)

        # Store references to widgets within the tab_widget itself for easy access
        tab_widget.mode = mode
        tab_widget.param_layout = param_layout
        tab_widget.canvas = canvas
        tab_widget.start_button = start_button
        tab_widget.stop_button = stop_button
        tab_widget.pause_button = pause_button
        tab_widget.status_label = status_label

        return tab_widget

    def create_resistance_tab(self):
        widget = self.create_tab_widget('resistance')
        layout = widget.param_layout

        # Add specific widgets for Resistance mode
        widget.res_test_current = QDoubleSpinBox(decimals=6, minimum=1e-7, maximum=1.0, singleStep=1e-3, suffix=" A")
        layout.addRow("Test Current:", widget.res_test_current)
        widget.res_voltage_compliance = QDoubleSpinBox(decimals=2, minimum=0.1, maximum=100.0, singleStep=0.1, suffix=" V")
        layout.addRow("Voltage Compliance:", widget.res_voltage_compliance)
        widget.res_measurement_type = QComboBox()
        widget.res_measurement_type.addItems(["2-wire", "4-wire"])
        layout.addRow("Measurement Type:", widget.res_measurement_type)
        widget.res_auto_range = QCheckBox("Auto Range Resistance")
        layout.addRow(widget.res_auto_range)
        widget.mark_event_button = QPushButton(QIcon.fromTheme("emblem-important"), "Mark Event (M)")
        widget.mark_event_button.setEnabled(False)
        layout.addRow(widget.mark_event_button)


        # Connect signals for this tab
        widget.start_button.clicked.connect(lambda: self.start_measurement('resistance'))
        widget.stop_button.clicked.connect(self.stop_current_measurement)
        widget.mark_event_button.clicked.connect(self.mark_event_shortcut)
        widget.pause_button.toggled.connect(lambda checked: self.pause_resume_measurement(checked))

        return widget

    def create_voltage_source_tab(self):
        widget = self.create_tab_widget('source_v')
        layout = widget.param_layout

        # Add specific widgets for V Source mode
        widget.vsource_voltage = QDoubleSpinBox(decimals=3, minimum=-100.0, maximum=100.0, singleStep=0.1, suffix=" V")
        layout.addRow("Source Voltage:", widget.vsource_voltage)
        widget.vsource_current_compliance = QDoubleSpinBox(decimals=6, minimum=1e-7, maximum=1.0, singleStep=1e-3, suffix=" A")
        layout.addRow("Current Compliance:", widget.vsource_current_compliance)
        widget.vsource_current_range_auto = QCheckBox("Auto Range Current Measurement")
        layout.addRow(widget.vsource_current_range_auto)
        widget.vsource_duration = QDoubleSpinBox(decimals=2, minimum=0.0, maximum=168.0, singleStep=0.5, suffix=" h")
        layout.addRow("Duration (hours):", widget.vsource_duration)

        widget.v_plot_var = QComboBox()
        widget.v_plot_var.addItems(["current", "voltage", "resistance"])  # default to current
        layout.addRow("Plot Variable:", widget.v_plot_var)
        widget.mark_event_button = QPushButton(QIcon.fromTheme("emblem-important"), "Mark Event (M)")
        widget.mark_event_button.setEnabled(False)
        layout.addRow(widget.mark_event_button)

        # Connect signals
        widget.start_button.clicked.connect(lambda: self.start_measurement('source_v'))
        widget.stop_button.clicked.connect(self.stop_current_measurement)
        widget.pause_button.toggled.connect(lambda checked: self.pause_resume_measurement(checked))
        widget.mark_event_button.clicked.connect(self.mark_event_shortcut)
        widget.v_plot_var.currentTextChanged.connect(lambda _: self.update_canvas_labels_for_mode('source_v'))

        return widget

    def create_current_source_tab(self):
        widget = self.create_tab_widget('source_i')
        layout = widget.param_layout

        # Add specific widgets for I Source mode
        widget.isource_current = QDoubleSpinBox(decimals=6, minimum=-1.0, maximum=1.0, singleStep=1e-3, suffix=" A")
        layout.addRow("Source Current:", widget.isource_current)
        widget.isource_voltage_compliance = QDoubleSpinBox(decimals=2, minimum=0.1, maximum=100.0, singleStep=0.1, suffix=" V")
        layout.addRow("Voltage Compliance:", widget.isource_voltage_compliance)
        widget.isource_voltage_range_auto = QCheckBox("Auto Range Voltage Measurement")
        layout.addRow(widget.isource_voltage_range_auto)
        widget.isource_duration = QDoubleSpinBox(decimals=2, minimum=0.0, maximum=168.0, singleStep=0.5, suffix=" h")
        layout.addRow("Duration (hours):", widget.isource_duration)

        widget.i_plot_var = QComboBox()
        widget.i_plot_var.addItems(["voltage", "current", "resistance"])  # default to voltage
        layout.addRow("Plot Variable:", widget.i_plot_var)
        widget.mark_event_button = QPushButton(QIcon.fromTheme("emblem-important"), "Mark Event (M)")
        widget.mark_event_button.setEnabled(False)
        layout.addRow(widget.mark_event_button)

        # Connect signals
        widget.start_button.clicked.connect(lambda: self.start_measurement('source_i'))
        widget.stop_button.clicked.connect(self.stop_current_measurement)
        widget.pause_button.toggled.connect(lambda checked: self.pause_resume_measurement(checked))
        widget.mark_event_button.clicked.connect(self.mark_event_shortcut)
        widget.i_plot_var.currentTextChanged.connect(lambda _: self.update_canvas_labels_for_mode('source_i'))

        return widget

    def create_menus(self):
        menu_bar = self.menuBar()
        # File Menu
        file_menu = menu_bar.addMenu("&File")
        save_plot_action = QAction(QIcon.fromTheme("document-save"), "Save Plot...", self)
        save_plot_action.triggered.connect(self.save_active_plot)
        exit_action = QAction(QIcon.fromTheme("application-exit"), "Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(save_plot_action)
        file_menu.addSeparator()
        file_menu.addAction(exit_action)

        # Settings Menu
        settings_menu = menu_bar.addMenu("&Settings")
        user_settings_action = QAction(QIcon.fromTheme("preferences-system"), "User Settings...", self)
        user_settings_action.triggered.connect(self.open_user_settings)
        global_settings_action = QAction(QIcon.fromTheme("preferences-system-windows"), "Global Settings...", self)
        global_settings_action.triggered.connect(self.open_global_settings)
        settings_menu.addAction(user_settings_action)
        settings_menu.addAction(global_settings_action)

        # Help Menu
        help_menu = menu_bar.addMenu("&Help")
        about_action = QAction(QIcon.fromTheme("help-about"), "About", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

    def select_user(self):
        """Open user selection dialog and update UI."""
        # If a measurement is running, prevent changing user
        if self.measurement_running:
            QMessageBox.warning(self, "Action Denied", "Cannot change user while a measurement is running.")
            return

        dialog = UserSelectionDialog(self.config_manager, self)
        if dialog.exec_():
            username = dialog.selected_user
            if username:
                self.current_user = username
                self.user_label.setText(f"User: <b>{username}</b>")
                self.user_settings = self.config_manager.get_user_settings(username)
                self.log_status(f"User selected: {username}")
                self.statusBar().showMessage(f"User: {username} | Ready")
                self.update_ui_from_settings()
                # Clear all data buffers on user change
                for buffer in self.data_buffers.values():
                    buffer.clear()
                self.clear_all_plots()
        else:
             # If no user is selected on first launch, disable start buttons
             if not self.current_user:
                  self.log_status("No user selected. Please select or create a user.")
                  self.set_all_controls_enabled(False) # Disable all start buttons


    def update_ui_from_settings(self):
        """Load settings into the relevant UI widgets on the tabs."""
        if not self.user_settings: return

        m_cfg = self.user_settings['measurement']
        d_cfg = self.user_settings['display']

        # Resistance Tab
        self.tab_resistance.res_test_current.setValue(m_cfg['res_test_current'])
        self.tab_resistance.res_voltage_compliance.setValue(m_cfg['res_voltage_compliance'])
        self.tab_resistance.res_measurement_type.setCurrentText(m_cfg['res_measurement_type'])
        self.tab_resistance.res_auto_range.setChecked(m_cfg['res_auto_range'])
        self.tab_resistance.canvas.set_plot_properties(
            'Elapsed Time (s)', 'Resistance (Ohms)', 'Resistance Measurement', d_cfg['plot_color_r'])

        # Voltage Source Tab
        self.tab_voltage_source.vsource_voltage.setValue(m_cfg['vsource_voltage'])
        self.tab_voltage_source.vsource_current_compliance.setValue(m_cfg['vsource_current_compliance'])
        self.tab_voltage_source.vsource_current_range_auto.setChecked(m_cfg['vsource_current_range_auto'])
        # Duration and default plot variable
        if hasattr(self.tab_voltage_source, 'vsource_duration'):
            self.tab_voltage_source.vsource_duration.setValue(m_cfg.get('vsource_duration_hours', 0.0))
        if hasattr(self.tab_voltage_source, 'v_plot_var'):
            self.tab_voltage_source.v_plot_var.setCurrentText('current')
        self.tab_voltage_source.canvas.set_plot_properties(
            'Elapsed Time (s)', 'Measured Current (A)', 'Voltage Source Output', d_cfg['plot_color_v'])

        # Current Source Tab
        self.tab_current_source.isource_current.setValue(m_cfg['isource_current'])
        self.tab_current_source.isource_voltage_compliance.setValue(m_cfg['isource_voltage_compliance'])
        self.tab_current_source.isource_voltage_range_auto.setChecked(m_cfg['isource_voltage_range_auto'])
        if hasattr(self.tab_current_source, 'isource_duration'):
            self.tab_current_source.isource_duration.setValue(m_cfg.get('isource_duration_hours', 0.0))
        if hasattr(self.tab_current_source, 'i_plot_var'):
            self.tab_current_source.i_plot_var.setCurrentText('voltage')
        self.tab_current_source.canvas.set_plot_properties(
            'Elapsed Time (s)', 'Measured Voltage (V)', 'Current Source Output', d_cfg['plot_color_i'])

        # Update buffer sizes
        buffer_size = d_cfg.get('buffer_size')
        new_size = None if buffer_size is None or buffer_size <= 0 else buffer_size
        for mode, buffer in list(self.data_buffers.items()):
            if buffer.size != new_size:
                self.data_buffers[mode] = EnhancedDataBuffer(size=new_size) # Recreate buffer

        self.clear_all_plots()
        self.log_status("User settings loaded into UI.")


    def open_user_settings(self):
        if not self.current_user:
            QMessageBox.warning(self, "No User Selected", "Please select a user first to edit their settings.")
            return
        if self.measurement_running:
            QMessageBox.warning(self, "Action Denied", "Cannot change settings while a measurement is running.")
            return

        dialog = SettingsDialog(self.config_manager, self.current_user, self)
        if dialog.exec_():
            self.log_status(f"User settings for {self.current_user} updated.")
            # Reload settings and update UI
            self.user_settings = self.config_manager.get_user_settings(self.current_user)
            self.update_ui_from_settings()

    def open_global_settings(self):
        if self.measurement_running:
            QMessageBox.warning(self, "Action Denied", "Cannot change settings while a measurement is running.")
            return

        dialog = SettingsDialog(self.config_manager, parent=self)
        if dialog.exec_():
            self.log_status("Global settings updated.")
            # If a user is selected, reload their effective settings
            if self.current_user:
                 self.user_settings = self.config_manager.get_user_settings(self.current_user)
                 self.update_ui_from_settings()

    def get_widget_for_mode(self, mode: str) -> Optional[QWidget]:
        """Get the main widget for a given mode."""
        if mode == 'resistance': return self.tab_resistance
        if mode == 'source_v': return self.tab_voltage_source
        if mode == 'source_i': return self.tab_current_source
        return None

    def gather_settings_for_mode(self, mode:str) -> Dict:
        """Gathers current settings from UI AND merges with non-UI settings."""
        if not self.user_settings:
            raise ValueError("User settings not loaded.")

        # Start with a copy of the full user settings
        effective_settings = {
            'measurement': dict(self.user_settings['measurement']),
            'display': dict(self.user_settings['display']),
            'file': dict(self.user_settings['file'])
        }
        m_cfg = effective_settings['measurement'] # Get reference

        # Override with current values from the UI for the *active* tab
        widget = self.get_widget_for_mode(mode)
        if not widget:
             raise ValueError(f"Invalid mode specified: {mode}")

        try:
            if mode == 'resistance':
                m_cfg['res_test_current'] = widget.res_test_current.value()
                m_cfg['res_voltage_compliance'] = widget.res_voltage_compliance.value()
                m_cfg['res_measurement_type'] = widget.res_measurement_type.currentText()
                m_cfg['res_auto_range'] = widget.res_auto_range.isChecked()
            elif mode == 'source_v':
                m_cfg['vsource_voltage'] = widget.vsource_voltage.value()
                m_cfg['vsource_current_compliance'] = widget.vsource_current_compliance.value()
                m_cfg['vsource_current_range_auto'] = widget.vsource_current_range_auto.isChecked()
                if hasattr(widget, 'vsource_duration'):
                    m_cfg['vsource_duration_hours'] = widget.vsource_duration.value()
            elif mode == 'source_i':
                m_cfg['isource_current'] = widget.isource_current.value()
                m_cfg['isource_voltage_compliance'] = widget.isource_voltage_compliance.value()
                m_cfg['isource_voltage_range_auto'] = widget.isource_voltage_range_auto.isChecked()
                if hasattr(widget, 'isource_duration'):
                    m_cfg['isource_duration_hours'] = widget.isource_duration.value()
        except AttributeError as e:
             raise ValueError(f"UI Widgets not found for mode {mode}: {e}")


        # Ensure general settings are present (they aren't directly on tabs)
        m_cfg['sampling_rate'] = self.user_settings['measurement']['sampling_rate']
        m_cfg['nplc'] = self.user_settings['measurement']['nplc']
        m_cfg['settling_time'] = self.user_settings['measurement']['settling_time']
        m_cfg['gpib_address'] = self.user_settings['measurement']['gpib_address']

        return effective_settings # Return the full dict

    def start_measurement(self, mode: str):
        """Start the measurement process for the specified mode."""
        if self.measurement_running:
            QMessageBox.warning(self, "Measurement Active", f"A measurement ({self.active_mode}) is already running. Please stop it first.")
            return
        if not self.current_user:
            QMessageBox.warning(self, "No User Selected", "Please select or create a user first.")
            return

        sample_name = self.sample_input.text().strip()
        if not sample_name:
            self.sample_input.setFocus()
            QMessageBox.warning(self, "Sample Name Required", "Please enter a sample name.")
            return

        widget = self.get_widget_for_mode(mode)
        if not widget:
             self.log_status(f"Error: Could not find UI for mode {mode}")
             return

        # Gather settings from UI + config just before starting
        try:
            current_settings = self.gather_settings_for_mode(mode)
        except ValueError as e:
            QMessageBox.critical(self, "Settings Error", f"Failed to gather settings: {e}")
            return

        # --- UI Updates ---
        self.active_mode = mode
        self.measurement_running = True
        self.set_controls_for_mode(mode, running=True) # Disable start, enable stop for this mode
        self.set_all_controls_enabled(False, except_mode=mode) # Disable controls on other tabs
        self.sample_input.setEnabled(False)
        self.change_user_button.setEnabled(False)
        self.shortcut_mark.setEnabled(True)

        # Clear buffer and plot for the specific mode
        self.data_buffers[mode].clear()
        widget.canvas.clear_plot()
        widget.status_label.setText("Status: Running")
        widget.status_label.setStyleSheet("font-weight: bold; color: green;")
        if hasattr(widget, 'mark_event_button'):
             widget.mark_event_button.setEnabled(True)


        # --- Worker Setup ---
        self.log_status(f"Starting {mode} measurement for sample: {sample_name}...")
        self.statusBar().showMessage(f"Measurement running ({mode})...")

        self.measurement_worker = MeasurementWorker(
            mode=mode,
            sample_name=sample_name,
            username=self.current_user,
            settings=current_settings # Pass the combined settings
        )

        # Connect signals from worker
        self.measurement_worker.data_point.connect(self.update_data)
        self.measurement_worker.status_update.connect(self.log_status_from_worker) # Use different slot for worker messages
        self.measurement_worker.measurement_complete.connect(self.on_measurement_complete)
        self.measurement_worker.error_occurred.connect(self.on_error)
        self.measurement_worker.compliance_hit.connect(self.on_compliance_hit)
        self.measurement_worker.finished.connect(self.on_worker_finished) # Cleanup signal

        # Start worker thread
        self.measurement_worker.start()

        # Start plot update timer
        update_interval = current_settings['display']['plot_update_interval']
        if current_settings['display']['enable_plot']:
            self.plot_timer.start(update_interval)
        else:
             self.log_status("Plotting disabled in settings.")

    def stop_current_measurement(self):
        """Stops the currently running measurement."""
        if self.measurement_worker and self.measurement_running:
            self.log_status(f"Attempting to stop {self.active_mode} measurement...")
            self.statusBar().showMessage(f"Stopping {self.active_mode} measurement...")

            # Disable stop button immediately to prevent multiple clicks
            widget = self.get_widget_for_mode(self.active_mode)
            if widget:
                 widget.stop_button.setEnabled(False)
                 widget.status_label.setText("Status: Stopping...")
                 widget.status_label.setStyleSheet("font-weight: bold; color: orange;")
                 if hasattr(widget, 'mark_event_button'):
                      widget.mark_event_button.setEnabled(False)
                 if hasattr(widget, 'pause_button'):
                      widget.pause_button.setEnabled(False)


            self.shortcut_mark.setEnabled(False)
            self.plot_timer.stop() # Stop plot updates

            # Signal the worker thread to stop
            self.measurement_worker.stop_measurement()
            # Don't reset flags here, wait for on_measurement_complete or on_error
        else:
            self.log_status("No measurement currently running.")

    # def pause_resume_measurement(self, pause: bool):
    #     """Handles the pause/resume button toggle."""
    #     if not self.measurement_running or not self.measurement_worker:
    #         return
    #
    #     widget = self.get_widget_for_mode(self.active_mode)
    #     if not widget: return
    #
    #     if pause:
    #         self.measurement_worker.pause_measurement()
    #         widget.pause_button.setText("Resume")
    #         widget.pause_button.setIcon(QIcon.fromTheme("media-playback-start"))
    #         widget.status_label.setText("Status: Paused")
    #         widget.status_label.setStyleSheet("font-weight: bold; color: blue;")
    #     else:
    #         self.measurement_worker.resume_measurement()
    #         widget.pause_button.setText("Pause")
    #         widget.pause_button.setIcon(QIcon.fromTheme("media-playback-pause"))
    #         widget.status_label.setText("Status: Running")
    #         widget.status_label.setStyleSheet("font-weight: bold; color: green;")
    def mark_event_shortcut(self):
        """Marks a generic event for the running measurement."""
        if self.measurement_running and self.measurement_worker:
            self.measurement_worker.mark_event("MARK")
            self.log_status("⭐ Event marked.", color="purple")
            # Optional: visual feedback like flashing the button
            widget = self.get_widget_for_mode(self.active_mode)
            if widget and hasattr(widget, 'mark_event_button'):
                 original_style = widget.mark_event_button.styleSheet()
                 widget.mark_event_button.setStyleSheet("background-color: yellow;")
                 QTimer.singleShot(500, lambda: widget.mark_event_button.setStyleSheet(original_style))


    def update_data(self, timestamp: float, value: Dict[str, float], compliance_status: str, event: str):
        """Slot to receive data points from the worker thread."""
        if not self.measurement_running or self.active_mode is None:
             return # Ignore data if not running or mode unknown

        # Add data to the buffer corresponding to the active mode
        buffer = self.data_buffers[self.active_mode]
        if 'resistance' in value and ('voltage' not in value and 'current' not in value):
            buffer.add_resistance(timestamp, value.get('resistance', float('nan')), compliance_status)
        else:
            buffer.add_voltage_current(timestamp, value.get('voltage', float('nan')), value.get('current', float('nan')), compliance_status)

        # No plotting here, handled by the timer calling update_active_plot

    def update_active_plot(self):
        """Update the plot for the currently active measurement mode."""
        if not self.measurement_running or self.active_mode is None or not self.user_settings:
            return

        mode = self.active_mode
        widget = self.get_widget_for_mode(mode)
        buffer = self.data_buffers[mode]

        if not widget or not buffer:
            return

        if self.user_settings['display']['enable_plot']:
            # Decide which variable to plot per mode
            if mode == 'resistance':
                var = 'resistance'
            elif mode == 'source_v':
                var = widget.v_plot_var.currentText() if hasattr(widget, 'v_plot_var') else 'current'
            else:
                var = widget.i_plot_var.currentText() if hasattr(widget, 'i_plot_var') else 'voltage'

            timestamps, values, compliance_list = buffer.get_data_for_plot(var)
            stats = buffer.get_statistics(var)
            widget.canvas.update_plot(
                timestamps,
                values,
                compliance_list,
                stats,
                self.current_user,
                self.sample_input.text()
            )

    def on_measurement_complete(self, mode: str):
        """Handles the measurement_complete signal from the worker."""
        self.log_status(f"Worker reported measurement complete for mode: {mode}", color="darkGreen")
        self.statusBar().showMessage(f"Measurement ({mode}) completed | Ready", 5000) # Timeout message

        # No need to call worker.stop() here, it finished naturally
        # Worker thread will exit, triggering on_worker_finished for cleanup

    def on_error(self, error_message: str):
        """Handles the error_occurred signal from the worker."""
        self.log_status(f"ERROR: {error_message}", color="red")
        self.statusBar().showMessage(f"Measurement Error ({self.active_mode})", 5000)

        # Ensure timer is stopped
        self.plot_timer.stop()
        # Worker thread will likely exit soon after error, triggering on_worker_finished

        # Show critical message box
        QMessageBox.critical(self, "Measurement Error", error_message)

        # Reset UI state (similar to on_worker_finished, maybe call it?)
        # self.reset_ui_after_measurement() # Call cleanup manually just in case worker hangs? No, wait for finished.


    def on_compliance_hit(self, compliance_type: str):
        """Handles the compliance_hit signal."""
        mode = self.active_mode
        widget = self.get_widget_for_mode(mode)
        if widget:
             widget.status_label.setText(f"Status: {compliance_type.upper()} COMPLIANCE")
             widget.status_label.setStyleSheet("font-weight: bold; color: red;")

        self.log_status(f"⚠️ {compliance_type} Compliance Hit during {mode} measurement!", color="orange")
        QMessageBox.warning(self, f"{compliance_type} Compliance Warning",
                            f"The {compliance_type.lower()} compliance limit was reached during the {mode} measurement.")

    def on_worker_finished(self):
        """Slot connected to the QThread.finished signal."""
        self.log_status(f"Measurement worker thread ({self.active_mode}) finished.", color="grey")
        self.reset_ui_after_measurement()

    def reset_ui_after_measurement(self):
         """Resets the UI state after a measurement stops or completes."""
         if not self.active_mode: # If already reset
              return

         finished_mode = self.active_mode
         self.measurement_running = False
         self.active_mode = None
         self.measurement_worker = None # Clear worker reference

         # Ensure timer is stopped
         self.plot_timer.stop()

         # Re-enable global controls
         self.sample_input.setEnabled(True)
         self.change_user_button.setEnabled(True)

         # Reset the UI state for the tab that was running
         widget = self.get_widget_for_mode(finished_mode)
         if widget:
              widget.status_label.setText("Status: Idle")
              widget.status_label.setStyleSheet("font-weight: bold; color: black;")
              widget.start_button.setEnabled(True)
              widget.stop_button.setEnabled(False)
              if hasattr(widget, 'pause_button'):
                  widget.pause_button.setEnabled(False)
                  widget.pause_button.setChecked(False)
              if hasattr(widget, 'mark_event_button'):
                   widget.mark_event_button.setEnabled(False)


         # Re-enable controls on ALL tabs
         self.set_all_controls_enabled(True)

         self.shortcut_mark.setEnabled(False) # Disable M key generally

         self.statusBar().showMessage("Ready", 0) # Persistent Ready message
         self.log_status("Measurement stopped. UI controls re-enabled.")


    def set_controls_for_mode(self, mode: str, running: bool):
        """Enable/disable controls for a specific mode tab."""
        widget = self.get_widget_for_mode(mode)
        if widget:
             widget.start_button.setEnabled(not running)
             widget.stop_button.setEnabled(running)
             if hasattr(widget, 'pause_button'):
                 widget.pause_button.setEnabled(running)
             # Disable parameter inputs while running
             for i in range(widget.param_layout.rowCount()):
                 field = widget.param_layout.itemAt(i, QFormLayout.FieldRole)
                 if field and field.widget():
                      field.widget().setEnabled(not running)
                 label = widget.param_layout.itemAt(i, QFormLayout.LabelRole)
                 if label and label.widget():
                      label.widget().setEnabled(not running) # Also disable labels? Optional.
             # Enable mark button when running
             if hasattr(widget, 'mark_event_button'):
                  widget.mark_event_button.setEnabled(running)

    def set_all_controls_enabled(self, enabled: bool, except_mode: Optional[str] = None):
         """Enable/disable start buttons and parameters on ALL tabs, optionally skipping one."""
         for mode in ['resistance', 'source_v', 'source_i']:
              if mode == except_mode:
                   continue
              widget = self.get_widget_for_mode(mode)
              if widget:
                   widget.start_button.setEnabled(enabled)
                   widget.stop_button.setEnabled(False) # Stop always disabled if not running
                   if hasattr(widget, 'pause_button'):
                       widget.pause_button.setEnabled(False)
                       widget.pause_button.setChecked(False)
                   if hasattr(widget, 'mark_event_button'):
                       widget.mark_event_button.setEnabled(False)

                   # Enable/disable parameter inputs
                   for i in range(widget.param_layout.rowCount()):
                       field = widget.param_layout.itemAt(i, QFormLayout.FieldRole)
                       if field and field.widget():
                            field.widget().setEnabled(enabled)
                       label = widget.param_layout.itemAt(i, QFormLayout.LabelRole)
                       if label and label.widget():
                           label.widget().setEnabled(enabled)


    def handle_tab_change(self, index):
        """Called when the user switches tabs."""
        # Important: Prevent switching tabs while a measurement is running?
        # Or stop the measurement automatically? Let's prevent it for safety.
        if self.measurement_running:
            current_widget = self.main_tabs.widget(index)
            # Check if the new tab corresponds to the running mode
            if not hasattr(current_widget, 'mode') or current_widget.mode != self.active_mode:
                QMessageBox.warning(self, "Measurement Active",
                                    f"Cannot switch tabs while a measurement ({self.active_mode}) is running. "
                                    "Please stop the current measurement first.")
                # Find the index of the active tab and switch back
                for i in range(self.main_tabs.count()):
                    widget = self.main_tabs.widget(i)
                    if hasattr(widget, 'mode') and widget.mode == self.active_mode:
                        self.main_tabs.blockSignals(True) # Prevent recursion
                        self.main_tabs.setCurrentIndex(i)
                        self.main_tabs.blockSignals(False)
                        break
            # else: switching to the tab that IS running is okay.
        # else: Okay to switch tabs if nothing is running.

    def update_canvas_labels_for_mode(self, mode: str):
        """Update canvas labels based on selected plot variable for the given mode."""
        if not self.user_settings:
            return
        d_cfg = self.user_settings['display']
        widget = self.get_widget_for_mode(mode)
        if not widget:
            return
        if mode == 'source_v':
            var = widget.v_plot_var.currentText()
            color = d_cfg['plot_color_v']
            if var == 'current':
                widget.canvas.set_plot_properties('Elapsed Time (s)', 'Measured Current (A)', 'Voltage Source Output', color)
            elif var == 'voltage':
                widget.canvas.set_plot_properties('Elapsed Time (s)', 'Measured Voltage (V)', 'Voltage Source Output', color)
            else:
                widget.canvas.set_plot_properties('Elapsed Time (s)', 'Resistance (Ohms)', 'Voltage Source Output', color)
        elif mode == 'source_i':
            var = widget.i_plot_var.currentText()
            color = d_cfg['plot_color_i']
            if var == 'voltage':
                widget.canvas.set_plot_properties('Elapsed Time (s)', 'Measured Voltage (V)', 'Current Source Output', color)
            elif var == 'current':
                widget.canvas.set_plot_properties('Elapsed Time (s)', 'Measured Current (A)', 'Current Source Output', color)
            else:
                widget.canvas.set_plot_properties('Elapsed Time (s)', 'Resistance (Ohms)', 'Current Source Output', color)


    def log_status(self, message: str, color: str = "black"):
        """Add a timestamped message to the status display."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        # Use simple HTML for color
        colored_message = f'<font color="{color}">[{timestamp}] {message}</font>'
        self.status_display.append(colored_message)
        # Auto-scroll to bottom
        self.status_display.verticalScrollBar().setValue(self.status_display.verticalScrollBar().maximum())

    def log_status_from_worker(self, message: str):
        """Log status messages coming directly from the worker thread."""
        # Use default color for worker messages unless it's an error/warning
        color = "black"
        if "error" in message.lower(): color="red"
        elif "warn" in message.lower() or "compliance" in message.lower(): color="orange"
        self.log_status(message, color=color)
        # Also update the main status bar briefly
        self.statusBar().showMessage(message, 3000) # Show for 3 seconds


    def save_active_plot(self):
        """Save the plot from the currently visible tab."""
        if self.measurement_running:
             # Maybe allow saving even if running? Or disable? Let's allow it.
             pass # Allow saving while running

        current_tab_widget = self.main_tabs.currentWidget()
        if not hasattr(current_tab_widget, 'canvas'):
             QMessageBox.warning(self, "Save Error", "Could not find plot canvas on the current tab.")
             return

        # Suggest a filename based on sample and mode
        mode = getattr(current_tab_widget, 'mode', 'unknown')
        sample_name = self.sample_input.text().strip().replace(' ','_') or "plot"
        timestamp = int(time.time())
        suggested_filename = f"{timestamp}_{sample_name}_{mode}.png"

        # Default directory could be the data directory or last used location
        default_dir = self.user_settings['file']['data_directory'] if self.user_settings else "."

        filename, selected_filter = QFileDialog.getSaveFileName(
            self, "Save Plot", os.path.join(default_dir, suggested_filename),
             "PNG Files (*.png);;PDF Files (*.pdf);;JPEG Files (*.jpg);;All Files (*)"
        )

        if filename:
            try:
                current_tab_widget.canvas.fig.savefig(filename, dpi=300) # Save with higher DPI
                self.log_status(f"Plot saved to: {filename}", color="blue")
            except Exception as e:
                 QMessageBox.critical(self, "Save Error", f"Failed to save plot: {str(e)}")
                 self.log_status(f"Error saving plot: {str(e)}", color="red")

    def clear_all_plots(self):
        """Clears plots on all tabs."""
        self.tab_resistance.canvas.clear_plot()
        self.tab_voltage_source.canvas.clear_plot()
        self.tab_current_source.canvas.clear_plot()
        self.log_status("All plots cleared.")


    def show_about(self):
        """Show about dialog."""
        about_text = f"""
        <h2>ResistaMet GUI (Tabbed)</h2>
        <p>Version: {__version__}</p>
        <p>Original Author: {__author__.split('(')[0]}</p>
        <hr>
        <p>A graphical interface for controlling Keithley SourceMeasure Units,
        providing modes for:</p>
        <ul>
            <li>Resistance Measurement (Source Current, Measure Resistance)</li>
            <li>Voltage Source (Source Voltage, Measure Current)</li>
            <li>Current Source (Source Current, Measure Voltage)</li>
        </ul>
        <p>Supports real-time plotting, data logging, user profiles, and compliance monitoring.</p>
        """
        QMessageBox.about(self, f"About ResistaMet GUI v{__version__}", about_text)

    def closeEvent(self, event):
        """Handle window close event."""
        if self.measurement_running:
            reply = QMessageBox.question(
                self, "Exit Confirmation",
                f"A measurement ({self.active_mode}) is currently running.\n"
                "Stopping the measurement may result in incomplete data.\n\n"
                "Are you sure you want to exit?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )

            if reply == QMessageBox.Yes:
                self.log_status("Exit requested during measurement. Stopping worker...", color="orange")
                if self.measurement_worker:
                     self.measurement_worker.stop_measurement()
                     # Wait briefly for the worker to potentially clean up?
                     if not self.measurement_worker.wait(2000): # Wait up to 2s
                           self.log_status("Worker did not stop gracefully. Forcing exit.", color="red")
                           # self.measurement_worker.terminate() # Use terminate as last resort - may corrupt data/state
                event.accept()
            else:
                event.ignore()
        else:
            self.log_status("Exiting application.")
            event.accept()


def main():
    """Main application entry point."""
    # Handle high DPI scaling
    if hasattr(Qt, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)

    # Apply a style
    # Available styles depend on the OS: "Fusion", "Windows", "WindowsVista" (Windows only), "Macintosh" (macOS only)
    app.setStyle("Fusion")

    # Optional: Set a dark theme palette (basic example)
    # dark_palette = QPalette()
    # dark_palette.setColor(QPalette.Window, QColor(53, 53, 53))
    # # ... set other colors ...
    # app.setPalette(dark_palette)


    # Ensure SIGINT (Ctrl+C) can be caught if running from terminal
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    window = ResistanceMeterApp()
    window.show()

    try:
        sys.exit(app.exec_())
    except KeyboardInterrupt:
         print("Ctrl+C detected, exiting.")
         # Perform any necessary cleanup if needed upon Ctrl+C

if __name__ == "__main__":
    main()

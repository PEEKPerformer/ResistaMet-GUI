#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Enhanced ResistaMet GUI: Integrated Resistance, Voltage, and Current Measurement System
Based on ResistaMet GUI v1.0.0 by Brenden Ferland

This implementation adds tabbed interface for three measurement modes:
1. Resistance measurement (original functionality)
2. Voltage application (source voltage, measure current)
3. Current application (source current, measure voltage)
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
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
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
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt5.QtGui import QIcon, QFont, QColor, QPalette

# Script version and metadata
__version__ = "2.0.0"  # Enhanced GUI version
__original_version__ = "1.0.0"  # Original GUI version
__author__ = "Brenden Ferland"

# Configuration file
CONFIG_FILE = "config.json"

# Default settings
DEFAULT_SETTINGS = {
    "resistance_mode": {
        "test_current": 1.0e-3,              # Test current in Amperes
        "voltage_compliance": 5.0,           # Voltage compliance in Volts
        "sampling_rate": 10.0,               # Sampling rate in Hz
        "auto_range": True,                  # Enable auto-ranging
        "nplc": 1,                           # Number of power line cycles
        "settling_time": 0.2,                # Settling time in seconds
        "measurement_type": "2-wire",        # Measurement type (2-wire or 4-wire)
        "gpib_address": "GPIB1::3::INSTR"    # GPIB address of the instrument
    },
    "voltage_mode": {
        "voltage": 10.0,                     # Source voltage in Volts
        "current_compliance": 10.0,          # Current compliance in mA
        "sampling_rate": 1.0,                # Sampling rate in Hz
        "duration": 1.0,                     # Duration in hours
        "auto_range": True,                  # Enable auto-ranging
        "nplc": 1,                           # Number of power line cycles
        "settling_time": 0.1,                # Settling time in seconds
        "gpib_address": "GPIB0::24::INSTR"   # GPIB address of the instrument
    },
    "current_mode": {
        "current": 1.0,                      # Source current in mA
        "voltage_compliance": 10.0,          # Voltage compliance in Volts
        "sampling_rate": 1.0,                # Sampling rate in Hz
        "duration": 1.0,                     # Duration in hours
        "auto_range": True,                  # Enable auto-ranging
        "nplc": 1,                           # Number of power line cycles
        "settling_time": 0.1,                # Settling time in seconds
        "gpib_address": "GPIB0::24::INSTR"   # GPIB address of the instrument
    },
    "display": {
        "enable_plot": True,                 # Enable real-time plotting
        "plot_update_interval": 200,         # Plot update interval in milliseconds
        "plot_colors": {                     # Plot line colors
            "resistance": "red",
            "voltage": "blue",
            "current": "green"
        },
        "plot_figsize": [10, 6],             # Plot figure size [width, height]
        "buffer_size": None                  # Data buffer size (None = unlimited)
    },
    "file": {
        "auto_save_interval": 60,            # Auto-save interval in seconds
        "data_directory": "measurement_data" # Base directory for data storage
    },
    "users": [],                            # List of users
    "last_user": None                       # Last active user
}

# ----- Enhanced Data Buffer Class -----
class EnhancedDataBuffer:
    def __init__(self, size: Optional[int] = None):
        self.size = size
        self.reset()
        
    def reset(self):
        """Reset all data in the buffer."""
        if self.size is not None:
            self.timestamps = deque(maxlen=self.size)
            self.resistance = deque(maxlen=self.size)
            self.voltage = deque(maxlen=self.size)
            self.current = deque(maxlen=self.size)
            self.events = deque(maxlen=self.size)
        else:
            self.timestamps = deque()
            self.resistance = deque()
            self.voltage = deque()
            self.current = deque()
            self.events = deque()
            
        self.stats = {
            'resistance': {'min': float('inf'), 'max': float('-inf'), 'avg': 0, 'count': 0},
            'voltage': {'min': float('inf'), 'max': float('-inf'), 'avg': 0, 'count': 0},
            'current': {'min': float('inf'), 'max': float('-inf'), 'avg': 0, 'count': 0}
        }
        
    def add_resistance(self, timestamp: float, resistance: float, event: str = ""):
        """Add resistance measurement to buffer."""
        self.timestamps.append(timestamp)
        self.resistance.append(resistance)
        self.events.append(event)
        
        # Fill other values with None
        self.voltage.append(None)
        self.current.append(None)
        
        # Update statistics if value is valid
        if np.isfinite(resistance) and resistance >= 0:
            stats = self.stats['resistance']
            stats['count'] += 1
            stats['min'] = min(stats['min'], resistance)
            stats['max'] = max(stats['max'], resistance)
            stats['avg'] = (stats['avg'] * (stats['count'] - 1) + resistance) / stats['count']
            
    def add_voltage_current(self, timestamp: float, voltage: float, current: float, event: str = ""):
        """Add voltage and current measurements to buffer."""
        self.timestamps.append(timestamp)
        self.voltage.append(voltage)
        self.current.append(current)
        self.events.append(event)
        
        # Calculate resistance if both values are valid
        if np.isfinite(voltage) and np.isfinite(current) and current != 0:
            resistance = voltage / current
            self.resistance.append(resistance)
            
            # Update resistance statistics
            if resistance >= 0:
                stats = self.stats['resistance']
                stats['count'] += 1
                stats['min'] = min(stats['min'], resistance)
                stats['max'] = max(stats['max'], resistance)
                stats['avg'] = (stats['avg'] * (stats['count'] - 1) + resistance) / stats['count']
        else:
            self.resistance.append(None)
            
        # Update voltage statistics if valid
        if np.isfinite(voltage):
            stats = self.stats['voltage']
            stats['count'] += 1
            stats['min'] = min(stats['min'], voltage)
            stats['max'] = max(stats['max'], voltage)
            stats['avg'] = (stats['avg'] * (stats['count'] - 1) + voltage) / stats['count']
            
        # Update current statistics if valid
        if np.isfinite(current):
            stats = self.stats['current']
            stats['count'] += 1
            stats['min'] = min(stats['min'], current)
            stats['max'] = max(stats['max'], current)
            stats['avg'] = (stats['avg'] * (stats['count'] - 1) + current) / stats['count']
            
    def get_data_for_plot(self, data_type: str = 'resistance'):
        """Get data for plotting."""
        timestamps = list(self.timestamps)
        
        if not timestamps:
            return [], []
            
        # Get elapsed times relative to the first data point
        elapsed_times = [t - timestamps[0] for t in timestamps]
        
        if data_type == 'resistance':
            values = list(self.resistance)
        elif data_type == 'voltage':
            values = list(self.voltage)
        elif data_type == 'current':
            values = list(self.current)
        else:
            values = []
            
        return elapsed_times, values
        
    def get_statistics(self, data_type: str = 'resistance'):
        """Get statistics for the specified data type."""
        if data_type in self.stats:
            return self.stats[data_type]
        return {'min': 0, 'max': 0, 'avg': 0, 'count': 0}
        
    def get_events(self):
        """Get events list."""
        return list(self.events)
        
    def clear(self):
        """Clear all data."""
        self.reset()

# ----- Config Manager Class -----
class ConfigManager:
    def __init__(self, config_file: str = CONFIG_FILE):
        self.config_file = config_file
        self.config = self.load_config()
        
    def load_config(self) -> Dict:
        """Load configuration from file or create with defaults if not exists."""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                    
                # Ensure all required sections and default values exist
                for section, defaults in DEFAULT_SETTINGS.items():
                    if section not in config:
                        config[section] = defaults
                    elif isinstance(defaults, dict):
                        for key, value in defaults.items():
                            if key not in config[section]:
                                config[section][key] = value
                                
                return config
            except Exception as e:
                print(f"Error loading configuration: {str(e)}")
                return dict(DEFAULT_SETTINGS)
        else:
            # Create new config with defaults
            return dict(DEFAULT_SETTINGS)
        
    def save_config(self) -> None:
        """Save configuration to file."""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=4)
        except Exception as e:
            print(f"Error saving configuration: {str(e)}")
            
    def get_user_settings(self, username: str) -> Dict:
        """Get settings for a specific user or global defaults if no user-specific settings exist."""
        user_settings = {}
        
        # Get global settings
        for section in ['resistance_mode', 'voltage_mode', 'current_mode', 'display', 'file']:
            user_settings[section] = dict(self.config[section])
            
        # Override with user-specific settings if they exist
        if 'user_settings' in self.config and username in self.config['user_settings']:
            for section, settings in self.config['user_settings'][username].items():
                if section in user_settings:
                    user_settings[section].update(settings)
        
        return user_settings
    
    def update_user_settings(self, username: str, settings: Dict) -> None:
        """Update settings for a specific user."""
        if 'user_settings' not in self.config:
            self.config['user_settings'] = {}
            
        if username not in self.config['user_settings']:
            self.config['user_settings'][username] = {}
            
        # Update each section provided
        for section, section_settings in settings.items():
            if section in ['resistance_mode', 'voltage_mode', 'current_mode', 'display', 'file']:
                if section not in self.config['user_settings'][username]:
                    self.config['user_settings'][username][section] = {}
                self.config['user_settings'][username][section].update(section_settings)
                
        self.save_config()
        
    def update_global_settings(self, settings: Dict) -> None:
        """Update global default settings."""
        # Update each section provided
        for section, section_settings in settings.items():
            if section in ['resistance_mode', 'voltage_mode', 'current_mode', 'display', 'file']:
                self.config[section].update(section_settings)
                
        self.save_config()
        
    def get_users(self) -> List[str]:
        """Get list of users."""
        return self.config.get('users', [])
    
    def get_last_user(self) -> Optional[str]:
        """Get last active user."""
        return self.config.get('last_user')
    
    def add_user(self, username: str) -> None:
        """Add a new user if they don't already exist."""
        username = username.strip()
        if username and username not in self.config.get('users', []):
            if 'users' not in self.config:
                self.config['users'] = []
            self.config['users'].append(username)
            self.save_config()
            
    def set_last_user(self, username: str) -> None:
        """Set the last used username."""
        if username in self.config.get('users', []):
            self.config['last_user'] = username
            self.save_config()

# ----- Base Measurement Worker -----
class BaseMeasurementWorker(QThread):
    """Base worker thread for measurements."""
    data_point = pyqtSignal(float, dict, str)  # timestamp, data_dict, event
    status_update = pyqtSignal(str)
    measurement_complete = pyqtSignal()
    error_occurred = pyqtSignal(str)
    
    def __init__(self, sample_name, username, settings, parent=None):
        super().__init__(parent)
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
        
    def mark_event(self, event_name: str) -> None:
        """Mark an event point."""
        self.event_marker = event_name
        self.status_update.emit(f"⭐ {event_name} MARKED ⭐")
        
    def pause_measurement(self) -> None:
        """Pause the measurement."""
        self.paused = True
        self.status_update.emit("Measurement paused")
        
    def resume_measurement(self) -> None:
        """Resume the measurement."""
        self.paused = False
        self.status_update.emit("Measurement resumed")
        
    def stop_measurement(self) -> None:
        """Stop the measurement thread."""
        self.running = False
        
    def _cleanup(self) -> None:
        """Clean up resources when measurement is stopped."""
        if self.keithley:
            try:
                self.keithley.write(":OUTP OFF")
                self.keithley.write("*RST")
                self.keithley.close()
            except:
                pass
            self.keithley = None
                
        if self.csvfile:
            try:
                self.csvfile.close()
            except:
                pass
            self.csvfile = None
            
    def _write_metadata(self, params: Dict) -> None:
        """Write metadata to CSV file."""
        if not self.writer:
            return
            
        self.writer.writerow(['Test Parameters'])
        for key, value in params.items():
            self.writer.writerow([key, value])
        self.writer.writerow([])  # Empty row for separation

# ----- Resistance Measurement Worker -----
class ResistanceMeasurementWorker(BaseMeasurementWorker):
    """Worker thread for resistance measurements."""
    
    def run(self):
        self.running = True
        
        try:
            # Extract settings
            measurement_settings = self.settings['resistance_mode']
            file_settings = self.settings['file']
            
            test_current = measurement_settings['test_current']
            voltage_compliance = measurement_settings['voltage_compliance']
            sampling_rate = measurement_settings['sampling_rate']
            auto_range = measurement_settings['auto_range']
            nplc = measurement_settings['nplc']
            settling_time = measurement_settings['settling_time']
            measurement_type = measurement_settings['measurement_type']
            gpib_address = measurement_settings['gpib_address']
            
            auto_save_interval = file_settings['auto_save_interval']
            
            sample_interval = 1.0 / sampling_rate
            
            # Open instrument
            try:
                rm = pyvisa.ResourceManager()
                
                # Check available resources
                available_resources = rm.list_resources()
                if not available_resources:
                    self.error_occurred.emit("No GPIB or other instruments detected!")
                    return
                elif gpib_address not in available_resources:
                    self.error_occurred.emit(f"Configured GPIB address '{gpib_address}' not found!")
                    return
                
                self.keithley = rm.open_resource(gpib_address)
                self.status_update.emit(f"Connected to instrument at {gpib_address}")
                
                line_freq = float(self.keithley.query(":SYST:LFR?"))
            except Exception as e:
                self.error_occurred.emit(f"Error connecting to instrument: {str(e)}")
                return
            
            # Instrument setup
            self.keithley.write("*RST")
            self.keithley.write(":SYST:AZER ON")  # Enable autozero
            
            # Set measurement type (2-wire or 4-wire)
            if measurement_type == "4-wire":
                self.keithley.write(":SYST:RSEN ON")  # 4-wire
            else:
                self.keithley.write(":SYST:RSEN OFF")  # 2-wire (default)
                
            self.keithley.write(":SENS:FUNC 'RES'")
            self.keithley.write(":SENS:RES:MODE MAN")
            
            self.keithley.write(":SOUR:FUNC CURR")
            self.keithley.write(f":SOUR:CURR {test_current}")
            self.keithley.write(f":SENS:VOLT:PROT {voltage_compliance}")
            
            if auto_range:
                self.keithley.write(":SENS:RES:RANG:AUTO ON")
            else:
                estimated_max_resistance = voltage_compliance / test_current
                self.keithley.write(f":SENS:RES:RANG {estimated_max_resistance}")
            
            self.keithley.write(f":SENS:RES:NPLC {nplc}")
            self.keithley.write(":FORM:ELEM RES")
            
            self.keithley.write(":TRIG:COUN 1")
            self.keithley.write(":INIT:CONT ON")

            # Create filename and open CSV file
            filename = self._create_filename("RES")
            self.csvfile = open(filename, 'w', newline='')
            self.writer = csv.writer(self.csvfile)
            
            self.start_time = time.time()
            start_unix_time = int(self.start_time)
            
            # Create metadata by combining all settings
            metadata = {
                'User': self.username,
                'Sample Name': self.sample_name,
                'Test Current (A)': test_current,
                'Voltage Compliance (V)': voltage_compliance,
                'Measurement Type': measurement_type,
                'Sampling Rate (Hz)': sampling_rate,
                'Line Frequency (Hz)': line_freq,
                'NPLC': nplc,
                'Auto Range': 'ON' if auto_range else 'OFF',
                'Start Time (Unix)': start_unix_time,
                'Start Time (Human Readable)': datetime.fromtimestamp(start_unix_time).isoformat(),
                'Software Version': __version__,
                'Author': __author__
            }
            self._write_metadata(metadata)
            
            self.writer.writerow([
                'Timestamp (Unix)', 
                'Elapsed Time (s)', 
                'Resistance (Ohms)', 
                'Event'
            ])
            
            self.keithley.write(":OUTP ON")
            time.sleep(settling_time)
            
            last_save = self.start_time
            last_measurement = 0
            
            while self.running:
                if self.paused:
                    time.sleep(0.1)
                    continue
                    
                now = time.time()
                time_since_last = now - last_measurement
                
                if time_since_last >= sample_interval:
                    resistance = float(self.keithley.query(":READ?"))
                    last_measurement = now
                    
                    if resistance < 0 or resistance > 1e9:
                        resistance = float('nan')
                        self.status_update.emit("Invalid reading detected")
                    
                    now_unix = int(now)
                    elapsed_time = now - self.start_time
                    
                    event = self.event_marker
                    if event:
                        self.event_marker = ""
                    
                    # Write to CSV
                    row_data = [
                        now_unix,
                        f"{elapsed_time:.3f}",
                        f"{resistance:.6e}",
                        event
                    ]
                    
                    self.writer.writerow(row_data)
                    
                    # Emit signal with the data
                    data_dict = {'resistance': resistance, 'voltage': None, 'current': None}
                    self.data_point.emit(now, data_dict, event)
                    
                    # Force save to file at configured interval
                    if now - last_save >= auto_save_interval:
                        self.csvfile.flush()
                        os.fsync(self.csvfile.fileno())
                        last_save = now
                    
                    elapsed_time_formatted = time.strftime("%H:%M:%S", time.gmtime(elapsed_time))
                    self.status_update.emit(
                        f"Running: {elapsed_time_formatted} | Resistance: {resistance:.6f} Ohms"
                    )
                
                time.sleep(0.01)

            # Finalize measurement
            self.writer.writerow([])
            end_unix_time = int(time.time())
            metadata['End Time (Unix)'] = end_unix_time
            metadata['End Time (Human Readable)'] = datetime.fromtimestamp(end_unix_time).isoformat()
            self._write_metadata(metadata)
            
            self.status_update.emit(f"Measurement completed successfully! Data saved to: {filename}")
            self.measurement_complete.emit()

        except Exception as e:
            self.error_occurred.emit(f"Error occurred: {str(e)}")
        
        finally:
            self._cleanup()
            
    def _create_filename(self, prefix: str) -> str:
        """Create filename for data storage."""
        base_dir = Path(self.settings['file']['data_directory'])
        base_dir.mkdir(exist_ok=True)
        
        # Create user-specific subdirectory
        user_dir = base_dir / self.username
        user_dir.mkdir(exist_ok=True)
        
        timestamp = int(time.time())
        sanitized_name = ''.join(c if c.isalnum() else '_' for c in self.sample_name)
        
        # Include measurement type and current in filename
        measurement_type = self.settings['resistance_mode']['measurement_type']
        test_current_ma = self.settings['resistance_mode']['test_current'] * 1000  # Convert to mA
        
        filename = f"{timestamp}_{sanitized_name}_{prefix}_{measurement_type}_{test_current_ma:.1f}mA.csv"
        return user_dir / filename

# ----- Voltage Application Worker -----
class VoltageApplicationWorker(BaseMeasurementWorker):
    """Worker thread for applying voltage and measuring current."""
    
    def run(self):
        self.running = True
        
        try:
            # Extract settings
            voltage_settings = self.settings['voltage_mode']
            file_settings = self.settings['file']
            
            voltage = voltage_settings['voltage']
            current_compliance = voltage_settings['current_compliance']
            sampling_rate = voltage_settings['sampling_rate']
            duration = voltage_settings['duration']
            auto_range = voltage_settings['auto_range']
            nplc = voltage_settings['nplc']
            settling_time = voltage_settings['settling_time']
            gpib_address = voltage_settings['gpib_address']
            
            auto_save_interval = file_settings['auto_save_interval']
            
            sample_interval = 1.0 / sampling_rate
            
            # Open instrument
            try:
                rm = pyvisa.ResourceManager()
                
                # Check available resources
                available_resources = rm.list_resources()
                if not available_resources:
                    self.error_occurred.emit("No GPIB or other instruments detected!")
                    return
                elif gpib_address not in available_resources:
                    self.error_occurred.emit(f"Configured GPIB address '{gpib_address}' not found!")
                    return
                
                self.keithley = rm.open_resource(gpib_address)
                self.status_update.emit(f"Connected to instrument at {gpib_address}")
                
                line_freq = float(self.keithley.query(":SYST:LFR?"))
            except Exception as e:
                self.error_occurred.emit(f"Error connecting to instrument: {str(e)}")
                return
                
            # Validate inputs
            if not -200 <= voltage <= 200:
                self.error_occurred.emit("Voltage must be between -200V and 200V")
                return
            if not 0 < duration <= 168:  # max 1 week
                self.error_occurred.emit("Duration must be between 0 and 168 hours")
                return
            if not 0 < current_compliance <= 1000:
                self.error_occurred.emit("Compliance must be between 0 and 1000 mA")
                return
                
            # Initialize instrument with optimal settings
            self.keithley.write("*RST")
            self.keithley.write(":SYST:AZER ON")  # Enable autozero
            self.keithley.write(f":SENS:CURR:NPLC {nplc}")  # Set integration time
            self.keithley.write(":SOUR:FUNC VOLT")
            self.keithley.write(f":SOUR:VOLT {voltage}")
            self.keithley.write(f":SENS:CURR:PROT {current_compliance * 1e-3}")  # Convert to A
            self.keithley.write(":SENS:FUNC 'CURR'")
            
            if auto_range:
                self.keithley.write(":SENS:CURR:RANG:AUTO ON")
            
            self.keithley.write(":FORM:ELEM CURR,VOLT")
            self.keithley.write(":TRIG:COUN 1")
            self.keithley.write(":INIT:CONT ON")
            
            # Create filename and open CSV file
            filename = self._create_filename("VOLT")
            self.csvfile = open(filename, 'w', newline='')
            self.writer = csv.writer(self.csvfile)
            
            self.start_time = time.time()
            start_unix_time = int(self.start_time)
            
            # Create metadata
            metadata = {
                'User': self.username,
                'Sample Name': self.sample_name,
                'Applied Voltage (V)': voltage,
                'Current Compliance (mA)': current_compliance,
                'Duration (hours)': duration,
                'Sampling Rate (Hz)': sampling_rate,
                'Line Frequency (Hz)': line_freq,
                'NPLC': nplc,
                'Auto Range': 'ON' if auto_range else 'OFF',
                'Start Time (Unix)': start_unix_time,
                'Start Time (Human Readable)': datetime.fromtimestamp(start_unix_time).isoformat(),
                'Software Version': __version__,
                'Author': __author__
            }
            self._write_metadata(metadata)
            
            # Write header
            self.writer.writerow([
                'Timestamp (Unix)', 
                'Elapsed Time (s)', 
                'Voltage (V)', 
                'Current (A)', 
                'Resistance (Ohms)', 
                'Event'
            ])
            
            # Turn on output
            self.keithley.write(":OUTP ON")
            time.sleep(settling_time)
            
            last_save = self.start_time
            last_measurement = 0
            end_time = self.start_time + duration * 3600  # Convert hours to seconds
            
            while time.time() < end_time and self.running:
                if self.paused:
                    time.sleep(0.1)
                    continue
                    
                now = time.time()
                time_since_last = now - last_measurement
                
                if time_since_last >= sample_interval:
                    # Read both current and voltage
                    results = self.keithley.query(":READ?").strip().split(',')
                    current = float(results[0])
                    measured_voltage = float(results[1]) if len(results) > 1 else voltage
                    
                    last_measurement = now
                    
                    # Calculate resistance
                    resistance = measured_voltage / current if current != 0 else float('inf')
                    
                    now_unix = int(now)
                    elapsed_time = now - self.start_time
                    
                    event = self.event_marker
                    if event:
                        self.event_marker = ""
                    
                    # Write to CSV
                    row_data = [
                        now_unix,
                        f"{elapsed_time:.3f}",
                        f"{measured_voltage:.6e}",
                        f"{current:.6e}",
                        f"{resistance:.6e}",
                        event
                    ]
                    
                    self.writer.writerow(row_data)
                    
                    # Emit signal with the data
                    data_dict = {
                        'voltage': measured_voltage, 
                        'current': current, 
                        'resistance': resistance
                    }
                    self.data_point.emit(now, data_dict, event)
                    
                    # Force save to file at configured interval
                    if now - last_save >= auto_save_interval:
                        self.csvfile.flush()
                        os.fsync(self.csvfile.fileno())
                        last_save = now
                    
                    # Calculate progress
                    progress = (elapsed_time / (duration * 3600)) * 100
                    elapsed_time_formatted = time.strftime("%H:%M:%S", time.gmtime(elapsed_time))
                    
                    self.status_update.emit(
                        f"Progress: {progress:.1f}% | Time: {elapsed_time_formatted} | "
                        f"Current: {current:.6e}A | Resistance: {resistance:.6f} Ohms"
                    )
                
                time.sleep(0.01)
                
            # Finalize measurement
            self.writer.writerow([])
            end_unix_time = int(time.time())
            metadata['End Time (Unix)'] = end_unix_time
            metadata['End Time (Human Readable)'] = datetime.fromtimestamp(end_unix_time).isoformat()
            self._write_metadata(metadata)
            
            self.status_update.emit(f"Measurement completed successfully! Data saved to: {filename}")
            self.measurement_complete.emit()
            
        except Exception as e:
            self.error_occurred.emit(f"Error occurred: {str(e)}")
            
        finally:
            self._cleanup()
            
    def _create_filename(self, prefix: str) -> str:
        """Create filename for data storage."""
        base_dir = Path(self.settings['file']['data_directory'])
        base_dir.mkdir(exist_ok=True)
        
        # Create user-specific subdirectory
        user_dir = base_dir / self.username
        user_dir.mkdir(exist_ok=True)
        
        timestamp = int(time.time())
        sanitized_name = ''.join(c if c.isalnum() else '_' for c in self.sample_name)
        
        # Include voltage and compliance in filename
        voltage = self.settings['voltage_mode']['voltage']
        compliance = self.settings['voltage_mode']['current_compliance']
        
        filename = f"{timestamp}_{sanitized_name}_{prefix}_V{voltage:.1f}_C{compliance:.1f}mA.csv"
        return user_dir / filename

# ----- Current Application Worker -----
class CurrentApplicationWorker(BaseMeasurementWorker):
    """Worker thread for applying current and measuring voltage."""
    
    def run(self):
        self.running = True
        
        try:
            # Extract settings
            current_settings = self.settings['current_mode']
            file_settings = self.settings['file']
            
            current = current_settings['current'] * 1e-3  # Convert mA to A
            voltage_compliance = current_settings['voltage_compliance']
            sampling_rate = current_settings['sampling_rate']
            duration = current_settings['duration']
            auto_range = current_settings['auto_range']
            nplc = current_settings['nplc']
            settling_time = current_settings['settling_time']
            gpib_address = current_settings['gpib_address']
            
            auto_save_interval = file_settings['auto_save_interval']
            
            sample_interval = 1.0 / sampling_rate
            
            # Open instrument
            try:
                rm = pyvisa.ResourceManager()
                
                # Check available resources
                available_resources = rm.list_resources()
                if not available_resources:
                    self.error_occurred.emit("No GPIB or other instruments detected!")
                    return
                elif gpib_address not in available_resources:
                    self.error_occurred.emit(f"Configured GPIB address '{gpib_address}' not found!")
                    return
                
                self.keithley = rm.open_resource(gpib_address)
                self.status_update.emit(f"Connected to instrument at {gpib_address}")
                
                line_freq = float(self.keithley.query(":SYST:LFR?"))
            except Exception as e:
                self.error_occurred.emit(f"Error connecting to instrument: {str(e)}")
                return
                
            # Validate inputs
            if not -1 <= current <= 1:  # ±1A should be a reasonable limit
                self.error_occurred.emit("Current must be between -1A and 1A")
                return
            if not 0 < duration <= 168:  # max 1 week
                self.error_occurred.emit("Duration must be between 0 and 168 hours")
                return
            if not 0 < voltage_compliance <= 200:
                self.error_occurred.emit("Voltage compliance must be between 0 and 200V")
                return
                
            # Initialize instrument with optimal settings
            self.keithley.write("*RST")
            self.keithley.write(":SYST:AZER ON")  # Enable autozero
            self.keithley.write(f":SENS:VOLT:NPLC {nplc}")  # Set integration time
            self.keithley.write(":SOUR:FUNC CURR")
            self.keithley.write(f":SOUR:CURR {current}")
            self.keithley.write(f":SENS:VOLT:PROT {voltage_compliance}")
            self.keithley.write(":SENS:FUNC 'VOLT'")
            
            if auto_range:
                self.keithley.write(":SENS:VOLT:RANG:AUTO ON")
            
            self.keithley.write(":FORM:ELEM VOLT,CURR")
            self.keithley.write(":TRIG:COUN 1")
            self.keithley.write(":INIT:CONT ON")
            
            # Create filename and open CSV file
            filename = self._create_filename("CURR")
            self.csvfile = open(filename, 'w', newline='')
            self.writer = csv.writer(self.csvfile)
            
            self.start_time = time.time()
            start_unix_time = int(self.start_time)
            
            # Create metadata
            metadata = {
                'User': self.username,
                'Sample Name': self.sample_name,
                'Applied Current (A)': current,
                'Voltage Compliance (V)': voltage_compliance,
                'Duration (hours)': duration,
                'Sampling Rate (Hz)': sampling_rate,
                'Line Frequency (Hz)': line_freq,
                'NPLC': nplc,
                'Auto Range': 'ON' if auto_range else 'OFF',
                'Start Time (Unix)': start_unix_time,
                'Start Time (Human Readable)': datetime.fromtimestamp(start_unix_time).isoformat(),
                'Software Version': __version__,
                'Author': __author__
            }
            self._write_metadata(metadata)
            
            # Write header
            self.writer.writerow([
                'Timestamp (Unix)', 
                'Elapsed Time (s)', 
                'Voltage (V)', 
                'Current (A)', 
                'Resistance (Ohms)', 
                'Event'
            ])
            
            # Turn on output
            self.keithley.write(":OUTP ON")
            time.sleep(settling_time)
            
            last_save = self.start_time
            last_measurement = 0
            end_time = self.start_time + duration * 3600  # Convert hours to seconds
            
            while time.time() < end_time and self.running:
                if self.paused:
                    time.sleep(0.1)
                    continue
                    
                now = time.time()
                time_since_last = now - last_measurement
                
                if time_since_last >= sample_interval:
                    # Read both voltage and current
                    results = self.keithley.query(":READ?").strip().split(',')
                    voltage = float(results[0])
                    measured_current = float(results[1]) if len(results) > 1 else current
                    
                    last_measurement = now
                    
                    # Calculate resistance
                    resistance = voltage / measured_current if measured_current != 0 else float('inf')
                    
                    now_unix = int(now)
                    elapsed_time = now - self.start_time
                    
                    event = self.event_marker
                    if event:
                        self.event_marker = ""
                    
                    # Write to CSV
                    row_data = [
                        now_unix,
                        f"{elapsed_time:.3f}",
                        f"{voltage:.6e}",
                        f"{measured_current:.6e}",
                        f"{resistance:.6e}",
                        event
                    ]
                    
                    self.writer.writerow(row_data)
                    
                    # Emit signal with the data
                    data_dict = {
                        'voltage': voltage, 
                        'current': measured_current, 
                        'resistance': resistance
                    }
                    self.data_point.emit(now, data_dict, event)
                    
                    # Force save to file at configured interval
                    if now - last_save >= auto_save_interval:
                        self.csvfile.flush()
                        os.fsync(self.csvfile.fileno())
                        last_save = now
                    
                    # Calculate progress
                    progress = (elapsed_time / (duration * 3600)) * 100
                    elapsed_time_formatted = time.strftime("%H:%M:%S", time.gmtime(elapsed_time))
                    
                    self.status_update.emit(
                        f"Progress: {progress:.1f}% | Time: {elapsed_time_formatted} | "
                        f"Voltage: {voltage:.6e}V | Resistance: {resistance:.6f} Ohms"
                    )
                
                time.sleep(0.01)
                
            # Finalize measurement
            self.writer.writerow([])
            end_unix_time = int(time.time())
            metadata['End Time (Unix)'] = end_unix_time
            metadata['End Time (Human Readable)'] = datetime.fromtimestamp(end_unix_time).isoformat()
            self._write_metadata(metadata)
            
            self.status_update.emit(f"Measurement completed successfully! Data saved to: {filename}")
            self.measurement_complete.emit()
            
        except Exception as e:
            self.error_occurred.emit(f"Error occurred: {str(e)}")
            
        finally:
            self._cleanup()
            
    def _create_filename(self, prefix: str) -> str:
        """Create filename for data storage."""
        base_dir = Path(self.settings['file']['data_directory'])
        base_dir.mkdir(exist_ok=True)
        
        # Create user-specific subdirectory
        user_dir = base_dir / self.username
        user_dir.mkdir(exist_ok=True)
        
        timestamp = int(time.time())
        sanitized_name = ''.join(c if c.isalnum() else '_' for c in self.sample_name)
        
        # Include current and compliance in filename
        current_ma = self.settings['current_mode']['current']
        compliance = self.settings['current_mode']['voltage_compliance']
        
        filename = f"{timestamp}_{sanitized_name}_{prefix}_I{current_ma:.1f}mA_V{compliance:.1f}V.csv"
        return user_dir / filename

# ----- Enhanced Plot Canvas -----
class EnhancedMplCanvas(FigureCanvas):
    """Enhanced Matplotlib canvas with support for multiple data types."""
    def __init__(self, parent=None, width=10, height=6, dpi=100):
        self.fig = Figure(figsize=(width, height), dpi=dpi)
        self.axes = self.fig.add_subplot(111)
        self.axes.ticklabel_format(useOffset=False, style='plain')
        
        super(EnhancedMplCanvas, self).__init__(self.fig)
        
        # Create initial empty plot
        self.axes.set_title('Measurement Data')
        self.axes.set_xlabel('Elapsed Time (s)')
        self.axes.set_ylabel('Value')
        self.axes.grid(True)
        
        # Create lines for different data types
        self.lines = {
            'resistance': self.axes.plot([], [], 'r-', label='Resistance (Ω)')[0],
            'voltage': self.axes.plot([], [], 'b-', label='Voltage (V)')[0],
            'current': self.axes.plot([], [], 'g-', label='Current (A)')[0]
        }
        
        # Current visible line
        self.visible_data = 'resistance'
        
        # Hide all lines initially
        for line in self.lines.values():
            line.set_visible(False)
            
        # Show default line
        self.lines[self.visible_data].set_visible(True)
        
        # Add min/max/avg text annotations
        self.min_text = self.axes.text(0.02, 0.95, 'Min: -- Ω', 
                               transform=self.axes.transAxes, 
                               ha='left', va='center')
        self.max_text = self.axes.text(0.02, 0.90, 'Max: -- Ω', 
                               transform=self.axes.transAxes, 
                               ha='left', va='center')
        self.avg_text = self.axes.text(0.02, 0.85, 'Avg: -- Ω', 
                               transform=self.axes.transAxes, 
                               ha='left', va='center')
        self.info_text = self.axes.text(0.98, 0.95, 'User: --\nSample: --\nMode: --', 
                                transform=self.axes.transAxes, 
                                ha='right', va='center')

        # Add bbox
        bbox_props = dict(boxstyle="round", fc="white", ec="black", alpha=0.5)
        self.min_text.set_bbox(bbox_props)
        self.max_text.set_bbox(bbox_props)
        self.avg_text.set_bbox(bbox_props)
        self.info_text.set_bbox(bbox_props)

        # Add legend
        self.axes.legend(loc='upper right')
        
        # Adjust layout
        self.fig.tight_layout()
        
    def update_plot(self, timestamps, data_values, stats, username, sample_name, mode, data_type='resistance'):
        """Update the plot with new data."""
        if not timestamps or not data_values:
            return
            
        # Set the visible data type
        self.visible_data = data_type
        
        # Hide all lines
        for line in self.lines.values():
            line.set_visible(False)
            
        # Show selected line and set data
        self.lines[data_type].set_visible(True)
        self.lines[data_type].set_data(timestamps, data_values)
        
        # Update axis limits
        self.axes.relim()
        self.axes.autoscale_view(True, True, True)
        
        # Update y-axis label based on data type
        if data_type == 'resistance':
            self.axes.set_ylabel('Resistance (Ω)')
            unit = 'Ω'
        elif data_type == 'voltage':
            self.axes.set_ylabel('Voltage (V)')
            unit = 'V'
        elif data_type == 'current':
            self.axes.set_ylabel('Current (A)')
            unit = 'A'
            
        # Update statistics text
        if np.isfinite(stats['min']) and np.isfinite(stats['max']):
            self.min_text.set_text(f'Min: {stats["min"]:.6g} {unit}')
            self.max_text.set_text(f'Max: {stats["max"]:.6g} {unit}')
            self.avg_text.set_text(f'Avg: {stats["avg"]:.6g} {unit}')
        else:
            self.min_text.set_text(f'Min: -- {unit}')
            self.max_text.set_text(f'Max: -- {unit}')
            self.avg_text.set_text(f'Avg: -- {unit}')
            
        self.info_text.set_text(f'User: {username}\nSample: {sample_name}\nMode: {mode}')
        
        # Draw the canvas
        self.draw()
        
    def set_plot_color(self, data_type, color):
        """Set the plot line color for the specified data type."""
        if data_type in self.lines:
            self.lines[data_type].set_color(color)
            self.draw()
            
    def clear_plot(self):
        """Clear the plot."""
        for line in self.lines.values():
            line.set_data([], [])
            
        self.min_text.set_text('Min: -- Ω')
        self.max_text.set_text('Max: -- Ω')
        self.avg_text.set_text('Avg: -- Ω')
        self.info_text.set_text('User: --\nSample: --\nMode: --')
        self.draw()

# ----- Settings Dialog -----
class SettingsDialog(QDialog):
    """Dialog for editing configuration settings."""
    def __init__(self, config_manager, username=None, parent=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self.username = username
        
        if username:
            self.settings = config_manager.get_user_settings(username)
            self.setWindowTitle(f"Settings for {username}")
        else:
            self.settings = {
                'resistance_mode': dict(config_manager.config['resistance_mode']),
                'voltage_mode': dict(config_manager.config['voltage_mode']),
                'current_mode': dict(config_manager.config['current_mode']),
                'display': dict(config_manager.config['display']),
                'file': dict(config_manager.config['file'])
            }
            self.setWindowTitle("Global Settings")
            
        self.init_ui()
        
    def init_ui(self):
        """Initialize the UI."""
        self.setMinimumWidth(600)
        
        # Create tab widget
        self.tabs = QTabWidget()
        
        # Create tabs
        self.resistance_tab = self.create_resistance_tab()
        self.voltage_tab = self.create_voltage_tab()
        self.current_tab = self.create_current_tab()
        self.display_tab = self.create_display_tab()
        self.file_tab = self.create_file_tab()
        
        # Add tabs to widget
        self.tabs.addTab(self.resistance_tab, "Resistance Mode")
        self.tabs.addTab(self.voltage_tab, "Voltage Mode")
        self.tabs.addTab(self.current_tab, "Current Mode")
        self.tabs.addTab(self.display_tab, "Display")
        self.tabs.addTab(self.file_tab, "File")
        
        # Create buttons
        self.save_button = QPushButton("Save")
        self.cancel_button = QPushButton("Cancel")
        
        # Connect signals
        self.save_button.clicked.connect(self.save_settings)
        self.cancel_button.clicked.connect(self.reject)
        
        # Create button layout
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.cancel_button)
        
        # Create main layout
        main_layout = QVBoxLayout()
        main_layout.addWidget(self.tabs)
        main_layout.addLayout(button_layout)
        
        self.setLayout(main_layout)
        
    def create_resistance_tab(self):
        """Create the resistance measurement settings tab."""
        tab = QWidget()
        layout = QFormLayout()
        
        # Test current
        self.res_test_current = QDoubleSpinBox()
        self.res_test_current.setRange(0.0001, 1.0)
        self.res_test_current.setDecimals(6)
        self.res_test_current.setSingleStep(0.001)
        self.res_test_current.setValue(self.settings['resistance_mode']['test_current'])
        layout.addRow("Test Current (A):", self.res_test_current)
        
        # Voltage compliance
        self.res_voltage_compliance = QDoubleSpinBox()
        self.res_voltage_compliance.setRange(0.1, 100.0)
        self.res_voltage_compliance.setDecimals(2)
        self.res_voltage_compliance.setSingleStep(0.1)
        self.res_voltage_compliance.setValue(self.settings['resistance_mode']['voltage_compliance'])
        layout.addRow("Voltage Compliance (V):", self.res_voltage_compliance)
        
        # Sampling rate
        self.res_sampling_rate = QDoubleSpinBox()
        self.res_sampling_rate.setRange(0.1, 100.0)
        self.res_sampling_rate.setDecimals(1)
        self.res_sampling_rate.setSingleStep(1.0)
        self.res_sampling_rate.setValue(self.settings['resistance_mode']['sampling_rate'])
        layout.addRow("Sampling Rate (Hz):", self.res_sampling_rate)
        
        # Auto-range
        self.res_auto_range = QCheckBox()
        self.res_auto_range.setChecked(self.settings['resistance_mode']['auto_range'])
        layout.addRow("Auto Range:", self.res_auto_range)
        
        # NPLC
        self.res_nplc = QDoubleSpinBox()
        self.res_nplc.setRange(0.01, 10.0)
        self.res_nplc.setDecimals(2)
        self.res_nplc.setSingleStep(0.1)
        self.res_nplc.setValue(self.settings['resistance_mode']['nplc'])
        layout.addRow("NPLC:", self.res_nplc)
        
        # Settling time
        self.res_settling_time = QDoubleSpinBox()
        self.res_settling_time.setRange(0.0, 10.0)
        self.res_settling_time.setDecimals(2)
        self.res_settling_time.setSingleStep(0.1)
        self.res_settling_time.setValue(self.settings['resistance_mode']['settling_time'])
        layout.addRow("Settling Time (s):", self.res_settling_time)
        
        # Measurement type
        self.res_measurement_type = QComboBox()
        self.res_measurement_type.addItems(["2-wire", "4-wire"])
        self.res_measurement_type.setCurrentText(self.settings['resistance_mode']['measurement_type'])
        layout.addRow("Measurement Type:", self.res_measurement_type)
        
        # GPIB address
        self.res_gpib_address = QLineEdit()
        self.res_gpib_address.setText(self.settings['resistance_mode']['gpib_address'])
        layout.addRow("GPIB Address:", self.res_gpib_address)
        
        # Detect GPIB button
        self.res_detect_gpib_button = QPushButton("Detect GPIB Devices")
        self.res_detect_gpib_button.clicked.connect(lambda: self.detect_gpib_devices(self.res_gpib_address))
        layout.addRow("", self.res_detect_gpib_button)
        
        tab.setLayout(layout)
        return tab
        
    def create_voltage_tab(self):
        """Create the voltage application settings tab."""
        tab = QWidget()
        layout = QFormLayout()
        
        # Voltage
        self.volt_voltage = QDoubleSpinBox()
        self.volt_voltage.setRange(-200.0, 200.0)
        self.volt_voltage.setDecimals(3)
        self.volt_voltage.setSingleStep(1.0)
        self.volt_voltage.setValue(self.settings['voltage_mode']['voltage'])
        layout.addRow("Applied Voltage (V):", self.volt_voltage)
        
        # Current compliance
        self.volt_current_compliance = QDoubleSpinBox()
        self.volt_current_compliance.setRange(0.001, 1000.0)
        self.volt_current_compliance.setDecimals(3)
        self.volt_current_compliance.setSingleStep(1.0)
        self.volt_current_compliance.setValue(self.settings['voltage_mode']['current_compliance'])
        layout.addRow("Current Compliance (mA):", self.volt_current_compliance)
        
        # Duration
        self.volt_duration = QDoubleSpinBox()
        self.volt_duration.setRange(0.01, 168.0)
        self.volt_duration.setDecimals(2)
        self.volt_duration.setSingleStep(0.5)
        self.volt_duration.setValue(self.settings['voltage_mode']['duration'])
        layout.addRow("Duration (hours):", self.volt_duration)
        
        # Sampling rate
        self.volt_sampling_rate = QDoubleSpinBox()
        self.volt_sampling_rate.setRange(0.01, 100.0)
        self.volt_sampling_rate.setDecimals(2)
        self.volt_sampling_rate.setSingleStep(0.1)
        self.volt_sampling_rate.setValue(self.settings['voltage_mode']['sampling_rate'])
        layout.addRow("Sampling Rate (Hz):", self.volt_sampling_rate)
        
        # Auto-range
        self.volt_auto_range = QCheckBox()
        self.volt_auto_range.setChecked(self.settings['voltage_mode']['auto_range'])
        layout.addRow("Auto Range:", self.volt_auto_range)
        
        # NPLC
        self.volt_nplc = QDoubleSpinBox()
        self.volt_nplc.setRange(0.01, 10.0)
        self.volt_nplc.setDecimals(2)
        self.volt_nplc.setSingleStep(0.1)
        self.volt_nplc.setValue(self.settings['voltage_mode']['nplc'])
        layout.addRow("NPLC:", self.volt_nplc)
        
        # Settling time
        self.volt_settling_time = QDoubleSpinBox()
        self.volt_settling_time.setRange(0.0, 10.0)
        self.volt_settling_time.setDecimals(2)
        self.volt_settling_time.setSingleStep(0.1)
        self.volt_settling_time.setValue(self.settings['voltage_mode']['settling_time'])
        layout.addRow("Settling Time (s):", self.volt_settling_time)
        
        # GPIB address
        self.volt_gpib_address = QLineEdit()
        self.volt_gpib_address.setText(self.settings['voltage_mode']['gpib_address'])
        layout.addRow("GPIB Address:", self.volt_gpib_address)
        
        # Detect GPIB button
        self.volt_detect_gpib_button = QPushButton("Detect GPIB Devices")
        self.volt_detect_gpib_button.clicked.connect(lambda: self.detect_gpib_devices(self.volt_gpib_address))
        layout.addRow("", self.volt_detect_gpib_button)
        
        tab.setLayout(layout)
        return tab
        
    def create_current_tab(self):
        """Create the current application settings tab."""
        tab = QWidget()
        layout = QFormLayout()
        
        # Current
        self.curr_current = QDoubleSpinBox()
        self.curr_current.setRange(-1000.0, 1000.0)
        self.curr_current.setDecimals(3)
        self.curr_current.setSingleStep(1.0)
        self.curr_current.setValue(self.settings['current_mode']['current'])
        layout.addRow("Applied Current (mA):", self.curr_current)
        
        # Voltage compliance
        self.curr_voltage_compliance = QDoubleSpinBox()
        self.curr_voltage_compliance.setRange(0.1, 200.0)
        self.curr_voltage_compliance.setDecimals(3)
        self.curr_voltage_compliance.setSingleStep(1.0)
        self.curr_voltage_compliance.setValue(self.settings['current_mode']['voltage_compliance'])
        layout.addRow("Voltage Compliance (V):", self.curr_voltage_compliance)
        
        # Duration
        self.curr_duration = QDoubleSpinBox()
        self.curr_duration.setRange(0.01, 168.0)
        self.curr_duration.setDecimals(2)
        self.curr_duration.setSingleStep(0.5)
        self.curr_duration.setValue(self.settings['current_mode']['duration'])
        layout.addRow("Duration (hours):", self.curr_duration)
        
        # Sampling rate
        self.curr_sampling_rate = QDoubleSpinBox()
        self.curr_sampling_rate.setRange(0.01, 100.0)
        self.curr_sampling_rate.setDecimals(2)
        self.curr_sampling_rate.setSingleStep(0.1)
        self.curr_sampling_rate.setValue(self.settings['current_mode']['sampling_rate'])
        layout.addRow("Sampling Rate (Hz):", self.curr_sampling_rate)
        
        # Auto-range
        self.curr_auto_range = QCheckBox()
        self.curr_auto_range.setChecked(self.settings['current_mode']['auto_range'])
        layout.addRow("Auto Range:", self.curr_auto_range)
        
        # NPLC
        self.curr_nplc = QDoubleSpinBox()
        self.curr_nplc.setRange(0.01, 10.0)
        self.curr_nplc.setDecimals(2)
        self.curr_nplc.setSingleStep(0.1)
        self.curr_nplc.setValue(self.settings['current_mode']['nplc'])
        layout.addRow("NPLC:", self.curr_nplc)
        
        # Settling time
        self.curr_settling_time = QDoubleSpinBox()
        self.curr_settling_time.setRange(0.0, 10.0)
        self.curr_settling_time.setDecimals(2)
        self.curr_settling_time.setSingleStep(0.1)
        self.curr_settling_time.setValue(self.settings['current_mode']['settling_time'])
        layout.addRow("Settling Time (s):", self.curr_settling_time)
        
        # GPIB address
        self.curr_gpib_address = QLineEdit()
        self.curr_gpib_address.setText(self.settings['current_mode']['gpib_address'])
        layout.addRow("GPIB Address:", self.curr_gpib_address)
        
        # Detect GPIB button
        self.curr_detect_gpib_button = QPushButton("Detect GPIB Devices")
        self.curr_detect_gpib_button.clicked.connect(lambda: self.detect_gpib_devices(self.curr_gpib_address))
        layout.addRow("", self.curr_detect_gpib_button)
        
        tab.setLayout(layout)
        return tab
        
    def create_display_tab(self):
        """Create the display settings tab."""
        tab = QWidget()
        layout = QFormLayout()
        
        # Enable plot
        self.enable_plot = QCheckBox()
        self.enable_plot.setChecked(self.settings['display']['enable_plot'])
        layout.addRow("Enable Real-time Plot:", self.enable_plot)
        
        # Plot update interval
        self.plot_update_interval = QSpinBox()
        self.plot_update_interval.setRange(50, 2000)
        self.plot_update_interval.setSingleStep(50)
        self.plot_update_interval.setValue(self.settings['display']['plot_update_interval'])
        layout.addRow("Plot Update Interval (ms):", self.plot_update_interval)
        
        # Plot colors
        colors = ["red", "blue", "green", "black", "purple", "orange", "darkblue", "darkred"]
        
        # Resistance color
        self.resistance_color = QComboBox()
        self.resistance_color.addItems(colors)
        resistance_color = self.settings['display']['plot_colors']['resistance']
        if resistance_color in colors:
            self.resistance_color.setCurrentText(resistance_color)
        layout.addRow("Resistance Plot Color:", self.resistance_color)
        
        # Voltage color
        self.voltage_color = QComboBox()
        self.voltage_color.addItems(colors)
        voltage_color = self.settings['display']['plot_colors']['voltage']
        if voltage_color in colors:
            self.voltage_color.setCurrentText(voltage_color)
        layout.addRow("Voltage Plot Color:", self.voltage_color)
        
        # Current color
        self.current_color = QComboBox()
        self.current_color.addItems(colors)
        current_color = self.settings['display']['plot_colors']['current']
        if current_color in colors:
            self.current_color.setCurrentText(current_color)
        layout.addRow("Current Plot Color:", self.current_color)
        
        # Plot figure size
        figsize_layout = QHBoxLayout()
        self.plot_width = QDoubleSpinBox()
        self.plot_width.setRange(4, 20)
        self.plot_width.setSingleStep(0.5)
        self.plot_width.setValue(self.settings['display']['plot_figsize'][0])
        
        self.plot_height = QDoubleSpinBox()
        self.plot_height.setRange(3, 15)
        self.plot_height.setSingleStep(0.5)
        self.plot_height.setValue(self.settings['display']['plot_figsize'][1])
        
        figsize_layout.addWidget(QLabel("Width:"))
        figsize_layout.addWidget(self.plot_width)
        figsize_layout.addWidget(QLabel("Height:"))
        figsize_layout.addWidget(self.plot_height)
        layout.addRow("Plot Figure Size:", figsize_layout)
        
        # Buffer size
        self.buffer_size = QSpinBox()
        self.buffer_size.setRange(0, 100000)
        self.buffer_size.setSingleStep(100)
        buffer_size = self.settings['display']['buffer_size']
        self.buffer_size.setValue(0 if buffer_size is None else buffer_size)
        self.buffer_size.setSpecialValueText("Unlimited")
        layout.addRow("Buffer Size (points):", self.buffer_size)
        
        tab.setLayout(layout)
        return tab
        
    def create_file_tab(self):
        """Create the file settings tab."""
        tab = QWidget()
        layout = QFormLayout()
        
        # Auto-save interval
        self.auto_save_interval = QSpinBox()
        self.auto_save_interval.setRange(1, 3600)
        self.auto_save_interval.setSingleStep(10)
        self.auto_save_interval.setValue(self.settings['file']['auto_save_interval'])
        layout.addRow("Auto-save Interval (s):", self.auto_save_interval)
        
        # Data directory
        self.data_directory = QLineEdit()
        self.data_directory.setText(self.settings['file']['data_directory'])
        
        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self.browse_directory)
        
        dir_layout = QHBoxLayout()
        dir_layout.addWidget(self.data_directory)
        dir_layout.addWidget(self.browse_button)
        
        layout.addRow("Data Directory:", dir_layout)
        
        tab.setLayout(layout)
        return tab
        
    def browse_directory(self):
        """Open a directory browser dialog."""
        current_dir = self.data_directory.text()
        directory = QFileDialog.getExistingDirectory(
            self, "Select Data Directory", current_dir
        )
        
        if directory:
            self.data_directory.setText(directory)
            
    def detect_gpib_devices(self, gpib_field):
        """Detect and list available GPIB devices."""
        try:
            rm = pyvisa.ResourceManager()
            resources = rm.list_resources()
            
            if not resources:
                QMessageBox.warning(self, "GPIB Detection", "No GPIB or other instruments detected!")
                return
                
            # Create dialog to select from available devices
            dialog = QDialog(self)
            dialog.setWindowTitle("Select GPIB Device")
            
            layout = QVBoxLayout()
            
            # Create list of radio buttons
            button_group = QButtonGroup(dialog)
            
            for i, resource in enumerate(resources):
                radio = QRadioButton(resource)
                if resource == gpib_field.text():
                    radio.setChecked(True)
                button_group.addButton(radio, i)
                layout.addWidget(radio)
                
            # Create buttons
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
            
            # Show dialog
            if dialog.exec_():
                selected_button = button_group.checkedButton()
                if selected_button:
                    gpib_field.setText(selected_button.text())
        
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error detecting GPIB devices: {str(e)}")
            
    def save_settings(self):
        """Save the settings and close the dialog."""
        # Update settings dictionary
        
        # Resistance mode settings
        self.settings['resistance_mode']['test_current'] = self.res_test_current.value()
        self.settings['resistance_mode']['voltage_compliance'] = self.res_voltage_compliance.value()
        self.settings['resistance_mode']['sampling_rate'] = self.res_sampling_rate.value()
        self.settings['resistance_mode']['auto_range'] = self.res_auto_range.isChecked()
        self.settings['resistance_mode']['nplc'] = self.res_nplc.value()
        self.settings['resistance_mode']['settling_time'] = self.res_settling_time.value()
        self.settings['resistance_mode']['measurement_type'] = self.res_measurement_type.currentText()
        self.settings['resistance_mode']['gpib_address'] = self.res_gpib_address.text()
        
        # Voltage mode settings
        self.settings['voltage_mode']['voltage'] = self.volt_voltage.value()
        self.settings['voltage_mode']['current_compliance'] = self.volt_current_compliance.value()
        self.settings['voltage_mode']['duration'] = self.volt_duration.value()
        self.settings['voltage_mode']['sampling_rate'] = self.volt_sampling_rate.value()
        self.settings['voltage_mode']['auto_range'] = self.volt_auto_range.isChecked()
        self.settings['voltage_mode']['nplc'] = self.volt_nplc.value()
        self.settings['voltage_mode']['settling_time'] = self.volt_settling_time.value()
        self.settings['voltage_mode']['gpib_address'] = self.volt_gpib_address.text()
        
        # Current mode settings
        self.settings['current_mode']['current'] = self.curr_current.value()
        self.settings['current_mode']['voltage_compliance'] = self.curr_voltage_compliance.value()
        self.settings['current_mode']['duration'] = self.curr_duration.value()
        self.settings['current_mode']['sampling_rate'] = self.curr_sampling_rate.value()
        self.settings['current_mode']['auto_range'] = self.curr_auto_range.isChecked()
        self.settings['current_mode']['nplc'] = self.curr_nplc.value()
        self.settings['current_mode']['settling_time'] = self.curr_settling_time.value()
        self.settings['current_mode']['gpib_address'] = self.curr_gpib_address.text()
        
        # Display settings
        self.settings['display']['enable_plot'] = self.enable_plot.isChecked()
        self.settings['display']['plot_update_interval'] = self.plot_update_interval.value()
        self.settings['display']['plot_colors'] = {
            'resistance': self.resistance_color.currentText(),
            'voltage': self.voltage_color.currentText(),
            'current': self.current_color.currentText()
        }
        self.settings['display']['plot_figsize'] = [self.plot_width.value(), self.plot_height.value()]
        buffer_size = self.buffer_size.value()
        self.settings['display']['buffer_size'] = None if buffer_size == 0 else buffer_size
        
        # File settings
        self.settings['file']['auto_save_interval'] = self.auto_save_interval.value()
        self.settings['file']['data_directory'] = self.data_directory.text()
        
        # Save settings to config
        if self.username:
            self.config_manager.update_user_settings(self.username, self.settings)
        else:
            self.config_manager.update_global_settings(self.settings)
            
        # Accept dialog
        self.accept()

# ----- User Selection Dialog -----
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
                
        # Create new user section
        new_user_group = QGroupBox("Create New User")
        new_user_layout = QHBoxLayout()
        self.new_user_input = QLineEdit()
        self.create_user_button = QPushButton("Create")
        self.create_user_button.clicked.connect(self.create_new_user)
        
        new_user_layout.addWidget(self.new_user_input)
        new_user_layout.addWidget(self.create_user_button)
        new_user_group.setLayout(new_user_layout)
        
        # Create buttons
        self.select_button = QPushButton("Select User")
        self.select_button.clicked.connect(self.select_user)
        self.select_button.setEnabled(len(users) > 0)
        
        self.settings_button = QPushButton("Global Settings")
        self.settings_button.clicked.connect(self.open_settings)
        
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
            layout.addWidget(QLabel("Select User:"))
            layout.addWidget(self.user_combo)
            
        layout.addWidget(new_user_group)
        layout.addLayout(button_layout)
        
        self.setLayout(layout)
        
    def create_new_user(self):
        """Create a new user."""
        username = self.new_user_input.text().strip()
        
        if not username:
            QMessageBox.warning(self, "Invalid Username", "Please enter a username.")
            return
            
        # Add user to config
        self.config_manager.add_user(username)
        self.selected_user = username
        
        # Accept dialog
        self.accept()
        
    def select_user(self):
        """Select an existing user."""
        if self.user_combo.count() == 0:
            QMessageBox.warning(self, "No Users", "Please create a new user first.")
            return
            
        self.selected_user = self.user_combo.currentText()
        self.config_manager.set_last_user(self.selected_user)
        
        # Accept dialog
        self.accept()
        
    def open_settings(self):
        """Open the global settings dialog."""
        dialog = SettingsDialog(self.config_manager, parent=self)
        dialog.exec_()

# ----- Main Application Window -----
class EnhancedResistanceMeterApp(QMainWindow):
    """Enhanced application window with tabbed interface for different measurement modes."""
    def __init__(self):
        super().__init__()
        
        self.config_manager = ConfigManager()
        self.data_buffer = EnhancedDataBuffer()
        self.measurement_worker = None
        self.plot_timer = None
        self.current_user = None
        self.user_settings = None
        self.current_mode = "resistance"
        self.plot_data_type = "resistance"
        
        self.setWindowTitle(f"Enhanced ResistaMet GUI v{__version__}")
        self.setMinimumSize(1000, 700)
        
        self.init_ui()
        
        # Select user on startup
        self.select_user()
        
    def init_ui(self):
        """Initialize the UI."""
        # Create central widget with tab interface
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        
        # Create tabs
        self.resistance_tab = self.create_resistance_tab()
        self.voltage_tab = self.create_voltage_tab()
        self.current_tab = self.create_current_tab()
        
        # Add tabs
        self.tabs.addTab(self.resistance_tab, "Resistance Measurement")
        self.tabs.addTab(self.voltage_tab, "Voltage Application")
        self.tabs.addTab(self.current_tab, "Current Application")
        
        # Connect tab change signal
        self.tabs.currentChanged.connect(self.on_tab_changed)
        
        # Create status bar
        self.statusBar().showMessage("Ready")
        
        # Create menu bar
        menu_bar = self.menuBar()
        
        # File menu
        file_menu = menu_bar.addMenu("File")
        
        save_plot_action = QAction("Save Plot...", self)
        save_plot_action.triggered.connect(self.save_plot)
        
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        
        file_menu.addAction(save_plot_action)
        file_menu.addSeparator()
        file_menu.addAction(exit_action)
        
        # Settings menu
        settings_menu = menu_bar.addMenu("Settings")
        
        user_settings_action = QAction("User Settings...", self)
        user_settings_action.triggered.connect(self.open_user_settings)
        
        global_settings_action = QAction("Global Settings...", self)
        global_settings_action.triggered.connect(self.open_global_settings)
        
        settings_menu.addAction(user_settings_action)
        settings_menu.addAction(global_settings_action)
        
        # View menu
        view_menu = menu_bar.addMenu("View")
        
        view_resistance_action = QAction("Show Resistance", self)
        view_resistance_action.triggered.connect(lambda: self.change_plot_data_type("resistance"))
        
        view_voltage_action = QAction("Show Voltage", self)
        view_voltage_action.triggered.connect(lambda: self.change_plot_data_type("voltage"))
        
        view_current_action = QAction("Show Current", self)
        view_current_action.triggered.connect(lambda: self.change_plot_data_type("current"))
        
        view_menu.addAction(view_resistance_action)
        view_menu.addAction(view_voltage_action)
        view_menu.addAction(view_current_action)
        
        # Help menu
        help_menu = menu_bar.addMenu("Help")
        
        about_action = QAction("About", self)
        about_action.triggered.connect(self.show_about)
        
        help_menu.addAction(about_action)
        
    def create_resistance_tab(self):
        """Create the resistance measurement tab."""
        tab = QWidget()
        layout = QVBoxLayout()
        
        # Top section with user and sample info
        top_layout = QHBoxLayout()
        
        # User info
        user_group = QGroupBox("User")
        user_layout = QHBoxLayout()
        self.res_user_label = QLabel("No user selected")
        self.res_change_user_button = QPushButton("Change User")
        self.res_change_user_button.clicked.connect(self.select_user)
        
        user_layout.addWidget(self.res_user_label)
        user_layout.addWidget(self.res_change_user_button)
        user_group.setLayout(user_layout)
        
        # Sample info
        sample_group = QGroupBox("Sample")
        sample_layout = QHBoxLayout()
        self.res_sample_input = QLineEdit()
        self.res_sample_input.setPlaceholderText("Enter sample name")
        
        sample_layout.addWidget(self.res_sample_input)
        sample_group.setLayout(sample_layout)
        
        # Measurement type and current display
        info_group = QGroupBox("Measurement Info")
        info_layout = QHBoxLayout()
        self.res_measurement_type_label = QLabel("Type: --")
        self.res_test_current_label = QLabel("Current: -- mA")
        
        info_layout.addWidget(self.res_measurement_type_label)
        info_layout.addWidget(self.res_test_current_label)
        info_group.setLayout(info_layout)
        
        top_layout.addWidget(user_group)
        top_layout.addWidget(sample_group)
        top_layout.addWidget(info_group)
        
        # Create plot canvas (shared across tabs)
        if not hasattr(self, 'canvas'):
            self.canvas = EnhancedMplCanvas(self, width=10, height=6, dpi=100)
            self.toolbar = NavigationToolbar(self.canvas, self)
            
        plot_layout = QVBoxLayout()
        plot_layout.addWidget(self.toolbar)
        plot_layout.addWidget(self.canvas)
        
        # Control panel
        control_panel = QGroupBox("Controls")
        control_layout = QVBoxLayout()
        
        # Buttons
        button_layout = QHBoxLayout()
        
        self.res_start_button = QPushButton("Start Measurement")
        self.res_start_button.clicked.connect(self.start_resistance_measurement)
        
        self.res_pause_button = QPushButton("Pause")
        self.res_pause_button.clicked.connect(self.pause_measurement)
        self.res_pause_button.setEnabled(False)
        
        self.res_mark_button = QPushButton("Mark Point (M)")
        self.res_mark_button.clicked.connect(lambda: self.mark_event("MARKED_POINT"))
        self.res_mark_button.setEnabled(False)
        
        self.res_stop_button = QPushButton("Stop Measurement")
        self.res_stop_button.clicked.connect(self.stop_measurement)
        self.res_stop_button.setEnabled(False)
        
        button_layout.addWidget(self.res_start_button)
        button_layout.addWidget(self.res_pause_button)
        button_layout.addWidget(self.res_mark_button)
        button_layout.addWidget(self.res_stop_button)
        
        # Settings button
        settings_layout = QHBoxLayout()
        
        self.res_settings_button = QPushButton("Settings")
        self.res_settings_button.clicked.connect(self.open_user_settings)
        
        settings_layout.addStretch()
        settings_layout.addWidget(self.res_settings_button)
        
        # Add layouts to control panel
        control_layout.addLayout(button_layout)
        control_layout.addLayout(settings_layout)
        
        control_panel.setLayout(control_layout)
        
        # Create status display (shared across tabs)
        if not hasattr(self, 'status_display'):
            self.status_display = QTextEdit()
            self.status_display.setReadOnly(True)
            self.status_display.setMaximumHeight(100)
            
        # Add widgets to main layout
        layout.addLayout(top_layout)
        layout.addLayout(plot_layout, stretch=1)
        layout.addWidget(control_panel)
        layout.addWidget(QLabel("Status:"))
        layout.addWidget(self.status_display)
        
        tab.setLayout(layout)
        return tab
        
    def create_voltage_tab(self):
        """Create the voltage application tab."""
        tab = QWidget()
        layout = QVBoxLayout()
        
        # Top section with user and sample info
        top_layout = QHBoxLayout()
        
        # User info
        user_group = QGroupBox("User")
        user_layout = QHBoxLayout()
        self.volt_user_label = QLabel("No user selected")
        self.volt_change_user_button = QPushButton("Change User")
        self.volt_change_user_button.clicked.connect(self.select_user)
        
        user_layout.addWidget(self.volt_user_label)
        user_layout.addWidget(self.volt_change_user_button)
        user_group.setLayout(user_layout)
        
        # Sample info
        sample_group = QGroupBox("Sample")
        sample_layout = QHBoxLayout()
        self.volt_sample_input = QLineEdit()
        self.volt_sample_input.setPlaceholderText("Enter sample name")
        
        sample_layout.addWidget(self.volt_sample_input)
        sample_group.setLayout(sample_layout)
        
        # Voltage and compliance display
        info_group = QGroupBox("Application Info")
        info_layout = QVBoxLayout()
        
        voltage_layout = QHBoxLayout()
        self.volt_voltage_label = QLabel("Voltage: -- V")
        self.volt_compliance_label = QLabel("Compliance: -- mA")
        voltage_layout.addWidget(self.volt_voltage_label)
        voltage_layout.addWidget(self.volt_compliance_label)
        
        duration_layout = QHBoxLayout()
        self.volt_duration_label = QLabel("Duration: -- hours")
        duration_layout.addWidget(self.volt_duration_label)
        duration_layout.addStretch()
        
        info_layout.addLayout(voltage_layout)
        info_layout.addLayout(duration_layout)
        info_group.setLayout(info_layout)
        
        top_layout.addWidget(user_group)
        top_layout.addWidget(sample_group)
        top_layout.addWidget(info_group)
        
        # Use shared canvas
        plot_layout = QVBoxLayout()
        plot_layout.addWidget(self.toolbar)
        plot_layout.addWidget(self.canvas)
        
        # Control panel
        control_panel = QGroupBox("Controls")
        control_layout = QVBoxLayout()
        
        # Parameters
        params_layout = QHBoxLayout()
        
        # Voltage
        voltage_group = QGroupBox("Voltage (V)")
        voltage_group_layout = QVBoxLayout()
        self.volt_voltage_input = QDoubleSpinBox()
        self.volt_voltage_input.setRange(-200.0, 200.0)
        self.volt_voltage_input.setDecimals(3)
        self.volt_voltage_input.setSingleStep(1.0)
        self.volt_voltage_input.setValue(10.0)
        voltage_group_layout.addWidget(self.volt_voltage_input)
        voltage_group.setLayout(voltage_group_layout)
        
        # Compliance
        compliance_group = QGroupBox("Current Compliance (mA)")
        compliance_group_layout = QVBoxLayout()
        self.volt_compliance_input = QDoubleSpinBox()
        self.volt_compliance_input.setRange(0.001, 1000.0)
        self.volt_compliance_input.setDecimals(3)
        self.volt_compliance_input.setSingleStep(1.0)
        self.volt_compliance_input.setValue(10.0)
        compliance_group_layout.addWidget(self.volt_compliance_input)
        compliance_group.setLayout(compliance_group_layout)
        
        # Duration
        duration_group = QGroupBox("Duration (hours)")
        duration_group_layout = QVBoxLayout()
        self.volt_duration_input = QDoubleSpinBox()
        self.volt_duration_input.setRange(0.01, 168.0)
        self.volt_duration_input.setDecimals(2)
        self.volt_duration_input.setSingleStep(0.5)
        self.volt_duration_input.setValue(1.0)
        duration_group_layout.addWidget(self.volt_duration_input)
        duration_group.setLayout(duration_group_layout)
        
        params_layout.addWidget(voltage_group)
        params_layout.addWidget(compliance_group)
        params_layout.addWidget(duration_group)
        
        # Buttons
        button_layout = QHBoxLayout()
        
        self.volt_start_button = QPushButton("Start Voltage Application")
        self.volt_start_button.clicked.connect(self.start_voltage_application)
        
        self.volt_pause_button = QPushButton("Pause")
        self.volt_pause_button.clicked.connect(self.pause_measurement)
        self.volt_pause_button.setEnabled(False)
        
        self.volt_mark_button = QPushButton("Mark Event (M)")
        self.volt_mark_button.clicked.connect(lambda: self.mark_event("VOLTAGE_EVENT"))
        self.volt_mark_button.setEnabled(False)
        
        self.volt_stop_button = QPushButton("Stop Application")
        self.volt_stop_button.clicked.connect(self.stop_measurement)
        self.volt_stop_button.setEnabled(False)
        
        button_layout.addWidget(self.volt_start_button)
        button_layout.addWidget(self.volt_pause_button)
        button_layout.addWidget(self.volt_mark_button)
        button_layout.addWidget(self.volt_stop_button)
        
        # Settings button
        settings_layout = QHBoxLayout()
        
        self.volt_settings_button = QPushButton("Settings")
        self.volt_settings_button.clicked.connect(self.open_user_settings)
        
        settings_layout.addStretch()
        settings_layout.addWidget(self.volt_settings_button)
        
        # Add layouts to control panel
        control_layout.addLayout(params_layout)
        control_layout.addLayout(button_layout)
        control_layout.addLayout(settings_layout)
        
        control_panel.setLayout(control_layout)
        
        # Add widgets to main layout
        layout.addLayout(top_layout)
        layout.addLayout(plot_layout, stretch=1)
        layout.addWidget(control_panel)
        layout.addWidget(QLabel("Status:"))
        layout.addWidget(self.status_display)
        
        tab.setLayout(layout)
        return tab
        
    def create_current_tab(self):
        """Create the current application tab."""
        tab = QWidget()
        layout = QVBoxLayout()
        
        # Top section with user and sample info
        top_layout = QHBoxLayout()
        
        # User info
        user_group = QGroupBox("User")
        user_layout = QHBoxLayout()
        self.curr_user_label = QLabel("No user selected")
        self.curr_user_label = QLabel("No user selected")
        self.curr_change_user_button = QPushButton("Change User")
        self.curr_change_user_button.clicked.connect(self.select_user)
        
        user_layout.addWidget(self.curr_user_label)
        user_layout.addWidget(self.curr_change_user_button)
        user_group.setLayout(user_layout)
        
        # Sample info
        sample_group = QGroupBox("Sample")
        sample_layout = QHBoxLayout()
        self.curr_sample_input = QLineEdit()
        self.curr_sample_input.setPlaceholderText("Enter sample name")
        
        sample_layout.addWidget(self.curr_sample_input)
        sample_group.setLayout(sample_layout)
        
        # Current and compliance display
        info_group = QGroupBox("Application Info")
        info_layout = QVBoxLayout()
        
        current_layout = QHBoxLayout()
        self.curr_current_label = QLabel("Current: -- mA")
        self.curr_compliance_label = QLabel("Compliance: -- V")
        current_layout.addWidget(self.curr_current_label)
        current_layout.addWidget(self.curr_compliance_label)
        
        duration_layout = QHBoxLayout()
        self.curr_duration_label = QLabel("Duration: -- hours")
        duration_layout.addWidget(self.curr_duration_label)
        duration_layout.addStretch()
        
        info_layout.addLayout(current_layout)
        info_layout.addLayout(duration_layout)
        info_group.setLayout(info_layout)
        
        top_layout.addWidget(user_group)
        top_layout.addWidget(sample_group)
        top_layout.addWidget(info_group)
        
        # Use shared canvas
        plot_layout = QVBoxLayout()
        plot_layout.addWidget(self.toolbar)
        plot_layout.addWidget(self.canvas)
        
        # Control panel
        control_panel = QGroupBox("Controls")
        control_layout = QVBoxLayout()
        
        # Parameters
        params_layout = QHBoxLayout()
        
        # Current
        current_group = QGroupBox("Current (mA)")
        current_group_layout = QVBoxLayout()
        self.curr_current_input = QDoubleSpinBox()
        self.curr_current_input.setRange(-1000.0, 1000.0)
        self.curr_current_input.setDecimals(3)
        self.curr_current_input.setSingleStep(1.0)
        self.curr_current_input.setValue(1.0)
        current_group_layout.addWidget(self.curr_current_input)
        current_group.setLayout(current_group_layout)
        
        # Compliance
        compliance_group = QGroupBox("Voltage Compliance (V)")
        compliance_group_layout = QVBoxLayout()
        self.curr_compliance_input = QDoubleSpinBox()
        self.curr_compliance_input.setRange(0.1, 200.0)
        self.curr_compliance_input.setDecimals(3)
        self.curr_compliance_input.setSingleStep(1.0)
        self.curr_compliance_input.setValue(10.0)
        compliance_group_layout.addWidget(self.curr_compliance_input)
        compliance_group.setLayout(compliance_group_layout)
        
        # Duration
        duration_group = QGroupBox("Duration (hours)")
        duration_group_layout = QVBoxLayout()
        self.curr_duration_input = QDoubleSpinBox()
        self.curr_duration_input.setRange(0.01, 168.0)
        self.curr_duration_input.setDecimals(2)
        self.curr_duration_input.setSingleStep(0.5)
        self.curr_duration_input.setValue(1.0)
        duration_group_layout.addWidget(self.curr_duration_input)
        duration_group.setLayout(duration_group_layout)
        
        params_layout.addWidget(current_group)
        params_layout.addWidget(compliance_group)
        params_layout.addWidget(duration_group)
        
        # Buttons
        button_layout = QHBoxLayout()
        
        self.curr_start_button = QPushButton("Start Current Application")
        self.curr_start_button.clicked.connect(self.start_current_application)
        
        self.curr_pause_button = QPushButton("Pause")
        self.curr_pause_button.clicked.connect(self.pause_measurement)
        self.curr_pause_button.setEnabled(False)
        
        self.curr_mark_button = QPushButton("Mark Event (M)")
        self.curr_mark_button.clicked.connect(lambda: self.mark_event("CURRENT_EVENT"))
        self.curr_mark_button.setEnabled(False)
        
        self.curr_stop_button = QPushButton("Stop Application")
        self.curr_stop_button.clicked.connect(self.stop_measurement)
        self.curr_stop_button.setEnabled(False)
        
        button_layout.addWidget(self.curr_start_button)
        button_layout.addWidget(self.curr_pause_button)
        button_layout.addWidget(self.curr_mark_button)
        button_layout.addWidget(self.curr_stop_button)
        
        # Settings button
        settings_layout = QHBoxLayout()
        
        self.curr_settings_button = QPushButton("Settings")
        self.curr_settings_button.clicked.connect(self.open_user_settings)
        
        settings_layout.addStretch()
        settings_layout.addWidget(self.curr_settings_button)
        
        # Add layouts to control panel
        control_layout.addLayout(params_layout)
        control_layout.addLayout(button_layout)
        control_layout.addLayout(settings_layout)
        
        control_panel.setLayout(control_layout)
        
        # Add widgets to main layout
        layout.addLayout(top_layout)
        layout.addLayout(plot_layout, stretch=1)
        layout.addWidget(control_panel)
        layout.addWidget(QLabel("Status:"))
        layout.addWidget(self.status_display)
        
        tab.setLayout(layout)
        return tab
        
    def on_tab_changed(self, index):
        """Handle tab change."""
        if index == 0:
            self.current_mode = "resistance"
        elif index == 1:
            self.current_mode = "voltage"
        elif index == 2:
            self.current_mode = "current"
            
        # Update plot based on the selected mode
        self.update_plot()
        
    def select_user(self):
        """Open user selection dialog."""
        dialog = UserSelectionDialog(self.config_manager, self)
        
        if dialog.exec_():
            username = dialog.selected_user
            
            if username:
                self.current_user = username
                self.user_settings = self.config_manager.get_user_settings(username)
                
                # Update user labels
                self.res_user_label.setText(f"User: {username}")
                self.volt_user_label.setText(f"User: {username}")
                self.curr_user_label.setText(f"User: {username}")
                
                # Update displayed settings
                self.update_settings_display()
                
                # Reset plot
                buffer_size = self.user_settings['display']['buffer_size']
                self.data_buffer = EnhancedDataBuffer(size=buffer_size)
                self.canvas.clear_plot()
                
                # Update status
                self.log_status(f"User selected: {username}")
                self.statusBar().showMessage(f"User: {username}")
                
    def update_settings_display(self):
        """Update the displayed settings in the UI."""
        if not self.user_settings:
            return
            
        # Update resistance mode display
        measurement_type = self.user_settings['resistance_mode']['measurement_type']
        self.res_measurement_type_label.setText(f"Type: {measurement_type}")
        
        test_current_ma = self.user_settings['resistance_mode']['test_current'] * 1000
        self.res_test_current_label.setText(f"Current: {test_current_ma:.2f} mA")
        
        # Update voltage mode display
        voltage = self.user_settings['voltage_mode']['voltage']
        self.volt_voltage_label.setText(f"Voltage: {voltage:.3f} V")
        
        current_compliance = self.user_settings['voltage_mode']['current_compliance']
        self.volt_compliance_label.setText(f"Compliance: {current_compliance:.3f} mA")
        
        duration = self.user_settings['voltage_mode']['duration']
        self.volt_duration_label.setText(f"Duration: {duration:.2f} hours")
        
        # Set voltage mode inputs
        self.volt_voltage_input.setValue(voltage)
        self.volt_compliance_input.setValue(current_compliance)
        self.volt_duration_input.setValue(duration)
        
        # Update current mode display
        current = self.user_settings['current_mode']['current']
        self.curr_current_label.setText(f"Current: {current:.3f} mA")
        
        voltage_compliance = self.user_settings['current_mode']['voltage_compliance']
        self.curr_compliance_label.setText(f"Compliance: {voltage_compliance:.3f} V")
        
        duration = self.user_settings['current_mode']['duration']
        self.curr_duration_label.setText(f"Duration: {duration:.2f} hours")
        
        # Set current mode inputs
        self.curr_current_input.setValue(current)
        self.curr_compliance_input.setValue(voltage_compliance)
        self.curr_duration_input.setValue(duration)
        
        # Update plot colors
        for data_type, color in self.user_settings['display']['plot_colors'].items():
            self.canvas.set_plot_color(data_type, color)
            
    def open_user_settings(self):
        """Open settings dialog for current user."""
        if not self.current_user:
            QMessageBox.warning(self, "No User Selected", "Please select a user first.")
            return
            
        dialog = SettingsDialog(self.config_manager, self.current_user, self)
        
        if dialog.exec_():
            # Reload settings
            self.user_settings = self.config_manager.get_user_settings(self.current_user)
            
            # Update displayed settings
            self.update_settings_display()
            
            # Update data buffer size if changed
            new_buffer_size = self.user_settings['display']['buffer_size']
            if new_buffer_size != self.data_buffer.size:
                old_data = None
                if self.data_buffer.timestamps:  # If we have data, try to preserve it
                    old_data = {
                        'timestamps': list(self.data_buffer.timestamps),
                        'resistance': list(self.data_buffer.resistance),
                        'voltage': list(self.data_buffer.voltage),
                        'current': list(self.data_buffer.current),
                        'events': list(self.data_buffer.events)
                    }
                
                self.data_buffer = EnhancedDataBuffer(size=new_buffer_size)
                
                # Restore data if available
                if old_data and old_data['timestamps']:
                    for i, timestamp in enumerate(old_data['timestamps']):
                        if old_data['resistance'][i] is not None:
                            self.data_buffer.add_resistance(
                                timestamp, 
                                old_data['resistance'][i], 
                                old_data['events'][i]
                            )
                        elif old_data['voltage'][i] is not None and old_data['current'][i] is not None:
                            self.data_buffer.add_voltage_current(
                                timestamp, 
                                old_data['voltage'][i], 
                                old_data['current'][i], 
                                old_data['events'][i]
                            )
                
                # Update plot
                self.update_plot()
            
    def open_global_settings(self):
        """Open global settings dialog."""
        dialog = SettingsDialog(self.config_manager, parent=self)
        dialog.exec_()
        
    def start_resistance_measurement(self):
        """Start resistance measurement."""
        if not self.current_user:
            QMessageBox.warning(self, "No User Selected", "Please select a user first.")
            return
            
        sample_name = self.res_sample_input.text().strip()
        if not sample_name:
            QMessageBox.warning(self, "No Sample Name", "Please enter a sample name.")
            return
            
        # Disable controls across all tabs
        self.res_start_button.setEnabled(False)
        self.res_settings_button.setEnabled(False)
        self.res_change_user_button.setEnabled(False)
        self.res_sample_input.setEnabled(False)
        
        self.volt_start_button.setEnabled(False)
        self.volt_settings_button.setEnabled(False)
        self.volt_change_user_button.setEnabled(False)
        self.volt_sample_input.setEnabled(False)
        self.volt_voltage_input.setEnabled(False)
        self.volt_compliance_input.setEnabled(False)
        self.volt_duration_input.setEnabled(False)
        
        self.curr_start_button.setEnabled(False)
        self.curr_settings_button.setEnabled(False)
        self.curr_change_user_button.setEnabled(False)
        self.curr_sample_input.setEnabled(False)
        self.curr_current_input.setEnabled(False)
        self.curr_compliance_input.setEnabled(False)
        self.curr_duration_input.setEnabled(False)
        
        # Enable appropriate control buttons
        self.res_pause_button.setEnabled(True)
        self.res_pause_button.setText("Pause")
        self.res_mark_button.setEnabled(True)
        self.res_stop_button.setEnabled(True)
        
        # Clear old data
        self.data_buffer.clear()
        self.canvas.clear_plot()
        
        # Set mode
        self.current_mode = "resistance"
        
        # Create and start worker thread
        self.measurement_worker = ResistanceMeasurementWorker(
            sample_name, self.current_user, self.user_settings
        )
        
        # Connect signals
        self.connect_worker_signals()
        
        # Start worker
        self.measurement_worker.start()
        
        # Start plot update timer
        self.start_plot_timer()
        
        # Update status
        self.log_status(f"Resistance measurement started with sample: {sample_name}")
        self.statusBar().showMessage("Resistance measurement running...")
        
    def start_voltage_application(self):
        """Start voltage application."""
        if not self.current_user:
            QMessageBox.warning(self, "No User Selected", "Please select a user first.")
            return
            
        sample_name = self.volt_sample_input.text().strip()
        if not sample_name:
            QMessageBox.warning(self, "No Sample Name", "Please enter a sample name.")
            return
            
        # Get input values
        voltage = self.volt_voltage_input.value()
        current_compliance = self.volt_compliance_input.value()
        duration = self.volt_duration_input.value()
        
        # Update settings
        self.user_settings['voltage_mode']['voltage'] = voltage
        self.user_settings['voltage_mode']['current_compliance'] = current_compliance
        self.user_settings['voltage_mode']['duration'] = duration
        
        # Disable controls across all tabs
        self.res_start_button.setEnabled(False)
        self.res_settings_button.setEnabled(False)
        self.res_change_user_button.setEnabled(False)
        self.res_sample_input.setEnabled(False)
        
        self.volt_start_button.setEnabled(False)
        self.volt_settings_button.setEnabled(False)
        self.volt_change_user_button.setEnabled(False)
        self.volt_sample_input.setEnabled(False)
        self.volt_voltage_input.setEnabled(False)
        self.volt_compliance_input.setEnabled(False)
        self.volt_duration_input.setEnabled(False)
        
        self.curr_start_button.setEnabled(False)
        self.curr_settings_button.setEnabled(False)
        self.curr_change_user_button.setEnabled(False)
        self.curr_sample_input.setEnabled(False)
        self.curr_current_input.setEnabled(False)
        self.curr_compliance_input.setEnabled(False)
        self.curr_duration_input.setEnabled(False)
        
        # Enable appropriate control buttons
        self.volt_pause_button.setEnabled(True)
        self.volt_pause_button.setText("Pause")
        self.volt_mark_button.setEnabled(True)
        self.volt_stop_button.setEnabled(True)
        
        # Clear old data
        self.data_buffer.clear()
        self.canvas.clear_plot()
        
        # Set mode
        self.current_mode = "voltage"
        
        # Create and start worker thread
        self.measurement_worker = VoltageApplicationWorker(
            sample_name, self.current_user, self.user_settings
        )
        
        # Connect signals
        self.connect_worker_signals()
        
        # Start worker
        self.measurement_worker.start()
        
        # Start plot update timer
        self.start_plot_timer()
        
        # Update status
        self.log_status(f"Voltage application started with sample: {sample_name}")
        self.statusBar().showMessage(f"Applying {voltage}V to sample...")
        
    def start_current_application(self):
        """Start current application."""
        if not self.current_user:
            QMessageBox.warning(self, "No User Selected", "Please select a user first.")
            return
            
        sample_name = self.curr_sample_input.text().strip()
        if not sample_name:
            QMessageBox.warning(self, "No Sample Name", "Please enter a sample name.")
            return
            
        # Get input values
        current = self.curr_current_input.value()
        voltage_compliance = self.curr_compliance_input.value()
        duration = self.curr_duration_input.value()
        
        # Update settings
        self.user_settings['current_mode']['current'] = current
        self.user_settings['current_mode']['voltage_compliance'] = voltage_compliance
        self.user_settings['current_mode']['duration'] = duration
        
        # Disable controls across all tabs
        self.res_start_button.setEnabled(False)
        self.res_settings_button.setEnabled(False)
        self.res_change_user_button.setEnabled(False)
        self.res_sample_input.setEnabled(False)
        
        self.volt_start_button.setEnabled(False)
        self.volt_settings_button.setEnabled(False)
        self.volt_change_user_button.setEnabled(False)
        self.volt_sample_input.setEnabled(False)
        self.volt_voltage_input.setEnabled(False)
        self.volt_compliance_input.setEnabled(False)
        self.volt_duration_input.setEnabled(False)
        
        self.curr_start_button.setEnabled(False)
        self.curr_settings_button.setEnabled(False)
        self.curr_change_user_button.setEnabled(False)
        self.curr_sample_input.setEnabled(False)
        self.curr_current_input.setEnabled(False)
        self.curr_compliance_input.setEnabled(False)
        self.curr_duration_input.setEnabled(False)
        
        # Enable appropriate control buttons
        self.curr_pause_button.setEnabled(True)
        self.curr_pause_button.setText("Pause")
        self.curr_mark_button.setEnabled(True)
        self.curr_stop_button.setEnabled(True)
        
        # Clear old data
        self.data_buffer.clear()
        self.canvas.clear_plot()
        
        # Set mode
        self.current_mode = "current"
        
        # Create and start worker thread
        self.measurement_worker = CurrentApplicationWorker(
            sample_name, self.current_user, self.user_settings
        )
        
        # Connect signals
        self.connect_worker_signals()
        
        # Start worker
        self.measurement_worker.start()
        
        # Start plot update timer
        self.start_plot_timer()
        
        # Update status
        self.log_status(f"Current application started with sample: {sample_name}")
        self.statusBar().showMessage(f"Applying {current}mA to sample...")
        
    def connect_worker_signals(self):
        """Connect signals from the measurement worker."""
        if not self.measurement_worker:
            return
            
        self.measurement_worker.data_point.connect(self.update_data)
        self.measurement_worker.status_update.connect(self.log_status)
        self.measurement_worker.measurement_complete.connect(self.on_measurement_complete)
        self.measurement_worker.error_occurred.connect(self.on_error)
        
    def start_plot_timer(self):
        """Start the plot update timer."""
        if self.plot_timer and self.plot_timer.isActive():
            self.plot_timer.stop()
            
        update_interval = self.user_settings['display']['plot_update_interval']
        self.plot_timer = QTimer()
        self.plot_timer.timeout.connect(self.update_plot)
        self.plot_timer.start(update_interval)
        
    def mark_event(self, event_name):
        """Mark an event."""
        if not self.measurement_worker or self.measurement_worker.paused:
            return
            
        self.measurement_worker.mark_event(event_name)
        
    def pause_measurement(self):
        """Pause or resume the measurement."""
        if not self.measurement_worker:
            return
            
        if self.measurement_worker.paused:
            # Resume
            self.measurement_worker.resume_measurement()
            
            # Update button texts
            self.res_pause_button.setText("Pause")
            self.volt_pause_button.setText("Pause")
            self.curr_pause_button.setText("Pause")
        else:
            # Pause
            self.measurement_worker.pause_measurement()
            
            # Update button texts
            self.res_pause_button.setText("Resume")
            self.volt_pause_button.setText("Resume")
            self.curr_pause_button.setText("Resume")
            
    def stop_measurement(self):
        """Stop the measurement."""
        if not self.measurement_worker:
            return
            
        # Stop worker thread
        self.measurement_worker.stop_measurement()
        
        # Update UI buttons
        self.res_pause_button.setEnabled(False)
        self.res_mark_button.setEnabled(False)
        self.res_stop_button.setEnabled(False)
        
        self.volt_pause_button.setEnabled(False)
        self.volt_mark_button.setEnabled(False)
        self.volt_stop_button.setEnabled(False)
        
        self.curr_pause_button.setEnabled(False)
        self.curr_mark_button.setEnabled(False)
        self.curr_stop_button.setEnabled(False)
        
        # Show stopping message
        self.log_status("Stopping measurement...")
        self.statusBar().showMessage("Stopping measurement...")
        
    def update_data(self, timestamp, data_dict, event):
        """Update data buffer with new measurement data."""
        # Add data to buffer based on what we received
        if 'resistance' in data_dict and data_dict['resistance'] is not None and \
           'voltage' not in data_dict or 'current' not in data_dict:
            # Resistance-only measurement
            self.data_buffer.add_resistance(timestamp, data_dict['resistance'], event)
        elif 'voltage' in data_dict and 'current' in data_dict:
            # Voltage and current measurements
            self.data_buffer.add_voltage_current(
                timestamp, 
                data_dict['voltage'], 
                data_dict['current'], 
                event
            )
            
    def update_plot(self):
        """Update the plot with current data."""
        if not self.data_buffer or not self.data_buffer.timestamps:
            return
            
        # Get sample name based on current mode
        if self.current_mode == "resistance":
            sample_name = self.res_sample_input.text()
        elif self.current_mode == "voltage":
            sample_name = self.volt_sample_input.text()
        elif self.current_mode == "current":
            sample_name = self.curr_sample_input.text()
        else:
            sample_name = "Unknown"
            
        # Get plot data
        timestamps, values = self.data_buffer.get_data_for_plot(self.plot_data_type)
        stats = self.data_buffer.get_statistics(self.plot_data_type)
        
        # Format mode name for display
        if self.current_mode == "resistance":
            mode_display = "Resistance Measurement"
        elif self.current_mode == "voltage":
            mode_display = "Voltage Application"
        elif self.current_mode == "current":
            mode_display = "Current Application"
        else:
            mode_display = "Unknown Mode"
        
        # Update the plot
        self.canvas.update_plot(
            timestamps,
            values,
            stats,
            self.current_user,
            sample_name,
            mode_display,
            self.plot_data_type
        )
        
    def change_plot_data_type(self, data_type):
        """Change the type of data shown in the plot."""
        self.plot_data_type = data_type
        self.update_plot()
        
        # Update status
        self.log_status(f"Plot display changed to {data_type}")
        
    def on_measurement_complete(self):
        """Handle measurement completion."""
        # Stop plot timer
        if self.plot_timer:
            self.plot_timer.stop()
            
        # Update UI in all tabs
        self.res_start_button.setEnabled(True)
        self.res_settings_button.setEnabled(True)
        self.res_change_user_button.setEnabled(True)
        self.res_sample_input.setEnabled(True)
        self.res_pause_button.setEnabled(False)
        self.res_mark_button.setEnabled(False)
        self.res_stop_button.setEnabled(False)
        
        self.volt_start_button.setEnabled(True)
        self.volt_settings_button.setEnabled(True)
        self.volt_change_user_button.setEnabled(True)
        self.volt_sample_input.setEnabled(True)
        self.volt_voltage_input.setEnabled(True)
        self.volt_compliance_input.setEnabled(True)
        self.volt_duration_input.setEnabled(True)
        self.volt_pause_button.setEnabled(False)
        self.volt_mark_button.setEnabled(False)
        self.volt_stop_button.setEnabled(False)
        
        self.curr_start_button.setEnabled(True)
        self.curr_settings_button.setEnabled(True)
        self.curr_change_user_button.setEnabled(True)
        self.curr_sample_input.setEnabled(True)
        self.curr_current_input.setEnabled(True)
        self.curr_compliance_input.setEnabled(True)
        self.curr_duration_input.setEnabled(True)
        self.curr_pause_button.setEnabled(False)
        self.curr_mark_button.setEnabled(False)
        self.curr_stop_button.setEnabled(False)
        
        # Update status
        self.log_status("Measurement completed successfully!")
        self.statusBar().showMessage("Measurement completed")
        
        # Make sure final plot is updated
        self.update_plot()
        
    def on_error(self, error_message):
        """Handle measurement errors."""
        # Stop plot timer
        if self.plot_timer:
            self.plot_timer.stop()
            
        # Update UI in all tabs
        self.res_start_button.setEnabled(True)
        self.res_settings_button.setEnabled(True)
        self.res_change_user_button.setEnabled(True)
        self.res_sample_input.setEnabled(True)
        self.res_pause_button.setEnabled(False)
        self.res_mark_button.setEnabled(False)
        self.res_stop_button.setEnabled(False)
        
        self.volt_start_button.setEnabled(True)
        self.volt_settings_button.setEnabled(True)
        self.volt_change_user_button.setEnabled(True)
        self.volt_sample_input.setEnabled(True)
        self.volt_voltage_input.setEnabled(True)
        self.volt_compliance_input.setEnabled(True)
        self.volt_duration_input.setEnabled(True)
        self.volt_pause_button.setEnabled(False)
        self.volt_mark_button.setEnabled(False)
        self.volt_stop_button.setEnabled(False)
        
        self.curr_start_button.setEnabled(True)
        self.curr_settings_button.setEnabled(True)
        self.curr_change_user_button.setEnabled(True)
        self.curr_sample_input.setEnabled(True)
        self.curr_current_input.setEnabled(True)
        self.curr_compliance_input.setEnabled(True)
        self.curr_duration_input.setEnabled(True)
        self.curr_pause_button.setEnabled(False)
        self.curr_mark_button.setEnabled(False)
        self.curr_stop_button.setEnabled(False)
        
        # Log error
        self.log_status(f"ERROR: {error_message}")
        self.statusBar().showMessage("Measurement error")
        
        # Show error message
        QMessageBox.critical(self, "Measurement Error", error_message)
        
    def log_status(self, message):
        """Add a message to the status display."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.status_display.append(f"[{timestamp}] {message}")
        
        # Scroll to bottom
        scroll_bar = self.status_display.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())
        
    def save_plot(self):
        """Save the current plot to a file."""
        filename, _ = QFileDialog.getSaveFileName(
            self, "Save Plot", "", "PNG Files (*.png);;PDF Files (*.pdf);;All Files (*)"
        )
        
        if filename:
            self.canvas.fig.savefig(filename)
            self.log_status(f"Plot saved to: {filename}")
            
    def show_about(self):
        """Show about dialog."""
        about_text = f"""
        <h2>Enhanced ResistaMet GUI</h2>
        <p>Version: {__version__}</p>
        <p>Based on original version: {__original_version__}</p>
        <p>Author: {__author__}</p>
        <p>An integrated interface for resistance measurement and voltage/current application.</p>
        """
        
        QMessageBox.about(self, "About Enhanced ResistaMet GUI", about_text)
        
    def closeEvent(self, event):
        """Handle window close event."""
        # Stop measurement if running
        if self.measurement_worker and self.measurement_worker.isRunning():
            reply = QMessageBox.question(
                self, "Exit Confirmation",
                "A measurement is still running. Are you sure you want to exit?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                self.measurement_worker.stop_measurement()
                self.measurement_worker.wait()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()

def main():
    """Main application entry point."""
    app = QApplication(sys.argv)
    
    # Set application style
    app.setStyle("Fusion")
    
    # Create and show main window
    window = EnhancedResistanceMeterApp()
    window.show()
    
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
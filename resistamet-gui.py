#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ResistaMet GUI: Resistance Measurement System with Graphical User Interface
Based on ResistaMet v0.9.2 by Brenden Ferland

This implementation adds a PyQt-based GUI while preserving all the original functionality.
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
__version__ = "1.0.0"  # GUI version
__original_version__ = "0.9.2"  # Original script version
__author__ = "Brenden Ferland"

# Configuration file
CONFIG_FILE = "config.json"

# Default settings (same as original)
DEFAULT_SETTINGS = {
    "measurement": {
        "test_current": 1.0e-3,              # Test current in Amperes
        "voltage_compliance": 5.0,           # Voltage compliance in Volts
        "sampling_rate": 10.0,               # Sampling rate in Hz
        "auto_range": True,                  # Enable auto-ranging
        "nplc": 1,                           # Number of power line cycles
        "settling_time": 0.2,                # Settling time in seconds
        "measurement_type": "2-wire",        # Measurement type (2-wire or 4-wire)
        "gpib_address": "GPIB1::3::INSTR"    # GPIB address of the instrument
    },
    "display": {
        "enable_plot": True,                 # Enable real-time plotting
        "plot_update_interval": 200,         # Plot update interval in milliseconds
        "plot_color": "red",                 # Plot line color
        "plot_figsize": [10, 6],             # Plot figure size [width, height]
        "buffer_size": None                  # Data buffer size (None = unlimited)
    },
    "file": {
        "auto_save_interval": 60,            # Auto-save interval in seconds
        "data_directory": "resistance_data"  # Base directory for data storage
    },
    "users": [],                            # List of users
    "last_user": None                       # Last active user
}

# ----- Data Buffer Class (same as original) -----
class DataBuffer:
    def __init__(self, size: Optional[int] = None):
        if size is not None:
            self.buffer = deque(maxlen=size)
            self.timestamps = deque(maxlen=size)
        else:
            self.buffer = deque()      # unbounded
            self.timestamps = deque()  # unbounded

        self.stats = {'min': float('inf'), 'max': float('-inf'), 'avg': 0}
        self.count = 0

    @property
    def size(self):
        return self.buffer.maxlen

        
    def add(self, timestamp: float, value: float) -> None:
        self.buffer.append(value)
        self.timestamps.append(timestamp)
        self.count += 1
        
        # Update statistics
        if value < 0 or not np.isfinite(value):
            return
            
        if value < self.stats['min']:
            self.stats['min'] = value
        if value > self.stats['max']:
            self.stats['max'] = value
            
        # Update running average
        self.stats['avg'] = (self.stats['avg'] * (self.count - 1) + value) / self.count
        
    def get_data_for_plot(self) -> Tuple[List[float], List[float]]:
        return list(self.timestamps), list(self.buffer)
        
    def clear(self) -> None:
        """Clear all data from the buffer."""
        self.buffer.clear()
        self.timestamps.clear()
        self.stats = {'min': float('inf'), 'max': float('-inf'), 'avg': 0}
        self.count = 0

# ----- Config Manager Class (modified from original) -----
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
        for section in ['measurement', 'display', 'file']:
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
            if section in ['measurement', 'display', 'file']:
                if section not in self.config['user_settings'][username]:
                    self.config['user_settings'][username][section] = {}
                self.config['user_settings'][username][section].update(section_settings)
                
        self.save_config()
        
    def update_global_settings(self, settings: Dict) -> None:
        """Update global default settings."""
        # Update each section provided
        for section, section_settings in settings.items():
            if section in ['measurement', 'display', 'file']:
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

# ----- Measurement Worker Thread -----
class MeasurementWorker(QThread):
    """Worker thread for running measurements without blocking the GUI."""
    data_point = pyqtSignal(float, float, str)  # timestamp, resistance, event
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
        self.max_compression_pressed = False
        self.keithley = None
        self.csvfile = None
        self.writer = None
        self.start_time = 0
        
    def run(self):
        self.running = True
        
        try:
            # Extract settings
            measurement_settings = self.settings['measurement']
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
            filename = self._create_filename()
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
                    
                    event_marker = ""
                    if self.max_compression_pressed:
                        event_marker = "MAX_COMPRESSION"
                        self.max_compression_pressed = False
                        self.status_update.emit(f"⭐ MAX COMPRESSION MARKED at {elapsed_time:.3f}s ⭐")
                    
                    # Write to CSV
                    row_data = [
                        now_unix,
                        f"{elapsed_time:.3f}",
                        f"{resistance:.6e}",
                        event_marker
                    ]
                    
                    self.writer.writerow(row_data)
                    
                    # Emit signal with the data
                    self.data_point.emit(now, resistance, event_marker)
                    
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
    
    def _create_filename(self) -> str:
        """Create filename for data storage."""
        base_dir = Path(self.settings['file']['data_directory'])
        base_dir.mkdir(exist_ok=True)
        
        # Create user-specific subdirectory
        user_dir = base_dir / self.username
        user_dir.mkdir(exist_ok=True)
        
        timestamp = int(time.time())
        sanitized_name = ''.join(c if c.isalnum() else '_' for c in self.sample_name)
        
        # Include measurement type and current in filename
        measurement_type = self.settings['measurement']['measurement_type']
        test_current_ma = self.settings['measurement']['test_current'] * 1000  # Convert to mA
        
        filename = f"{timestamp}_{sanitized_name}_R_{measurement_type}_{test_current_ma:.1f}mA.csv"
        return user_dir / filename
    
    def _write_metadata(self, params: Dict) -> None:
        """Write metadata to CSV file."""
        self.writer.writerow(['Test Parameters'])
        for key, value in params.items():
            self.writer.writerow([key, value])
        self.writer.writerow([])  # Empty row for separation
    
    def mark_max_compression(self) -> None:
        """Mark a max compression point."""
        self.max_compression_pressed = True
        
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

# ----- Custom Matplotlib Canvas -----
class MplCanvas(FigureCanvas):
    """Matplotlib canvas for embedding in Qt."""
    def __init__(self, parent=None, width=10, height=6, dpi=100):
        self.fig = Figure(figsize=(width, height), dpi=dpi)
        self.axes = self.fig.add_subplot(111)
        self.axes.ticklabel_format(useOffset=False, style='plain')

        
        super(MplCanvas, self).__init__(self.fig)
        
        # Create initial empty plot
        self.axes.set_title('Real-time Resistance Measurement')
        self.axes.set_xlabel('Elapsed Time (s)')
        self.axes.set_ylabel('Resistance (Ohms)')
        self.axes.grid(True)
        
        # Create a line plot
        self.line, = self.axes.plot([], [], 'r-', label='Resistance')
        
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
        self.info_text = self.axes.text(0.98, 0.85, 'User: --\nSample: --', 
                                transform=self.axes.transAxes, 
                                ha='right', va='center')

        
        #Add bbox
        bbox_props = dict(boxstyle="round", fc="white", ec="black", alpha=0.5)
        self.min_text.set_bbox(bbox_props)
        self.max_text.set_bbox(bbox_props)
        self.avg_text.set_bbox(bbox_props)
        self.info_text.set_bbox(bbox_props)

        
        # Add legend
        self.axes.legend(loc='upper right')
        
        # Adjust layout
        self.fig.tight_layout()
        
    def update_plot(self, timestamps, resistances, stats, username, sample_name):
        """Update the plot with new data."""
        if not timestamps:
            return
            
        # Calculate elapsed times relative to the first data point
        elapsed_times = [t - timestamps[0] for t in timestamps]
        
        # Update line data
        self.line.set_data(elapsed_times, resistances)
        
        # Update axis limits
        self.axes.relim()
        self.axes.autoscale_view(True, True, True)
        
        # Update statistics text
        self.min_text.set_text(f'Min: {stats["min"]:.2f} Ω')
        self.max_text.set_text(f'Max: {stats["max"]:.2f} Ω')
        self.avg_text.set_text(f'Avg: {stats["avg"]:.2f} Ω')
        self.info_text.set_text(f'User: {username}\nSample: {sample_name}')
        
        # Draw the canvas
        self.draw()
        
    def clear_plot(self):
        """Clear the plot."""
        self.line.set_data([], [])
        self.min_text.set_text('Min: -- Ω')
        self.max_text.set_text('Max: -- Ω')
        self.avg_text.set_text('Avg: -- Ω')
        self.info_text.set_text('User: --\nSample: --')
        self.draw()
        
    def set_plot_color(self, color):
        """Set the plot line color."""
        self.line.set_color(color)
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
                'measurement': dict(config_manager.config['measurement']),
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
        self.measurement_tab = self.create_measurement_tab()
        self.display_tab = self.create_display_tab()
        self.file_tab = self.create_file_tab()
        
        # Add tabs to widget
        self.tabs.addTab(self.measurement_tab, "Measurement")
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
        
    def create_measurement_tab(self):
        """Create the measurement settings tab."""
        tab = QWidget()
        layout = QFormLayout()
        
        # Test current
        self.test_current = QDoubleSpinBox()
        self.test_current.setRange(0.0001, 1.0)
        self.test_current.setDecimals(6)
        self.test_current.setSingleStep(0.001)
        self.test_current.setValue(self.settings['measurement']['test_current'])
        layout.addRow("Test Current (A):", self.test_current)
        
        # Voltage compliance
        self.voltage_compliance = QDoubleSpinBox()
        self.voltage_compliance.setRange(0.1, 100.0)
        self.voltage_compliance.setDecimals(2)
        self.voltage_compliance.setSingleStep(0.1)
        self.voltage_compliance.setValue(self.settings['measurement']['voltage_compliance'])
        layout.addRow("Voltage Compliance (V):", self.voltage_compliance)
        
        # Sampling rate
        self.sampling_rate = QDoubleSpinBox()
        self.sampling_rate.setRange(0.1, 100.0)
        self.sampling_rate.setDecimals(1)
        self.sampling_rate.setSingleStep(1.0)
        self.sampling_rate.setValue(self.settings['measurement']['sampling_rate'])
        layout.addRow("Sampling Rate (Hz):", self.sampling_rate)
        
        # Auto-range
        self.auto_range = QCheckBox()
        self.auto_range.setChecked(self.settings['measurement']['auto_range'])
        layout.addRow("Auto Range:", self.auto_range)
        
        # NPLC
        self.nplc = QDoubleSpinBox()
        self.nplc.setRange(0.01, 10.0)
        self.nplc.setDecimals(2)
        self.nplc.setSingleStep(0.1)
        self.nplc.setValue(self.settings['measurement']['nplc'])
        layout.addRow("NPLC:", self.nplc)
        
        # Settling time
        self.settling_time = QDoubleSpinBox()
        self.settling_time.setRange(0.0, 10.0)
        self.settling_time.setDecimals(2)
        self.settling_time.setSingleStep(0.1)
        self.settling_time.setValue(self.settings['measurement']['settling_time'])
        layout.addRow("Settling Time (s):", self.settling_time)
        
        # Measurement type
        self.measurement_type = QComboBox()
        self.measurement_type.addItems(["2-wire", "4-wire"])
        self.measurement_type.setCurrentText(self.settings['measurement']['measurement_type'])
        layout.addRow("Measurement Type:", self.measurement_type)
        
        # GPIB address
        self.gpib_address = QLineEdit()
        self.gpib_address.setText(self.settings['measurement']['gpib_address'])
        layout.addRow("GPIB Address:", self.gpib_address)
        
        # Detect GPIB button
        self.detect_gpib_button = QPushButton("Detect GPIB Devices")
        self.detect_gpib_button.clicked.connect(self.detect_gpib_devices)
        layout.addRow("", self.detect_gpib_button)
        
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
        
        # Plot color
        self.plot_color = QComboBox()
        colors = ["red", "blue", "green", "black", "purple", "orange", "darkblue", "darkred"]
        self.plot_color.addItems(colors)
        current_color = self.settings['display']['plot_color']
        if current_color in colors:
            self.plot_color.setCurrentText(current_color)
        layout.addRow("Plot Color:", self.plot_color)
        
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
            
    def detect_gpib_devices(self):
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
                if resource == self.gpib_address.text():
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
                    self.gpib_address.setText(selected_button.text())
        
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error detecting GPIB devices: {str(e)}")
            
    def save_settings(self):
        """Save the settings and close the dialog."""
        # Update settings dictionary
        self.settings['measurement']['test_current'] = self.test_current.value()
        self.settings['measurement']['voltage_compliance'] = self.voltage_compliance.value()
        self.settings['measurement']['sampling_rate'] = self.sampling_rate.value()
        self.settings['measurement']['auto_range'] = self.auto_range.isChecked()
        self.settings['measurement']['nplc'] = self.nplc.value()
        self.settings['measurement']['settling_time'] = self.settling_time.value()
        self.settings['measurement']['measurement_type'] = self.measurement_type.currentText()
        self.settings['measurement']['gpib_address'] = self.gpib_address.text()
        
        self.settings['display']['enable_plot'] = self.enable_plot.isChecked()
        self.settings['display']['plot_update_interval'] = self.plot_update_interval.value()
        self.settings['display']['plot_color'] = self.plot_color.currentText()
        self.settings['display']['plot_figsize'] = [self.plot_width.value(), self.plot_height.value()]
        buffer_size = self.buffer_size.value()
        self.settings['display']['buffer_size'] = None if buffer_size == 0 else buffer_size
        
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
class ResistanceMeterApp(QMainWindow):
    """Main application window for ResistaMet."""
    def __init__(self):
        super().__init__()
        
        self.config_manager = ConfigManager()
        self.data_buffer = DataBuffer()
        self.measurement_worker = None
        self.plot_timer = None
        self.current_user = None
        self.user_settings = None
        
        self.setWindowTitle(f"ResistaMet GUI v{__version__}")
        self.setMinimumSize(800, 600)
        
        self.init_ui()
        
        # Select user on startup
        self.select_user()
        
    def init_ui(self):
        """Initialize the UI."""
        # Create central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # Create main layout
        main_layout = QVBoxLayout()
        central_widget.setLayout(main_layout)
        
        # Create top section with user and sample info
        top_layout = QHBoxLayout()
        
        # User info
        user_group = QGroupBox("User")
        user_layout = QHBoxLayout()
        self.user_label = QLabel("No user selected")
        self.change_user_button = QPushButton("Change User")
        self.change_user_button.clicked.connect(self.select_user)
        
        user_layout.addWidget(self.user_label)
        user_layout.addWidget(self.change_user_button)
        user_group.setLayout(user_layout)
        
        # Sample info
        sample_group = QGroupBox("Sample")
        sample_layout = QHBoxLayout()
        self.sample_input = QLineEdit()
        self.sample_input.setPlaceholderText("Enter sample name")
        
        sample_layout.addWidget(self.sample_input)
        sample_group.setLayout(sample_layout)
        
        top_layout.addWidget(user_group)
        top_layout.addWidget(sample_group)
        
        # Measurement type and current display
        info_group = QGroupBox("Measurement Info")
        info_layout = QHBoxLayout()
        self.measurement_type_label = QLabel("Type: --")
        self.test_current_label = QLabel("Current: -- mA")
        
        info_layout.addWidget(self.measurement_type_label)
        info_layout.addWidget(self.test_current_label)
        info_group.setLayout(info_layout)
        
        top_layout.addWidget(info_group)
        
        # Add top section to main layout
        main_layout.addLayout(top_layout)
        
        # Create plot canvas
        self.canvas = MplCanvas(self, width=10, height=6, dpi=100)
        self.toolbar = NavigationToolbar(self.canvas, self)
        
        plot_layout = QVBoxLayout()
        plot_layout.addWidget(self.toolbar)
        plot_layout.addWidget(self.canvas)
        
        # Create control panel
        control_panel = QGroupBox("Controls")
        control_layout = QVBoxLayout()
        
        # Buttons
        button_layout = QHBoxLayout()
        
        self.start_button = QPushButton("Start Measurement")
        self.start_button.clicked.connect(self.start_measurement)
        
        self.pause_button = QPushButton("Pause")
        self.pause_button.clicked.connect(self.pause_measurement)
        self.pause_button.setEnabled(False)
        
        self.max_button = QPushButton("Mark Max Compression (M)")
        self.max_button.clicked.connect(self.mark_max_compression)
        self.max_button.setEnabled(False)
        
        self.stop_button = QPushButton("Stop Measurement")
        self.stop_button.clicked.connect(self.stop_measurement)
        self.stop_button.setEnabled(False)
        
        button_layout.addWidget(self.start_button)
        button_layout.addWidget(self.pause_button)
        button_layout.addWidget(self.max_button)
        button_layout.addWidget(self.stop_button)
        
        # Settings button
        settings_layout = QHBoxLayout()
        
        self.settings_button = QPushButton("User Settings")
        self.settings_button.clicked.connect(self.open_user_settings)
        
        settings_layout.addStretch()
        settings_layout.addWidget(self.settings_button)
        
        # Add layouts to control panel
        control_layout.addLayout(button_layout)
        control_layout.addLayout(settings_layout)
        
        control_panel.setLayout(control_layout)
        
        # Create status display
        self.status_display = QTextEdit()
        self.status_display.setReadOnly(True)
        self.status_display.setMaximumHeight(100)
        
        # Add widgets to main layout
        main_layout.addLayout(plot_layout, stretch=1)
        main_layout.addWidget(control_panel)
        main_layout.addWidget(QLabel("Status:"))
        main_layout.addWidget(self.status_display)
        
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
        
        # Help menu
        help_menu = menu_bar.addMenu("Help")
        
        about_action = QAction("About", self)
        about_action.triggered.connect(self.show_about)
        
        help_menu.addAction(about_action)
        
        # Create keyboard shortcuts
        self.shortcut_max = QShortcut(Qt.Key_M, self)
        self.shortcut_max.activated.connect(self.mark_max_compression)
        
    def select_user(self):
        """Open user selection dialog."""
        dialog = UserSelectionDialog(self.config_manager, self)
        
        if dialog.exec_():
            username = dialog.selected_user
            
            if username:
                self.current_user = username
                self.user_label.setText(f"User: {username}")
                self.user_settings = self.config_manager.get_user_settings(username)
                
                # Update displayed settings
                self.update_settings_display()
                
                # Reset plot
                buffer_size = self.user_settings['display']['buffer_size']
                self.data_buffer = DataBuffer(size=buffer_size)
                self.canvas.clear_plot()
                
                # Update status
                self.log_status(f"User selected: {username}")
                self.statusBar().showMessage(f"User: {username}")
                
    def update_settings_display(self):
        """Update the displayed settings in the UI."""
        if not self.user_settings:
            return
            
        # Update measurement type and current display
        measurement_type = self.user_settings['measurement']['measurement_type']
        self.measurement_type_label.setText(f"Type: {measurement_type}")
        
        test_current_ma = self.user_settings['measurement']['test_current'] * 1000
        self.test_current_label.setText(f"Current: {test_current_ma:.2f} mA")
        
        # Update plot settings
        color = self.user_settings['display']['plot_color']
        self.canvas.set_plot_color(color)
        
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
                self.data_buffer = DataBuffer(size=new_buffer_size)
                self.canvas.clear_plot()
            
    def open_global_settings(self):
        """Open global settings dialog."""
        dialog = SettingsDialog(self.config_manager, parent=self)
        dialog.exec_()
        
    def start_measurement(self):
        """Start the measurement process."""
        if not self.current_user:
            QMessageBox.warning(self, "No User Selected", "Please select a user first.")
            return
            
        sample_name = self.sample_input.text().strip()
        if not sample_name:
            QMessageBox.warning(self, "No Sample Name", "Please enter a sample name.")
            return
            
        # Disable controls
        self.start_button.setEnabled(False)
        self.settings_button.setEnabled(False)
        self.change_user_button.setEnabled(False)
        self.sample_input.setEnabled(False)
        
        # Enable other buttons
        self.pause_button.setEnabled(True)
        self.pause_button.setText("Pause")
        self.max_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        
        # Clear old data
        self.data_buffer.clear()
        self.canvas.clear_plot()
        
        # Create and start worker thread
        self.measurement_worker = MeasurementWorker(
            sample_name, self.current_user, self.user_settings
        )
        
        # Connect signals
        self.measurement_worker.data_point.connect(self.update_data)
        self.measurement_worker.status_update.connect(self.log_status)
        self.measurement_worker.measurement_complete.connect(self.on_measurement_complete)
        self.measurement_worker.error_occurred.connect(self.on_error)
        
        # Start worker
        self.measurement_worker.start()
        
        # Start plot update timer
        update_interval = self.user_settings['display']['plot_update_interval']
        self.plot_timer = QTimer()
        self.plot_timer.timeout.connect(self.update_plot)
        self.plot_timer.start(update_interval)
        
        # Update status
        self.log_status(f"Measurement started with sample: {sample_name}")
        self.statusBar().showMessage("Measurement running...")
        
    def pause_measurement(self):
        """Pause or resume the measurement."""
        if not self.measurement_worker:
            return
            
        if self.measurement_worker.paused:
            # Resume
            self.measurement_worker.resume_measurement()
            self.pause_button.setText("Pause")
        else:
            # Pause
            self.measurement_worker.pause_measurement()
            self.pause_button.setText("Resume")
            
    def mark_max_compression(self):
        """Mark a maximum compression point."""
        if not self.measurement_worker or self.measurement_worker.paused:
            return
            
        self.measurement_worker.mark_max_compression()
        
    def stop_measurement(self):
        """Stop the measurement."""
        if not self.measurement_worker:
            return
            
        # Stop worker thread
        self.measurement_worker.stop_measurement()
        
        # Update UI
        self.stop_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.max_button.setEnabled(False)
        
        # Show stopping message
        self.log_status("Stopping measurement...")
        self.statusBar().showMessage("Stopping measurement...")
        
    def update_data(self, timestamp, resistance, event):
        """Update data buffer with new measurement data."""
        if np.isfinite(resistance):
            self.data_buffer.add(timestamp, resistance)
            
    def update_plot(self):
        """Update the plot with current data."""
        if not self.data_buffer:
            return
            
        timestamps, resistances = self.data_buffer.get_data_for_plot()
        
        self.canvas.update_plot(
            timestamps, 
            resistances, 
            self.data_buffer.stats, 
            self.current_user, 
            self.sample_input.text()
        )
        
    def on_measurement_complete(self):
        """Handle measurement completion."""
        # Stop plot timer
        if self.plot_timer:
            self.plot_timer.stop()
            
        # Update UI
        self.start_button.setEnabled(True)
        self.settings_button.setEnabled(True)
        self.change_user_button.setEnabled(True)
        self.sample_input.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.max_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        
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
            
        # Update UI
        self.start_button.setEnabled(True)
        self.settings_button.setEnabled(True)
        self.change_user_button.setEnabled(True)
        self.sample_input.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.max_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        
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
        <h2>ResistaMet GUI</h2>
        <p>Version: {__version__}</p>
        <p>Based on original version: {__original_version__}</p>
        <p>Author: {__author__}</p>
        <p>A graphical interface for the ResistaMet resistance measurement system.</p>
        """
        
        QMessageBox.about(self, "About ResistaMet GUI", about_text)
        
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
    window = ResistanceMeterApp()
    window.show()
    
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()

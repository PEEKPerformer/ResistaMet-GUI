# ResistaMet GUI

A graphical user interface for the ResistaMet resistance measurement system.

**Version:** 1.0.0  
**Author:** Brenden Ferland  
**Based on:** ResistaMet v0.9.2

![ResistaMet GUI Screenshot](resistamet-gui-screenshot.PNG)

## Overview

ResistaMet GUI is a PyQt-based graphical interface for the original ResistaMet resistance measurement system. It provides a user-friendly way to configure, run, and visualize resistance measurements through a Keithley measurement instrument, with all the functionality of the original command-line application now accessible through an intuitive graphical interface.

## Features

- **Real-time Data Visualization:**
  - Live plotting of resistance measurements
  - Interactive matplotlib-based graph with zoom, pan, and export capabilities
  - Real-time display of min/max/avg statistics

- **Multi-user Support:**
  - Per-user configuration settings
  - User-specific data storage directories
  - Quick user switching

- **Measurement Controls:**
  - Start/pause/stop measurement functionality
  - "Mark maximum compression" button (with keyboard shortcut 'M')
  - Live status updates

- **Configuration Options:**
  - Measurement parameters (current, voltage compliance, sampling rate, etc.)
  - Display settings (plot colors, update intervals, buffer size)
  - File storage settings

- **Equipment Integration:**
  - Automatic GPIB device detection
  - 2-wire and 4-wire measurement modes
  - Configurable instrument settings

- **Data Management:**
  - Automatic data saving to CSV files
  - Configurable auto-save intervals
  - Plot export functionality

## Installation

### Requirements

- Python 3.6 or higher
- PyQt5
- PyVISA
- Matplotlib
- NumPy

### Installation Steps

1. **Install the required Python packages:**

   ```bash
   pip install pyqt5 pyvisa pyvisa-py numpy matplotlib
   ```

2. **Hardware-specific dependencies:**
   
   Depending on your GPIB interface, you may need additional drivers:
   
   - For National Instruments GPIB interfaces: Install NI-VISA
   - For other GPIB adapters: Install appropriate drivers

3. **Clone or download the ResistaMet GUI code**

4. **Run the application:**

   ```bash
   python resistamet_gui.py
   ```

## Usage Guide

### Getting Started

1. **Select or create a user:**
   - At startup, you'll be prompted to select an existing user or create a new one
   - User-specific settings will be loaded automatically

2. **Configure your measurement:**
   - Enter a sample name
   - Confirm or adjust measurement settings via the "User Settings" button
   - Verify that the correct GPIB device is selected

3. **Start a measurement:**
   - Click "Start Measurement" to begin
   - Live resistance values will be displayed in the plot
   - The status area will show current operation information

### During Measurement

- **Marking Max Compression:**
  - Click the "Mark Max Compression" button or press the 'M' key when maximum compression is reached
  - This event will be recorded in the data file

- **Pause/Resume:**
  - The "Pause" button temporarily halts data collection
  - Click "Resume" to continue

- **Stopping:**
  - Click "Stop Measurement" to end the measurement
  - Data will be automatically saved and the plot will remain visible

### Data Management

- Data is automatically saved to CSV files in the configured data directory
- Each user has their own subdirectory for data storage
- Files are named with a timestamp, sample name, and measurement parameters
- The plot can be saved via File → Save Plot

## Configuration Options

### Measurement Settings

- **Test Current:** Current applied by the instrument (in Amperes)
- **Voltage Compliance:** Maximum allowed voltage (in Volts)
- **Sampling Rate:** Number of measurements per second (in Hz)
- **Auto Range:** Enable/disable automatic range selection
- **NPLC:** Number of power line cycles for integration
- **Settling Time:** Delay before measurements begin (in seconds)
- **Measurement Type:** 2-wire or 4-wire measurement
- **GPIB Address:** Address of the measurement instrument

### Display Settings

- **Enable Plot:** Toggle real-time plotting
- **Plot Update Interval:** Time between plot updates (in milliseconds)
- **Plot Color:** Color of the resistance plot line
- **Plot Figure Size:** Width and height of the plot
- **Buffer Size:** Number of data points to keep in memory (unlimited by default)

### File Settings

- **Auto-save Interval:** Time between automatic saves (in seconds)
- **Data Directory:** Base folder for data storage

## File Structure

```
resistance_data/
├── username1/
│   ├── timestamp_samplename_R_2-wire_1.0mA.csv
│   └── ...
├── username2/
│   └── ...
└── ...
```

## Troubleshooting

### Common Issues

1. **GPIB Connection Problems:**
   - Ensure your GPIB interface is properly installed
   - Verify the correct GPIB address is configured
   - Check that the instrument is powered on and connected

2. **PyVISA Errors:**
   - Make sure you have the appropriate backend installed
   - For NI-VISA, ensure the drivers are installed correctly
   - For other backends, consult PyVISA documentation

3. **GUI Display Issues:**
   - Try an alternative Qt style (e.g., "Fusion" or "Windows")
   - Update your graphics drivers
   - Ensure your display scaling is set appropriately

### Error Reporting

If you encounter errors during operation, they will be:
- Displayed in the status area at the bottom of the window
- Shown in error message dialogs for critical issues
- Written to the console output

## License

ResistaMet GUI is provided under the MIT License with Academic Citation Clause, the same as the original ResistaMet application.

## Contact

For support, bug reports, or feature requests, please contact the author or create an issue in the repository.
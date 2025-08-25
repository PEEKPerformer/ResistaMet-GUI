import os

# Script version and metadata
__version__ = "1.2.0"
__original_version__ = "0.9.2"
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
        # Current Source Mode (Source I, Measure V)
        "isource_current": 1.0e-3,           # Source current in Amperes
        "isource_voltage_compliance": 5.0,   # Voltage compliance in Volts for I source mode
        "isource_voltage_range_auto": True,  # Auto range for voltage measurement
        "isource_duration_hours": 1.0,       # Duration to apply current in hours
        # General
        "sampling_rate": 10.0,               # Sampling rate in Hz (shared for now)
        "nplc": 1,                           # Number of power line cycles (shared)
        "settling_time": 0.2,                # Settling time in seconds (shared)
        "gpib_address": "GPIB0::24::INSTR",  # GPIB address of the instrument
        "stop_on_compliance": false,         # Stop run when compliance is hit
        # Four-Point Probe (FPP) defaults (SP4-40085TBQ)
        "fpp_current": 1.0e-3,               # Source current in Amperes
        "fpp_voltage_compliance": 5.0,       # Voltage compliance (V)
        "fpp_voltage_range_auto": true,      # Auto range for voltage measurement
        "fpp_spacing_cm": 0.1016,            # s = 0.040 inches = 0.1016 cm
        "fpp_thickness_cm": 0.0,             # optional, 0 means unknown
        "fpp_alpha": 1.0,                    # thickness correction factor
        "fpp_model": "thin_film"             # one of: thin_film, semi_infinite, finite_thin, finite_alpha
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

# Keithley compliance heuristics
KEITHLEY_COMPLIANCE_MAGIC_NUMBER = 9.9e37
COMPLIANCE_THRESHOLD_FACTOR = 1.0

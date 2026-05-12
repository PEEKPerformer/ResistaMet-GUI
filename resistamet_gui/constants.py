import os

# Script version and metadata
__version__ = "1.8.0"
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
        "stop_on_compliance": False,
        "auto_zero": "on",                   # "on" (accurate), "once" (fast), "off" (fastest, drifts)
        "filter_enabled": True,              # enable built-in Keithley averaging filter
        "filter_type": "repeat",             # "repeat" or "moving"
        "filter_count": 10,                  # number of readings to average (1-100)
        "res_offset_comp": False,            # offset-compensated ohms (cancels thermoelectric EMF)
        "res_cable_null": 0.0,               # cable null reference value (0 = disabled)         # Stop run when compliance is hit
        # Four-Point Probe (FPP) defaults (SP4-40085TBQ)
        "fpp_current": 1.0e-4,               # Source current in Amperes (100 µA — safe for unknown films)
        "fpp_voltage_compliance": 5.0,       # Voltage compliance (V)
        "fpp_voltage_range_auto": True,      # Auto range for voltage measurement
        "fpp_spacing_cm": 0.1016,            # s = 0.040 inches = 0.1016 cm
        "fpp_thickness_um": 0.0,            # thin-film thickness in micrometers (µm); 0 = unknown
        "fpp_alpha": 1.0,                    # thickness correction factor
        "fpp_k_factor": 4.532,              # geometric coefficient replacing 4.532 when needed
        "fpp_samples": 0,                  # number of samples to take (0 = continuous)
        "fpp_model": "thin_film",             # one of: thin_film, semi_infinite, finite_thin, finite_alpha
        # F84-aligned correction factor inputs. Defaults reproduce legacy behavior
        # (infinite-diameter circle, no temperature correction) so existing config
        # files keep producing the same numbers.
        "fpp_diameter_cm": 0.0,               # specimen diameter D, cm. 0 = treat as infinite (F2 = 4.5324)
        "fpp_geometry": "circle",             # one of: circle, square, rectangle_2, rectangle_3, rectangle_4
        "fpp_temperature_c": float('nan'),    # measurement temperature, °C. NaN = no temperature correction
        "fpp_dopant_type": "none",            # 'n', 'p', or 'none' (none = skip F_T even if temperature is set)
        "fpp_delta_mode": False,              # current reversal (delta) mode — alternates +I/-I to cancel thermoelectric EMF
        "fpp_delta_settling": 0.1,            # settling time (s) between polarity flips in delta mode
        # 4PP probe safety. Defaults sized for the Signatone SP4 series:
        # tungsten-carbide tips with ~100 mA continuous spec; tens of mW is a
        # conservative warning threshold for thin films and conductive polymers
        # where local Joule heating can melt the sample before the probe.
        "fpp_power_warn_w": 1.0e-2,           # warn (status flash) above this measured V*I, watts
        "fpp_power_stop_w": 1.0e-1,           # hard stop above this measured V*I, watts
        "fpp_stop_on_overpower": True,        # abort 4PP run if measured power exceeds fpp_power_stop_w
        # Van der Pauw (vdP) defaults per ASTM F76-08 Method A. 4 manual lead
        # reconnections, current reversal automated at each geometry → 8
        # voltage readings total. Thickness in cm to match F76 units.
        "vdp_current": 1.0e-3,                # source current magnitude (A)
        "vdp_voltage_compliance": 5.0,        # V compliance for the source
        "vdp_voltage_range_auto": True,       # auto-range voltage measurement
        "vdp_thickness_cm": 1.0e-4,           # sample thickness, cm (1 µm default)
        "vdp_settling_s": 0.2,                # delay after polarity flip before READ?
        "vdp_readings_per_polarity": 1,       # average N hardware readings per +I and -I
        # I-V Sweep defaults
        "sweep_source": "voltage",           # "voltage" or "current"
        "sweep_start": 0.0,                  # sweep start value (V or A)
        "sweep_stop": 1.0,                   # sweep stop value (V or A)
        "sweep_step": 0.05,                  # sweep step size (V or A)
        "sweep_compliance": 0.1,             # compliance limit (A or V)
        "sweep_delay": 0.01,                 # source delay per step (s)
        "sweep_direction": "up"              # "up", "down", or "up_down"
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

import os

# Script version and metadata
__version__ = "1.12.3"
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
        "vsource_duration_hours": 0.0,       # Duration to apply voltage in hours (0 = run until stopped)
        # Current Source Mode (Source I, Measure V)
        "isource_current": 1.0e-3,           # Source current in Amperes
        "isource_voltage_compliance": 5.0,   # Voltage compliance in Volts for I source mode
        "isource_voltage_range_auto": True,  # Auto range for voltage measurement
        "isource_duration_hours": 0.0,       # Duration to apply current in hours (0 = run until stopped)
        # General
        "sampling_rate": 10.0,               # Sampling rate in Hz (shared for now)
        "nplc": 1,                           # Number of power line cycles (shared)
        "settling_time": 0.2,                # Settling time in seconds (shared)
        "gpib_address": "GPIB0::24::INSTR",  # GPIB address of the instrument
        "stop_on_compliance": False,
        # auto_zero=once cuts each reading from 3 integrations to 1 (a 3×
        # speedup) by caching the zero/reference at run start. Acceptable
        # for sensor work where R/R0 vs t is the signal of interest and
        # absolute-zero drift over a single run is below the noise floor.
        # Switch to "on" for absolute-accuracy runs longer than ~30 min.
        "auto_zero": "once",
        "filter_enabled": True,              # enable built-in Keithley averaging filter
        "filter_type": "repeat",             # "repeat" or "moving"
        # filter_count=5 keeps NPLC=1 line-noise rejection per conversion
        # while halving the per-reading time vs the old 10. Run-level stats
        # over many samples are unchanged; the live trace is slightly more
        # jittery per point but smoother in aggregate.
        "filter_count": 5,                   # number of readings to average (1-100)
        # Enhanced-accuracy resistance mode: source-readback (always on
        # here via :FORM:ELEM VOLT,CURR,RES,STAT) + offset-compensated
        # ohms. Reports σ_R from the datasheet's Enhanced R column
        # (~30% tighter than V/I propagation). Throughput halves. ON by
        # default because a tool aimed at precision measurement should
        # ship the accurate-by-default setting; fast scans opt out.
        "res_offset_comp": True,             # offset-compensated ohms / Enhanced accuracy
        "res_cable_null": 0.0,               # cable null reference value (0 = disabled)         # Stop run when compliance is hit
        # Four-Point Probe (FPP) defaults (SP4-40085TBQ)
        "fpp_current": 1.0e-4,               # Source current in Amperes (100 µA — safe for unknown films)
        "fpp_voltage_compliance": 5.0,       # Voltage compliance (V)
        "fpp_voltage_range_auto": True,      # Auto range for voltage measurement
        "fpp_spacing_cm": 0.1016,            # s = 0.040 inches = 0.1016 cm
        "fpp_thickness_um": 0.0,            # thin-film thickness in micrometers (µm); 0 = unknown
        "fpp_alpha": 1.0,                    # thickness correction factor
        "fpp_k_factor": 4.532,              # geometric coefficient replacing 4.532 when needed
        "fpp_samples": 20,                 # number of samples per spot before auto-stop (0 = continuous)
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
        # 0 = unset; vdP Start prompts the user. Avoids silently reporting
        # ρ = R_s × 1 µm when the user never actually entered a thickness.
        "vdp_thickness_cm": 0.0,              # sample thickness, cm (0 = unset, prompt on Start)
        "vdp_settling_s": 0.2,                # delay after polarity flip before READ?
        "vdp_readings_per_polarity": 5,       # average N hardware readings per +I and -I
        # I-V Sweep defaults
        "sweep_source": "voltage",           # "voltage" or "current"
        "sweep_start": 0.0,                  # sweep start value (V or A)
        "sweep_stop": 1.0,                   # sweep stop value (V or A)
        "sweep_step": 0.05,                  # sweep step size (V or A)
        "sweep_compliance": 0.1,             # compliance limit (A or V)
        "sweep_delay": 0.01,                 # source delay per step (s)
        "sweep_direction": "up",             # "up", "down", or "up_down"
        # Human-touch-safety voltage warning. See resistamet_gui/safety.py
        # for the rationale (IEC 61010-1 SELV at 30 V DC). Set to 0 to
        # disable; the silenced flag flips when a user clicks "don't show
        # again" on the warning dialog and stays per-profile until they
        # re-enable in Settings.
        "safety_voltage_warn_v": 30.0,       # threshold in V; 0 disables
        "safety_voltage_warn_silenced": False
    },
    "display": {
        "enable_plot": True,
        # 16 ms ≈ 60 fps, synced to a typical 60 Hz monitor. Used to be
        # 200 ms when the live canvas was matplotlib; pyqtgraph renders
        # for free at this cadence. The main_window timer also caps at
        # 16 ms so older saved configs still get the snappy feel.
        "plot_update_interval": 16,          # Plot update interval in milliseconds
        # Plot line colors — Wong (Nature Methods 2011) colorblind-safe
        # palette. Matched by the readout-strip label colors in
        # ui/widgets.py so the live label hue equals the trace hue.
        # Existing configs override these via deep-merge.
        "plot_color_r": "#D55E00",           # Wong vermillion (Resistance)
        "plot_color_v": "#0072B2",           # Wong blue (Voltage Source / Current trace)
        "plot_color_i": "#009E73",           # Wong bluish green (Current Source / Voltage trace)
        "plot_figsize": [8, 5],              # Plot figure size [width, height]
        # Unlimited by default — pyqtgraph downsamples on render (peak mode +
        # clipToView), so a 17-hr / 270k-sample run is ~13 MB and stays smooth.
        # A bounded buffer silently truncated the live trace on overnight runs.
        "buffer_size": 0                     # Data buffer size (points, 0 or None = unlimited)
    },
    "file": {
        "auto_save_interval": 60,            # Auto-save interval in seconds
        "data_directory": "measurement_data" # Base directory for data storage
    },
    "output": {
        # Exporter selection. See resistamet_gui/data_export.py.
        #   "csv"               single .csv with #-prefixed metadata header (default)
        #   "hdf5"              single .h5, gzip-compressed, metadata in attrs (needs h5py)
        #   "csv+legacy_json"   pre-2.0 dual .csv + .json emit (back-compat)
        "format": "csv",
        # Gzip policy applied at finalize for the "csv" backend only.
        #   "never"  no gzip (default; many lab tools can't open .gz directly)
        #   "always" always gzip the .csv
        #   "auto"   gzip only when the .csv exceeds compression_threshold_mb
        "compression": "never",
        "compression_threshold_mb": 5
    },
    "users": [],
    "last_user": None
}


# Some modes optimize for the tightest possible number rather than fast
# real-time visibility. 4PP and vdP are static spot measurements — the
# sample isn't evolving during a single spot, so the right defaults are
# the slow ones (full auto-zero, maximum hardware averaging) regardless
# of what the shared 'measurement' block says. gather_settings_for_mode
# applies these on top of the loaded settings right before the worker
# starts. Sensor modes (resistance, source_v, source_i) are intentionally
# absent: they want the snappy shared defaults so live R-vs-t looks
# alive on screen.
MODE_TIMING_OVERRIDES = {
    'four_point': {'auto_zero': 'on', 'filter_count': 10},
    'vdp':        {'auto_zero': 'on', 'filter_count': 10},
}

# Keithley compliance heuristics
KEITHLEY_COMPLIANCE_MAGIC_NUMBER = 9.9e37
COMPLIANCE_THRESHOLD_FACTOR = 1.0

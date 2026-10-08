/* eslint-disable */
// GENERATED from contracts/*.schema.json by desktop/scripts/generate-types.mjs.
// Do not edit. Regenerate with `npm run gen:types`.

/**
 * The verdict on a run an agent would start. ``ok`` when nothing is beyond.
 */
export interface AgentLimitCheck {
  ok: boolean;
  violations: AgentLimitViolation[];
}
/**
 * One way a run goes beyond what an agent may start.
 *
 * ``limit`` names the bound: one of the profile's ``agent_limits`` keys
 * when ``source`` is ``'agent_limits'``, a ``ModelSpec`` field when it is
 * ``'model'`` (and ``model`` says which). ``keys`` are the run's setting
 * keys that produce ``value``; ``value`` is None when the instrument
 * chooses the quantity itself, and ``allowed`` is None when the profile's
 * limit is itself not a valid number.
 */
export interface AgentLimitViolation {
  allowed: number | null;
  keys: string[];
  limit: "max_voltage_v" | "max_current_a" | "max_power_w" | "max_source_v" | "max_source_i";
  message: string;
  model: string | null;
  source: "agent_limits" | "model";
  value: number | null;
}

/**
 * What a run an AI agent starts may put on the device. Its own section.
 *
 * A section beside ``measurement`` rather than more keys inside it, because
 * these are not settings of a run: the resolver never copies them into one,
 * so they cannot reach a data file, and a run request only carries
 * measurement keys. They bound the ``agent`` role only; a run a person
 * starts is never held to them (``docs/design/mcp_layer.md`` M4).
 *
 * None is no cap beyond the instrument's own. A cap is a positive, finite
 * number: 0 would refuse every run, which is what turning agents off is for.
 */
export interface AgentLimitSettings {
  /**
   * Largest |I| an agent's run may source or allow as compliance, in A. None = the instrument's own limit.
   */
  max_current_a?: number | null;
  /**
   * Largest |V| x |I| an agent's run could deliver, in W. None = the instrument's own limit.
   */
  max_power_w?: number | null;
  /**
   * Largest |V| an agent's run may source or allow as compliance, in V. None = the instrument's own limit.
   */
  max_voltage_v?: number | null;
}

/**
 * Auxiliary-sensor co-logging. Serialized inside ``measurement``.
 *
 * Co-logging is only wired into the continuous modes
 * (``data_export.AUX_LOG_MODES``); the resolver reports an issue when a
 * request asks for it on a sweep or vdP run.
 */
export interface AuxSensorSettings {
  aux_address?: string;
  aux_driver?: string;
  aux_log_enabled?: boolean;
}

/**
 * Source I, measure V. ``isource_duration_hours`` of 0 runs until stopped.
 */
export interface CurrentSourceSettings {
  isource_current?: number;
  /**
   * 0 = until stopped.
   */
  isource_duration_hours?: number;
  isource_voltage_compliance?: number;
  isource_voltage_range_auto?: boolean;
}

/**
 * Plot appearance. Frontend-only, modelled so a profile round-trips.
 */
export interface DisplaySettings {
  buffer_size?: number | null;
  enable_plot?: boolean;
  plot_color_i?: string;
  plot_color_r?: string;
  plot_color_v?: string;
  plot_figsize?: number[];
  plot_update_interval?: number;
}

/**
 * Where rows are written and how often they are flushed.
 */
export interface FileSettings {
  auto_save_interval?: number;
  data_directory?: string;
}

/**
 * Four-point probe, with the ASTM F84 correction-factor inputs.
 *
 * ``fpp_temperature_c`` is ``None`` when the temperature was not measured.
 * The PySide6 UI carries that as a -50 C sentinel on its spin box (the
 * widget's "not measured" special value) and a profile as NaN. The resolver
 * shows this model ``None`` in place of NaN and converts nothing in the run
 * settings: NaN from a profile stays NaN and a JSON client's ``null`` stays
 * ``None``. The consumers read both as "not measured".
 */
export interface FourPointSettings {
  /**
   * Finite-size correction multiplying K (fpp_model thin_film).
   */
  fpp_alpha?: number;
  fpp_array_angle_deg?: number;
  fpp_current?: number;
  /**
   * Current reversal: each reading combines +I and -I, cancelling thermal offsets.
   */
  fpp_delta_mode?: boolean;
  /**
   * Wait after each reversal, in s.
   */
  fpp_delta_settling?: number;
  /**
   * Sample diameter (or width) for the ASTM F84 F2 correction; 0 = infinite. Above 0 the F84 corrections apply.
   */
  fpp_diameter_cm?: number;
  /**
   * Silicon dopant type for the F84 temperature correction.
   */
  fpp_dopant_type?: "none" | "n" | "p";
  fpp_edge_warn_pct?: number;
  /**
   * Sample shape for the F2 correction (rectangle_N: length = N x width). Anything but circle applies the F84 corrections.
   */
  fpp_geometry?: "circle" | "square" | "rectangle_2" | "rectangle_3" | "rectangle_4";
  /**
   * Geometric factor K: Rs = K x V/I; 4.532 for an infinite thin sheet.
   */
  fpp_k_factor?: number;
  /**
   * thin_film: Rs = K x alpha x V/I, rho = Rs x t; finite_thin: the same without alpha; semi_infinite: rho = 2 pi s x V/I (bulk, t >> s); finite_alpha: rho = alpha x 2 pi s x V/I. Not used when an ASTM F84 input is set (fpp_diameter_cm > 0, fpp_geometry not circle, or fpp_temperature_c with fpp_dopant_type n or p): the F84 corrections apply instead.
   */
  fpp_model?: "thin_film" | "semi_infinite" | "finite_thin" | "finite_alpha";
  fpp_position_correction?: "warn";
  /**
   * The run refuses to start when |fpp_current| x fpp_voltage_compliance is above this, and with fpp_stop_on_overpower stops when a measured V x I is.
   */
  fpp_power_stop_w?: number;
  /**
   * The run warns when |fpp_current| x fpp_voltage_compliance is above this.
   */
  fpp_power_warn_w?: number;
  fpp_sample_diameter_mm?: number;
  fpp_sample_length_mm?: number;
  /**
   * Sample outline for a spot's position check only; unbounded: fpp_geometry and fpp_diameter_cm describe it.
   */
  fpp_sample_shape?: "unbounded" | "circle" | "rectangle";
  fpp_sample_width_mm?: number;
  /**
   * Readings to take, then stop; 0 = until stopped.
   */
  fpp_samples?: number;
  fpp_spacing_cm?: number;
  fpp_stop_on_overpower?: boolean;
  /**
   * Silicon sample temperature for the F84 correction to 23 C, with fpp_dopant_type; null = not measured.
   */
  fpp_temperature_c?: number | null;
  /**
   * 0 = not given: sheet resistance only, no resistivity or conductivity (except fpp_model semi_infinite, which does not use it).
   */
  fpp_thickness_um?: number;
  fpp_voltage_compliance?: number;
  fpp_voltage_range_auto?: boolean;
}

/**
 * Knobs that apply to every mode, wherever the value comes from.
 *
 * ``gpib_address``, ``visa_library``, ``gpib_interface`` and
 * ``allow_agents`` are machine-local — ``ConfigManager`` keeps them in this
 * machine's own file and injects them into the profile on read, so they are
 * never stored per user (``config.py``).
 */
export interface InstrumentSettings {
  /**
   * Let AI agents connect to a backend on this machine, through an MCP server, with a token of their own (role 'agent'). Machine-local; only the 'ui' role may change it.
   */
  allow_agents?: boolean;
  /**
   * on: re-zero with every reading (slower, no drift); once: zero at the start of the run; off: never.
   */
  auto_zero?: "on" | "once" | "off";
  filter_count?: number;
  filter_enabled?: boolean;
  /**
   * repeat: each reading averages filter_count new conversions; moving: a running average.
   */
  filter_type?: "repeat" | "moving";
  gpib_address?: string;
  /**
   * Interface resource of a Prologix-style GPIB adapter (Prologix GPIB-USB / GPIB-ETHERNET, AR488), opened before the instrument so that GPIB<board>::<addr>::INSTR resolves; pyvisa-py only. Empty = none. Serial: PRLGX-ASRL[board]::<device>::INTFC, where <device> is the port path on macOS and Linux (PRLGX-ASRL::/dev/cu.usbserial-PX12345::INTFC, PRLGX-ASRL::/dev/ttyUSB0::INTFC) and the COM port number alone on Windows (PRLGX-ASRL::5::INTFC for COM5). Ethernet: PRLGX-TCPIP[board]::<host>[::port]::INTFC, port 1234 by default. [board] defaults to 0 and is the <board> of the instrument address.
   */
  gpib_interface?: string;
  /**
   * Integration time per reading, in power-line cycles.
   */
  nplc?: number;
  /**
   * Readings per second asked for; the timing settings may allow fewer.
   */
  sampling_rate?: number;
  settling_time?: number;
  /**
   * End the run when the output reaches its compliance limit.
   */
  stop_on_compliance?: boolean;
  visa_library?: string;
}

/**
 * Exporter selection; consumed by ``data_export.make_exporter``.
 */
export interface OutputSettings {
  compression?: "never" | "always" | "auto";
  compression_threshold_mb?: number;
  format?: "csv" | "hdf5" | "csv+legacy_json";
}

/**
 * Source I, measure R. 2-wire or 4-wire, optional cable null.
 */
export interface ResistanceSettings {
  /**
   * Auto-ohms: the instrument chooses its own test current and voltage limit.
   */
  res_auto_range?: boolean;
  /**
   * Lead resistance subtracted from every reading, in ohms; set by the cable-null procedure.
   */
  res_cable_null?: number;
  /**
   * 4-wire senses the voltage at the sample on separate leads (remote sense); 2-wire includes the leads' resistance.
   */
  res_measurement_type?: "2-wire" | "4-wire";
  /**
   * Offset-compensated ohms: cancels thermal EMFs, halves the reading rate.
   */
  res_offset_comp?: boolean;
  res_test_current?: number;
  res_voltage_compliance?: number;
}

/**
 * What a client asks for. Never persisted.
 *
 * This is the body ``POST /session/start`` validates, and the model the
 * desktop's request type is generated from; unknown fields are refused so
 * a misspelt one cannot be silently ignored.
 *
 * ``overrides`` is flat measurement keys, the same shape the tabs produce
 * today, so one request format serves the UI, a script and the MCP layer.
 * Strict validation of the keys happens in the resolver, which knows which
 * ones the profile owns.
 */
export interface RunRequest {
  client?: ClientInfo | null;
  mode: "resistance" | "source_v" | "source_i" | "four_point" | "sweep" | "vdp";
  overrides?: {};
  prompt_timeout_s?: number;
  sample_name: string;
  spot?: SpotRequest | null;
  username: string;
}
/**
 * Which program asked for the run, recorded in the file header.
 *
 * The backend's own version is always written (``software_version``); this
 * says what was driving it -- the desktop app, a script, the MCP layer --
 * so a file written through the API can be told from one the PySide6 app
 * wrote. Self-reported, so it is provenance and not authentication.
 */
export interface ClientInfo {
  name: string;
  version: string;
}
/**
 * One placement of the probe, as the client describes it.
 *
 * The position is optional -- a spot can be a label and nothing more -- but
 * ``x_mm`` and ``y_mm`` only mean something together. ``angle_deg`` is the
 * direction of the probe array, anticlockwise from +x; absent, the run uses
 * the ``fpp_array_angle_deg`` setting.
 */
export interface SpotRequest {
  angle_deg?: number | null;
  index: number;
  label: string;
  map_id: string;
  x_mm?: number | null;
  y_mm?: number | null;
}

/**
 * Touch-safety warning threshold. Serialized inside ``measurement``.
 *
 * ``safety_voltage_warn_silenced`` is the sticky per-profile flag behind the
 * warning dialog's "don't show again" checkbox.
 * ``safety_voltage_warn_silenced_until`` is the timed form, a Unix time:
 * the warning is silenced until then and back by itself afterwards. An
 * expired value is ignored, never cleared (``safety.warning_silenced``).
 *
 * The threshold runs to 1100 V, the 2410's range and the Settings dialog's
 * maximum: a threshold the dialog can save must not make the profile
 * unrunnable here. 0 disables the warning. Every key here belongs to the
 * profile; a strict run request may not send any (``resolve.SAFETY_KEYS``).
 */
export interface SafetySettings {
  safety_voltage_warn_silenced?: boolean;
  safety_voltage_warn_silenced_until?: number | null;
  safety_voltage_warn_v?: number;
}

/**
 * The lateral outline of a thin sample with insulating edges.
 *
 * ``unbounded`` means a sheet large enough that its edges do not matter,
 * which is what the software assumed before it knew about outlines.
 */
export interface SampleGeometry {
  diameter_mm?: number | null;
  length_mm?: number | null;
  shape?: "unbounded" | "circle" | "rectangle";
  width_mm?: number | null;
}

/**
 * Something the run will warn about once it is going, said beforehand.
 *
 * Not an :class:`Issue`: nothing is wrong with the settings, and a start
 * is never refused for one. The run itself would say it, in a log line
 * or not at all (a rate it cannot reach is simply not reached); a
 * preview that stayed silent left a client to find out from the run, or
 * to work it out for itself and disagree. ``keys`` are the settings the
 * judgement reads, the one to change first.
 */
export interface SettingsWarning {
  keys: string[];
  message: string;
}

/**
 * One placement of the probe, as the client describes it.
 *
 * The position is optional -- a spot can be a label and nothing more -- but
 * ``x_mm`` and ``y_mm`` only mean something together. ``angle_deg`` is the
 * direction of the probe array, anticlockwise from +x; absent, the run uses
 * the ``fpp_array_angle_deg`` setting.
 */
export interface SweepSettings {
  /**
   * Limit on the measured quantity: a current, at most 3.15 A, when sweep_source is voltage; a voltage, at most 210 V, when it is current.
   */
  sweep_compliance?: number;
  /**
   * Delay between sourcing and measuring at each point, in s.
   */
  sweep_delay?: number;
  sweep_direction?: "up" | "down" | "up_down";
  sweep_source?: "voltage" | "current";
  /**
   * In the source's unit: V, within +/-200, when sweep_source is voltage; A, within +/-3, when it is current.
   */
  sweep_start?: number;
  /**
   * In the source's unit: V, within +/-200, when sweep_source is voltage; A, within +/-3, when it is current.
   */
  sweep_step?: number;
  /**
   * In the source's unit: V, within +/-200, when sweep_source is voltage; A, within +/-3, when it is current.
   */
  sweep_stop?: number;
}

/**
 * van der Pauw, ASTM F76 Method A.
 *
 * Thickness stays >= 0 here because a stored profile legitimately holds 0 for
 * "never entered"; a run request needs > 0, which the resolver enforces in
 * strict mode (the UI prompts for it on Start).
 */
export interface VdpSettings {
  vdp_current?: number;
  /**
   * Readings averaged at each current direction of each wiring.
   */
  vdp_readings_per_polarity?: number;
  /**
   * Wait after each change of current, in s.
   */
  vdp_settling_s?: number;
  vdp_thickness_cm?: number;
  vdp_voltage_compliance?: number;
  vdp_voltage_range_auto?: boolean;
}

/**
 * Source V, measure I. ``vsource_duration_hours`` of 0 runs until stopped.
 */
export interface VoltageSourceSettings {
  vsource_current_compliance?: number;
  vsource_current_range_auto?: boolean;
  /**
   * 0 = until stopped.
   */
  vsource_duration_hours?: number;
  vsource_voltage?: number;
}

/** Measurement modes and the settings model each one uses. */
export const MODES = ["four_point", "resistance", "source_i", "source_v", "sweep", "vdp"] as const;
export type Mode = (typeof MODES)[number];

export interface ModeSettingsMap {
  four_point: FourPointSettings;
  resistance: ResistanceSettings;
  source_i: CurrentSourceSettings;
  source_v: VoltageSourceSettings;
  sweep: SweepSettings;
  vdp: VdpSettings;
}

/** The settings model each mode's tab writes, by mode. */
export const MODE_MODEL = {
  four_point: "FourPointSettings",
  resistance: "ResistanceSettings",
  source_i: "CurrentSourceSettings",
  source_v: "VoltageSourceSettings",
  sweep: "SweepSettings",
  vdp: "VdpSettings",
} as const;

export interface FieldMeta {
  type?: string;
  nullable?: boolean;
  enum?: readonly (string | number)[];
  min?: number;
  exclusiveMin?: number;
  max?: number;
  exclusiveMax?: number;
  default?: unknown;
  required?: boolean;
}

/** Bounds, defaults and enums per model field, from the schema. */
export const FIELD_META: Record<string, Record<string, FieldMeta>> = {
  "AgentLimitCheck": {
    "ok": {
      "type": "boolean",
      "required": true
    },
    "violations": {
      "type": "array",
      "required": true
    }
  },
  "AgentLimitSettings": {
    "max_current_a": {
      "type": "number",
      "nullable": true,
      "exclusiveMin": 0,
      "default": null
    },
    "max_power_w": {
      "type": "number",
      "nullable": true,
      "exclusiveMin": 0,
      "default": null
    },
    "max_voltage_v": {
      "type": "number",
      "nullable": true,
      "exclusiveMin": 0,
      "default": 30
    }
  },
  "AuxSensorSettings": {
    "aux_address": {
      "type": "string",
      "default": "ASRL6::INSTR"
    },
    "aux_driver": {
      "type": "string",
      "default": "arduino_thermocouple"
    },
    "aux_log_enabled": {
      "type": "boolean",
      "default": false
    }
  },
  "CurrentSourceSettings": {
    "isource_current": {
      "type": "number",
      "min": -3,
      "max": 3,
      "default": 0.001
    },
    "isource_duration_hours": {
      "type": "number",
      "min": 0,
      "max": 168,
      "default": 0
    },
    "isource_voltage_compliance": {
      "type": "number",
      "min": 0.1,
      "max": 200,
      "default": 5
    },
    "isource_voltage_range_auto": {
      "type": "boolean",
      "default": true
    }
  },
  "DisplaySettings": {
    "buffer_size": {
      "type": "integer",
      "nullable": true,
      "min": 0,
      "default": 0
    },
    "enable_plot": {
      "type": "boolean",
      "default": true
    },
    "plot_color_i": {
      "type": "string",
      "default": "#009E73"
    },
    "plot_color_r": {
      "type": "string",
      "default": "#D55E00"
    },
    "plot_color_v": {
      "type": "string",
      "default": "#0072B2"
    },
    "plot_figsize": {
      "type": "array"
    },
    "plot_update_interval": {
      "type": "integer",
      "min": 1,
      "max": 10000,
      "default": 16
    }
  },
  "FileSettings": {
    "auto_save_interval": {
      "type": "integer",
      "min": 1,
      "max": 3600,
      "default": 60
    },
    "data_directory": {
      "type": "string",
      "default": "measurement_data"
    }
  },
  "FourPointSettings": {
    "fpp_alpha": {
      "type": "number",
      "min": 0,
      "max": 10,
      "default": 1
    },
    "fpp_array_angle_deg": {
      "type": "number",
      "min": -360,
      "max": 360,
      "default": 0
    },
    "fpp_current": {
      "type": "number",
      "min": -3,
      "max": 3,
      "default": 0.0001
    },
    "fpp_delta_mode": {
      "type": "boolean",
      "default": false
    },
    "fpp_delta_settling": {
      "type": "number",
      "min": 0.01,
      "max": 5,
      "default": 0.1
    },
    "fpp_diameter_cm": {
      "type": "number",
      "min": 0,
      "max": 100,
      "default": 0
    },
    "fpp_dopant_type": {
      "type": "string",
      "enum": [
        "none",
        "n",
        "p"
      ],
      "default": "none"
    },
    "fpp_edge_warn_pct": {
      "type": "number",
      "min": 0,
      "max": 100,
      "default": 1
    },
    "fpp_geometry": {
      "type": "string",
      "enum": [
        "circle",
        "square",
        "rectangle_2",
        "rectangle_3",
        "rectangle_4"
      ],
      "default": "circle"
    },
    "fpp_k_factor": {
      "type": "number",
      "min": 0.1,
      "max": 50,
      "default": 4.532
    },
    "fpp_model": {
      "type": "string",
      "enum": [
        "thin_film",
        "semi_infinite",
        "finite_thin",
        "finite_alpha"
      ],
      "default": "thin_film"
    },
    "fpp_position_correction": {
      "type": "string",
      "default": "warn"
    },
    "fpp_power_stop_w": {
      "type": "number",
      "min": 0.0001,
      "max": 22,
      "default": 0.1
    },
    "fpp_power_warn_w": {
      "type": "number",
      "min": 0.0001,
      "max": 10,
      "default": 0.01
    },
    "fpp_sample_diameter_mm": {
      "type": "number",
      "min": 0,
      "max": 1000,
      "default": 0
    },
    "fpp_sample_length_mm": {
      "type": "number",
      "min": 0,
      "max": 1000,
      "default": 0
    },
    "fpp_sample_shape": {
      "type": "string",
      "enum": [
        "unbounded",
        "circle",
        "rectangle"
      ],
      "default": "unbounded"
    },
    "fpp_sample_width_mm": {
      "type": "number",
      "min": 0,
      "max": 1000,
      "default": 0
    },
    "fpp_samples": {
      "type": "integer",
      "min": 0,
      "max": 1000000,
      "default": 20
    },
    "fpp_spacing_cm": {
      "type": "number",
      "min": 0.001,
      "max": 5,
      "default": 0.1016
    },
    "fpp_stop_on_overpower": {
      "type": "boolean",
      "default": true
    },
    "fpp_temperature_c": {
      "type": "number",
      "nullable": true,
      "min": -50,
      "max": 200,
      "default": null
    },
    "fpp_thickness_um": {
      "type": "number",
      "min": 0,
      "max": 5000,
      "default": 0
    },
    "fpp_voltage_compliance": {
      "type": "number",
      "min": 0.1,
      "max": 200,
      "default": 5
    },
    "fpp_voltage_range_auto": {
      "type": "boolean",
      "default": true
    }
  },
  "InstrumentSettings": {
    "allow_agents": {
      "type": "boolean",
      "default": false
    },
    "auto_zero": {
      "type": "string",
      "enum": [
        "on",
        "once",
        "off"
      ],
      "default": "once"
    },
    "filter_count": {
      "type": "integer",
      "min": 1,
      "max": 100,
      "default": 5
    },
    "filter_enabled": {
      "type": "boolean",
      "default": true
    },
    "filter_type": {
      "type": "string",
      "enum": [
        "repeat",
        "moving"
      ],
      "default": "repeat"
    },
    "gpib_address": {
      "type": "string",
      "default": "GPIB0::24::INSTR"
    },
    "gpib_interface": {
      "type": "string",
      "default": ""
    },
    "nplc": {
      "type": "number",
      "min": 0.01,
      "max": 10,
      "default": 1
    },
    "sampling_rate": {
      "type": "number",
      "min": 0.1,
      "max": 100,
      "default": 10
    },
    "settling_time": {
      "type": "number",
      "min": 0,
      "max": 10,
      "default": 0.2
    },
    "stop_on_compliance": {
      "type": "boolean",
      "default": false
    },
    "visa_library": {
      "type": "string",
      "default": ""
    }
  },
  "OutputSettings": {
    "compression": {
      "type": "string",
      "enum": [
        "never",
        "always",
        "auto"
      ],
      "default": "never"
    },
    "compression_threshold_mb": {
      "type": "number",
      "min": 0,
      "default": 5
    },
    "format": {
      "type": "string",
      "enum": [
        "csv",
        "hdf5",
        "csv+legacy_json"
      ],
      "default": "csv"
    }
  },
  "ResistanceSettings": {
    "res_auto_range": {
      "type": "boolean",
      "default": true
    },
    "res_cable_null": {
      "type": "number",
      "min": 0,
      "default": 0
    },
    "res_measurement_type": {
      "type": "string",
      "enum": [
        "2-wire",
        "4-wire"
      ],
      "default": "2-wire"
    },
    "res_offset_comp": {
      "type": "boolean",
      "default": true
    },
    "res_test_current": {
      "type": "number",
      "min": 1e-7,
      "max": 3,
      "default": 0.001
    },
    "res_voltage_compliance": {
      "type": "number",
      "min": 0.1,
      "max": 200,
      "default": 5
    }
  },
  "RunRequest": {
    "client": {
      "nullable": true,
      "default": null
    },
    "mode": {
      "type": "string",
      "enum": [
        "resistance",
        "source_v",
        "source_i",
        "four_point",
        "sweep",
        "vdp"
      ],
      "required": true
    },
    "overrides": {
      "type": "object"
    },
    "prompt_timeout_s": {
      "type": "number",
      "exclusiveMin": 0,
      "max": 86400,
      "default": 900
    },
    "sample_name": {
      "type": "string",
      "required": true
    },
    "spot": {
      "nullable": true,
      "default": null
    },
    "username": {
      "type": "string",
      "required": true
    }
  },
  "SafetySettings": {
    "safety_voltage_warn_silenced": {
      "type": "boolean",
      "default": false
    },
    "safety_voltage_warn_silenced_until": {
      "type": "number",
      "nullable": true,
      "min": 0,
      "default": null
    },
    "safety_voltage_warn_v": {
      "type": "number",
      "min": 0,
      "max": 1100,
      "default": 30
    }
  },
  "SampleGeometry": {
    "diameter_mm": {
      "type": "number",
      "nullable": true,
      "exclusiveMin": 0,
      "default": null
    },
    "length_mm": {
      "type": "number",
      "nullable": true,
      "exclusiveMin": 0,
      "default": null
    },
    "shape": {
      "type": "string",
      "enum": [
        "unbounded",
        "circle",
        "rectangle"
      ],
      "default": "unbounded"
    },
    "width_mm": {
      "type": "number",
      "nullable": true,
      "exclusiveMin": 0,
      "default": null
    }
  },
  "SettingsWarning": {
    "keys": {
      "type": "array",
      "required": true
    },
    "message": {
      "type": "string",
      "required": true
    }
  },
  "SpotRequest": {
    "angle_deg": {
      "type": "number",
      "nullable": true,
      "min": -360,
      "max": 360,
      "default": null
    },
    "index": {
      "type": "integer",
      "min": 0,
      "max": 9999,
      "required": true
    },
    "label": {
      "type": "string",
      "required": true
    },
    "map_id": {
      "type": "string",
      "required": true
    },
    "x_mm": {
      "type": "number",
      "nullable": true,
      "default": null
    },
    "y_mm": {
      "type": "number",
      "nullable": true,
      "default": null
    }
  },
  "SweepSettings": {
    "sweep_compliance": {
      "type": "number",
      "min": 1e-7,
      "max": 210,
      "default": 0.1
    },
    "sweep_delay": {
      "type": "number",
      "min": 0,
      "max": 10,
      "default": 0.01
    },
    "sweep_direction": {
      "type": "string",
      "enum": [
        "up",
        "down",
        "up_down"
      ],
      "default": "up"
    },
    "sweep_source": {
      "type": "string",
      "enum": [
        "voltage",
        "current"
      ],
      "default": "voltage"
    },
    "sweep_start": {
      "type": "number",
      "min": -200,
      "max": 200,
      "default": 0
    },
    "sweep_step": {
      "type": "number",
      "exclusiveMin": 0,
      "max": 200,
      "default": 0.05
    },
    "sweep_stop": {
      "type": "number",
      "min": -200,
      "max": 200,
      "default": 1
    }
  },
  "VdpSettings": {
    "vdp_current": {
      "type": "number",
      "exclusiveMin": 0,
      "max": 1,
      "default": 0.001
    },
    "vdp_readings_per_polarity": {
      "type": "integer",
      "min": 1,
      "max": 100,
      "default": 5
    },
    "vdp_settling_s": {
      "type": "number",
      "min": 0,
      "max": 10,
      "default": 0.2
    },
    "vdp_thickness_cm": {
      "type": "number",
      "min": 0,
      "max": 10,
      "default": 0
    },
    "vdp_voltage_compliance": {
      "type": "number",
      "exclusiveMin": 0,
      "max": 200,
      "default": 5
    },
    "vdp_voltage_range_auto": {
      "type": "boolean",
      "default": true
    }
  },
  "VoltageSourceSettings": {
    "vsource_current_compliance": {
      "type": "number",
      "min": 1e-7,
      "max": 3,
      "default": 0.1
    },
    "vsource_current_range_auto": {
      "type": "boolean",
      "default": true
    },
    "vsource_duration_hours": {
      "type": "number",
      "min": 0,
      "max": 168,
      "default": 0
    },
    "vsource_voltage": {
      "type": "number",
      "min": -200,
      "max": 200,
      "default": 1
    }
  }
};

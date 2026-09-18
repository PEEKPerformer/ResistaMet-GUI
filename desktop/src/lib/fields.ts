// How each settings field is presented: label, unit, grouping. The *keys* are
// typed against the generated models, so a backend rename is a compile error
// here; bounds and defaults come from FIELD_META, never from this file.

import type {
  CurrentSourceSettings,
  FourPointSettings,
  InstrumentSettings,
  Mode,
  ResistanceSettings,
  SweepSettings,
  VdpSettings,
  VoltageSourceSettings,
} from "../generated/settings";

export interface FieldSpec<K extends string = string> {
  key: K;
  label: string;
  /** Base SI unit the backend uses (values are always in this unit). */
  unit?: string;
  /** Extra explanation shown under the field. Sparingly. */
  hint?: string;
  /** Enum labels, when the raw values are not self-explanatory. */
  options?: Record<string, string>;
  /** The instrument ignores this field while another field has a given
   *  value; the row is shown disabled with `hint` so the number on screen is
   *  not mistaken for the number in use. */
  overriddenBy?: { key: string; when: unknown; hint: string };
}

export interface FieldGroup<K extends string = string> {
  title: string;
  fields: FieldSpec<K>[];
}

type Keys<T> = Extract<keyof T, string>;

export const RESISTANCE_FIELDS: FieldGroup<Keys<ResistanceSettings>>[] = [
  {
    title: "Source",
    fields: [
      // With Auto range on, the 2400's auto-ohms picks the test current per
      // range and sets its own voltage limit (2.1 V seen for a 0.5 V request);
      // the CSV's Current column and effective.voltage_compliance_V record
      // what it chose. Bench, 2026-09-18.
      {
        key: "res_test_current",
        label: "Test current",
        unit: "A",
        overriddenBy: { key: "res_auto_range", when: true, hint: "Chosen by the instrument per range while Auto range is on." },
      },
      {
        key: "res_voltage_compliance",
        label: "Voltage compliance",
        unit: "V",
        overriddenBy: { key: "res_auto_range", when: true, hint: "Set by the instrument while Auto range is on; the file records the value in force." },
      },
    ],
  },
  {
    title: "Measure",
    fields: [
      { key: "res_measurement_type", label: "Wiring" },
      { key: "res_auto_range", label: "Auto range" },
      { key: "res_offset_comp", label: "Offset compensation", hint: "Halves throughput; tightens σR." },
    ],
  },
];

export const VOLTAGE_SOURCE_FIELDS: FieldGroup<Keys<VoltageSourceSettings>>[] = [
  {
    title: "Source",
    fields: [
      { key: "vsource_voltage", label: "Voltage", unit: "V" },
      { key: "vsource_current_compliance", label: "Current compliance", unit: "A" },
    ],
  },
  {
    title: "Measure",
    fields: [
      { key: "vsource_current_range_auto", label: "Auto range" },
      { key: "vsource_duration_hours", label: "Duration", unit: "h", hint: "0 runs until stopped." },
    ],
  },
];

export const CURRENT_SOURCE_FIELDS: FieldGroup<Keys<CurrentSourceSettings>>[] = [
  {
    title: "Source",
    fields: [
      { key: "isource_current", label: "Current", unit: "A" },
      { key: "isource_voltage_compliance", label: "Voltage compliance", unit: "V" },
    ],
  },
  {
    title: "Measure",
    fields: [
      { key: "isource_voltage_range_auto", label: "Auto range" },
      { key: "isource_duration_hours", label: "Duration", unit: "h", hint: "0 runs until stopped." },
    ],
  },
];

export const FOUR_POINT_FIELDS: FieldGroup<Keys<FourPointSettings>>[] = [
  {
    title: "Source",
    fields: [
      { key: "fpp_current", label: "Source current", unit: "A" },
      { key: "fpp_voltage_compliance", label: "Voltage compliance", unit: "V" },
      { key: "fpp_voltage_range_auto", label: "Auto range" },
      { key: "fpp_samples", label: "Samples per spot", hint: "0 runs until stopped." },
    ],
  },
  {
    title: "Geometry",
    fields: [
      { key: "fpp_spacing_cm", label: "Probe spacing", unit: "cm" },
      { key: "fpp_thickness_um", label: "Thickness", unit: "µm", hint: "0 = unknown; sheet resistance only." },
      { key: "fpp_diameter_cm", label: "Specimen diameter", unit: "cm", hint: "0 = infinite." },
      {
        key: "fpp_geometry",
        label: "Specimen shape",
        options: {
          circle: "Circle",
          square: "Square",
          rectangle_2: "Rectangle 2:1",
          rectangle_3: "Rectangle 3:1",
          rectangle_4: "Rectangle 4:1",
        },
      },
      {
        key: "fpp_model",
        label: "Correction model",
        options: {
          thin_film: "Thin film",
          semi_infinite: "Semi-infinite",
          finite_thin: "Finite, thin",
          finite_alpha: "Finite, α",
        },
      },
      { key: "fpp_alpha", label: "α" },
      { key: "fpp_k_factor", label: "K factor" },
    ],
  },
  {
    title: "Temperature (ASTM F84 F_T)",
    fields: [
      { key: "fpp_temperature_c", label: "Temperature", unit: "°C", hint: "Blank = not measured." },
      { key: "fpp_dopant_type", label: "Dopant", options: { none: "None", n: "n-type", p: "p-type" } },
    ],
  },
  {
    title: "Delta mode",
    fields: [
      { key: "fpp_delta_mode", label: "Current reversal" },
      { key: "fpp_delta_settling", label: "Polarity settle", unit: "s" },
    ],
  },
  {
    title: "Probe safety",
    fields: [
      { key: "fpp_power_warn_w", label: "Warn above", unit: "W" },
      { key: "fpp_power_stop_w", label: "Hard stop above", unit: "W" },
      { key: "fpp_stop_on_overpower", label: "Stop on overpower" },
    ],
  },
];

export const SWEEP_FIELDS: FieldGroup<Keys<SweepSettings>>[] = [
  {
    title: "Sweep",
    fields: [
      { key: "sweep_source", label: "Source", options: { voltage: "Voltage", current: "Current" } },
      { key: "sweep_start", label: "Start" },
      { key: "sweep_stop", label: "Stop" },
      { key: "sweep_step", label: "Step" },
      { key: "sweep_direction", label: "Direction", options: { up: "Up", down: "Down", up_down: "Up then down" } },
    ],
  },
  {
    title: "Limits",
    fields: [
      { key: "sweep_compliance", label: "Compliance" },
      { key: "sweep_delay", label: "Source delay", unit: "s" },
    ],
  },
];

export const VDP_FIELDS: FieldGroup<Keys<VdpSettings>>[] = [
  {
    title: "Source",
    fields: [
      { key: "vdp_current", label: "Source current", unit: "A" },
      { key: "vdp_voltage_compliance", label: "Voltage compliance", unit: "V" },
      { key: "vdp_voltage_range_auto", label: "Auto range" },
    ],
  },
  {
    title: "Sample",
    fields: [{ key: "vdp_thickness_cm", label: "Thickness", unit: "cm" }],
  },
  {
    title: "Readings",
    fields: [
      { key: "vdp_settling_s", label: "Settle after flip", unit: "s" },
      { key: "vdp_readings_per_polarity", label: "Readings per polarity" },
    ],
  },
];

/** Instrument-wide knobs the tab exposes (the rest live in the profile). */
export const TIMING_FIELDS: FieldSpec<Keys<InstrumentSettings>>[] = [
  { key: "nplc", label: "NPLC", hint: "Integration time, power-line cycles." },
  { key: "sampling_rate", label: "Sampling rate", unit: "Hz" },
  { key: "auto_zero", label: "Auto zero", options: { on: "On", once: "Once", off: "Off" } },
];

export const MODE_FIELDS: Record<Mode, FieldGroup[]> = {
  resistance: RESISTANCE_FIELDS,
  source_v: VOLTAGE_SOURCE_FIELDS,
  source_i: CURRENT_SOURCE_FIELDS,
  four_point: FOUR_POINT_FIELDS,
  sweep: SWEEP_FIELDS,
  vdp: VDP_FIELDS,
};

export const MODE_LABEL: Record<Mode, string> = {
  resistance: "Resistance",
  source_v: "Voltage source",
  source_i: "Current source",
  four_point: "Four-point probe",
  sweep: "I-V sweep",
  vdp: "van der Pauw",
};

/** Which timing knobs each mode's tab shows (mirrors the PySide6 tabs). */
export const MODE_TIMING: Record<Mode, Keys<InstrumentSettings>[]> = {
  resistance: ["sampling_rate", "auto_zero"],
  source_v: ["sampling_rate", "auto_zero"],
  source_i: ["sampling_rate", "auto_zero"],
  four_point: ["nplc", "sampling_rate"],
  sweep: ["nplc"],
  vdp: ["nplc"],
};

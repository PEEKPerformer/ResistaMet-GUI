/* eslint-disable */
// GENERATED from contracts/*.schema.json by desktop/scripts/generate-types.mjs.
// Do not edit. Regenerate with `npm run gen:types`.

/**
 * One thing that happened during a run.
 */
export interface EventEnvelope {
  cursor?: number | null;
  payload?: {
    [k: string]: unknown | undefined;
  };
  run_id?: string | null;
  seq: number;
  t: number;
  type: string;
  v?: number;
}

/**
 * The acquisition loop ended; cleanup and finalize still follow.
 */
export interface AcquisitionFinishedPayload {
  mode: string;
}

/**
 * The auxiliary sensor is open and has declared its channels.
 */
export interface AuxConnectedPayload {
  address: string;
  channels?: {
    [k: string]: unknown | undefined;
  }[];
  driver: string;
}

/**
 * The source is in compliance on this sample.
 */
export interface CompliancePayload {
  kind: "Voltage" | "Current";
  stop_on_compliance?: boolean;
}

/**
 * Something failed. ``source`` says which subsystem.
 *
 * ``source`` replaces the substring matching the UI does today to decide
 * whether an error came from the instrument, the aux sensor or the file.
 * ``fatal`` marks the errors that end the run.
 */
export interface ErrorPayload {
  code: string;
  fatal?: boolean;
  message: string;
  source: "smu" | "aux" | "file" | "run";
}

/**
 * The run's file is closed, with its end metadata written.
 */
export interface FileFinalizedPayload {
  end_metadata?: {
    [k: string]: unknown | undefined;
  };
  path: string;
}

/**
 * The run's output file exists and its column schema is fixed.
 */
export interface FileOpenedPayload {
  columns?: string[];
  path: string;
  units?: string[];
}

/**
 * A four-point spot's position is a problem, said before the first sample.
 *
 * ``refused`` with ``off_sample``: a probe tip is on or beyond the edge and
 * the run ends without touching the instrument. ``near_edge``: the position
 * costs more than ``edge_warn_pct`` here; the run goes on and the values are
 * recorded as measured, without a position correction.
 *
 * Two errors, both fractions and not percentages. ``relative_error_rows`` is
 * ``factor_rows / factor_here - 1``, against the lateral factor the run's
 * rows really apply (the table look-up, or K*alpha); it is the error in the
 * file's Rs. ``relative_error`` is ``factor_centre / factor_here - 1``,
 * against the closed-form centre of the sample outline. ``compared_with``
 * says which one was held against the threshold: ``rows`` whenever the rows
 * have a factor, else ``centre``. The factors are absent off the sample,
 * where they diverge.
 */
export interface GeometryWarningPayload {
  compared_with?: "rows" | "centre";
  edge_clearance_s: number;
  edge_warn_pct: number;
  factor_centre?: number | null;
  factor_here?: number | null;
  factor_rows?: number | null;
  message: string;
  reason: "off_sample" | "near_edge";
  refused: boolean;
  relative_error?: number | null;
  relative_error_rows?: number | null;
  spot: SpotRequest;
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
 * The SMU answered *IDN? and its limits are known.
 */
export interface InstrumentConnectedPayload {
  address: string;
  idn: string;
  max_power_w?: number | null;
  max_source_i?: number | null;
  max_source_v?: number | null;
  model: string;
}

/**
 * Mains frequency, queried or assumed. Continuous modes only.
 */
export interface LineFrequencyPayload {
  assumed?: boolean;
  hz: number;
}

/**
 * Human-readable progress, one per current ``status_update`` site.
 *
 * ``code`` is a short closed set so a client can act on an event without
 * parsing prose; ``message`` stays the exact text the GUI shows today.
 */
export interface LogPayload {
  code: string;
  level?: "info" | "warning" | "error";
  message: string;
}

/**
 * Measured V*I crossed the 4PP probe-safety hard stop.
 */
export interface OverpowerPayload {
  measured_w: number;
  stop_w: number;
}

/**
 * Paused, resumed or stopping, as observed by the acquisition thread.
 */
export interface RunStatePayload {
  reason?: string | null;
}

/**
 * The run is blocked until someone answers.
 */
export interface PromptPayload {
  detail?: {
    [k: string]: unknown | undefined;
  };
  kind: "vdp_geometry" | "safety_voltage_ack" | "cable_null_shorted";
  options?: string[];
  prompt_id: string;
  requires_human?: boolean;
}

/**
 * How a prompt ended: answered, or released by a stop.
 */
export interface PromptResolvedPayload {
  answered_by?: string | null;
  choice?: string | null;
  prompt_id: string;
}

/**
 * Paused, resumed or stopping, as observed by the acquisition thread.
 */
export interface RunEndedPayload {
  duration_s?: number;
  ok?: boolean;
  output_verified?: boolean;
  path?: string | null;
  reason: string;
  samples?: number;
}

/**
 * A run is beginning; the settings are exactly what it will use.
 */
export interface RunStartedPayload {
  mode: string;
  sample_name: string;
  settings?: {
    [k: string]: unknown | undefined;
  };
  started_at: number;
  username: string;
}

/**
 * One acquired point.
 *
 * ``values`` is the mode's data dict exactly as the parse produced it, with
 * no coercion — the aux-fault column is a string, and a client that wants
 * numbers must say which key it means.
 */
export interface SamplePayload {
  compliance?: "OK" | "V_COMP" | "I_COMP";
  delta?: DeltaPayload | null;
  derived?: DerivedPayload | null;
  elapsed_s: number;
  event_marker?: string;
  t_unix: number;
  values?: {
    [k: string]: unknown | undefined;
  };
}
/**
 * Per-polarity values from a current-reversal (delta) reading.
 */
export interface DeltaPayload {
  r_f: number;
  r_r: number;
  v_minus: number;
  v_plus: number;
}
/**
 * The 4PP quantities computed for this sample, as written to the row.
 *
 * ``method`` says which correction path produced them: the ASTM F84
 * decomposition or the legacy K*alpha form.
 */
export interface DerivedPayload {
  i_unc: number;
  method: "f84" | "legacy";
  ratio: number;
  rho: number;
  rs: number;
  sigma: number;
  v_unc: number;
}

/**
 * A four-point run's file is closed; these are the numbers in its footer.
 *
 * Emitted for every four-point run whose file was finalized, so a client
 * shows the backend's statistics instead of computing its own. ``spot`` is
 * null for a run that was not given one. How the run ended is in the
 * ``run_ended`` event that follows.
 */
export interface SpotCompletePayload {
  path?: string | null;
  spot?: SpotRequest | null;
  stats: SpotStats;
}
/**
 * One placement of the probe, as the client describes it.
 *
 * The position is optional -- a spot can be a label and nothing more -- but
 * ``x_mm`` and ``y_mm`` only mean something together. ``angle_deg`` is the
 * direction of the probe array, anticlockwise from +x; absent, the run uses
 * the ``fpp_array_angle_deg`` setting.
 */
export interface SpotStats {
  end_reason?: string | null;
  n: number;
  n_excluded?: number;
  rho: QuantityStats;
  rs: QuantityStats;
  sigma: QuantityStats;
}
/**
 * One derived quantity over a spot's samples (``session.spot_stats``).
 *
 * ``n`` counts the finite values. Every other field is null on the wire when
 * it does not exist: all of them with no finite value, ``sd`` and
 * ``rsd_pct`` with fewer than two.
 */
export interface QuantityStats {
  mean?: number | null;
  n: number;
  rsd_pct?: number | null;
  sd?: number | null;
  u_inst?: number | null;
  u_stat?: number | null;
  u_total?: number | null;
}

/**
 * Paused, resumed or stopping, as observed by the acquisition thread.
 */
export interface SweepSegmentPayload {
  compliance?: string[];
  currents?: number[];
  direction?: "forward" | "reverse";
  voltages?: number[];
}

/**
 * One F76 geometry measured: the +I and -I voltages at this wiring.
 */
export interface VdpGeometryCompletePayload {
  current_a: number;
  group: string;
  index: number;
  label_neg: string;
  label_pos: string;
  name: string;
  v_neg: number;
  v_pos: number;
}

/**
 * The finished van der Pauw result, ASTM F76.
 *
 * Field for field what the GUI result panel has always received, including
 * the f(Q) homogeneity check and the combined uncertainties, so the panel
 * reads the same numbers the CSV metadata carries.
 */
export interface VdpResultPayload {
  asymmetry_pct: number;
  current_a: number;
  f_a: number;
  f_b: number;
  homogeneous: boolean;
  q_a: number;
  q_b: number;
  rho_a: number;
  rho_avg: number;
  rho_avg_uncertainty?: number | null;
  rho_b: number;
  sheet_resistance: number;
  sheet_resistance_uncertainty?: number | null;
  thickness_cm: number;
  voltages?: {
    [k: string]: number | undefined;
  };
}

export const EVENT_SCHEMA_VERSION = 1;

/** Event type -> payload. Events not listed here carry an untyped payload. */
export interface EventPayloadMap {
  acquisition_finished: AcquisitionFinishedPayload;
  aux_connected: AuxConnectedPayload;
  compliance: CompliancePayload;
  error: ErrorPayload;
  file_finalized: FileFinalizedPayload;
  file_opened: FileOpenedPayload;
  geometry_warning: GeometryWarningPayload;
  instrument_connected: InstrumentConnectedPayload;
  line_frequency: LineFrequencyPayload;
  log: LogPayload;
  overpower_trip: OverpowerPayload;
  paused: RunStatePayload;
  prompt: PromptPayload;
  prompt_resolved: PromptResolvedPayload;
  resumed: RunStatePayload;
  run_ended: RunEndedPayload;
  run_started: RunStartedPayload;
  sample: SamplePayload;
  spot_complete: SpotCompletePayload;
  stopping: RunStatePayload;
  sweep_segment: SweepSegmentPayload;
  vdp_geometry_complete: VdpGeometryCompletePayload;
  vdp_result: VdpResultPayload;
}

export type EventType = keyof EventPayloadMap;

/** A typed event: the envelope with its payload narrowed by `type`. */
export type Event<T extends EventType = EventType> = Omit<EventEnvelope, "type" | "payload"> & {
  type: T;
  payload: EventPayloadMap[T];
};

export type AnyEvent = { [T in EventType]: Event<T> }[EventType];

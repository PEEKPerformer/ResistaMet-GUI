/* eslint-disable */
// GENERATED from contracts/*.schema.json by desktop/scripts/generate-types.mjs.
// Do not edit. Regenerate with `npm run gen:types`.

/**
 * The ``fields`` of an answer to ``safety_voltage_ack``.
 *
 * ``silence_for_profile`` silences the warning on the run's profile for
 * good; ``silence_for_days`` for that many days from the answer. Either
 * is saved only with ``acknowledge``: a person who cancelled has not
 * agreed to stop being asked. One or the other, not both. Unknown keys
 * are refused, so a misspelt one is an error rather than a silence that
 * quietly never happened.
 *
 * The silence applies to the runs a person starts; a run an agent
 * started asks regardless (``docs/design/mcp_layer.md`` M5).
 */
export interface SafetyAckFields {
  silence_for_days?: number | null;
  silence_for_profile?: boolean;
}

/**
 * One session: its state and the run it is on, or was last on.
 */
export interface SessionStatus {
  instrument: InstrumentInfo | null;
  last_seq: number;
  mode: ("resistance" | "source_v" | "source_i" | "four_point" | "sweep" | "vdp") | null;
  path: string | null;
  pending_prompt: PendingPrompt | null;
  run_id: string | null;
  started_by: string | null;
  state: "idle" | "identifying" | "running" | "paused" | "awaiting_prompt" | "stopping";
}
/**
 * The SourceMeter as last seen: what ``identify`` returns, and the data
 * of a run's ``instrument_connected`` event. ``model`` is None when the
 * *IDN? reply names a model the limits table does not know.
 */
export interface InstrumentInfo {
  address: string;
  idn: string;
  max_power_w: number | null;
  max_source_i: number | null;
  max_source_v: number | null;
  model: string | null;
}
/**
 * The question a run is parked on. Same fields as the ``prompt`` event.
 */
export interface PendingPrompt {
  detail: {
    [k: string]: unknown | undefined;
  };
  kind: "vdp_geometry" | "safety_voltage_ack" | "cable_null_shorted";
  options: string[];
  prompt_id: string;
  requires_human: boolean;
}

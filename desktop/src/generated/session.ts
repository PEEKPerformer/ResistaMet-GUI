/* eslint-disable */
// GENERATED from contracts/*.schema.json by desktop/scripts/generate-types.mjs.
// Do not edit. Regenerate with `npm run gen:types`.

/**
 * One session: its state and the run it is on, or was last on.
 */
export interface SessionStatus {
  last_seq: number;
  mode: ("resistance" | "source_v" | "source_i" | "four_point" | "sweep" | "vdp") | null;
  path: string | null;
  pending_prompt: PendingPrompt | null;
  run_id: string | null;
  state: "idle" | "identifying" | "running" | "paused" | "awaiting_prompt" | "stopping";
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

// What the four-point current finder's question shows. The finder asks only
// when it could not simply choose: the target is out of reach (marginal), the
// sense voltage is under the noise (too conductive), or the smallest current
// needs more voltage than the compliance allows (too resistive). The numbers
// and the reason are the backend's; this lays them out.

import type { FppCurrentSelectedPayload } from "../generated/events";
import { formatEngineering } from "./format.ts";

/** The prompt's detail: the selection event's fields plus the reason. Read
 *  as untyped JSON, so every field may be missing. */
export type FppCurrentDetail = Partial<FppCurrentSelectedPayload> & { message?: string };

export interface FppCurrentDecision {
  title: string;
  /** The finder's own sentence; empty when it sent none. */
  reason: string;
  rows: { label: string; value: string }[];
}

const TITLES: Record<string, string> = {
  marginal: "Target significant figures not reached",
  too_conductive: "Sample too conductive",
  too_resistive: "Sample too resistive",
};

/** Non-finite numbers arrive as null, whatever the generated type says. */
function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function fppCurrentDecision(detail: FppCurrentDetail): FppCurrentDecision {
  const rows: FppCurrentDecision["rows"] = [];
  const quantity = (label: string, raw: unknown, unit: string, prefix = "") => {
    const value = finite(raw);
    if (value !== null) rows.push({ label, value: prefix + formatEngineering(value, unit) });
  };

  // Still in compliance at the smallest current: V/I is only bounded below.
  if (detail.bound === true) quantity("R, lower bound", detail.resistance_ohm, "Ω", "> ");
  else quantity("Measured R", detail.resistance_ohm, "Ω");
  quantity("Probe current", detail.requested_current_a, "A");
  quantity("Suggested current", detail.current_a, "A");
  quantity("Expected voltage", detail.expected_voltage_v, "V");
  quantity("Noise floor", detail.noise_floor_v, "V");

  const target = finite(detail.target_sig_figs);
  if (target !== null) {
    const reached = finite(detail.sig_figs);
    rows.push({ label: "Significant figures", value: `${reached === null ? "—" : reached.toFixed(1)} of ${target}` });
  }

  return {
    title: TITLES[String(detail.verdict)] ?? "Auto-select current",
    reason: typeof detail.message === "string" ? detail.message : "",
    rows,
  };
}

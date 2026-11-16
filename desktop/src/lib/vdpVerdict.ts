// What a van der Pauw result's check line says. An invalid result (readings
// in voltage compliance, or signs that cannot be one sample's) is neither a
// pass nor a fail of the F76 homogeneity check: its numbers are kept for
// diagnosis and it is shown as invalid, with the run's reason.

import type { VdpResultPayload } from "../generated/events";

export interface VdpVerdict {
  tone: "ok" | "danger";
  label: string;
  detail: string;
}

export function vdpVerdict(result: Pick<VdpResultPayload, "valid" | "invalid_reason" | "homogeneous" | "asymmetry_pct">): VdpVerdict {
  // A result from before the field existed has no `valid`: it is valid.
  if (result.valid === false) {
    return { tone: "danger", label: "Invalid result", detail: result.invalid_reason || "no reason given" };
  }
  // Non-finite numbers arrive as null, whatever the generated type says.
  const asymmetry: number | null = result.asymmetry_pct;
  return {
    tone: result.homogeneous ? "ok" : "danger",
    label: result.homogeneous ? "f(Q) homogeneity passes" : "f(Q) homogeneity fails",
    detail: typeof asymmetry === "number" && Number.isFinite(asymmetry) ? `asymmetry ${asymmetry.toFixed(2)} %` : "asymmetry —",
  };
}

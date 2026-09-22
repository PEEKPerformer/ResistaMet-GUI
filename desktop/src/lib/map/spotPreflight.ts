// The backend's pre-flight of a spot (POST /spots/preflight) beside the
// client's own distance check: which of the two the map shows, and in what
// words. The backend's answer is the run's; the client's is instant.

import type { SpotPreflight, SpotPreflightRequest } from "../../generated/maps";
import type { Preflight, PreflightState } from "./geometry.ts";

/** How long the position has to rest before the backend is asked. */
export const PREFLIGHT_DEBOUNCE_MS = 250;

/** A pre-flight names no run, so the spot's identity is a stand-in: only the
 *  position is looked at. `mapId` is used when there is one so the body is a
 *  Start body in all but `mode` and `sample_name`. */
const STAND_IN = { map_id: "preflight", index: 0, label: "next" };

export function preflightBody(
  username: string,
  overrides: Record<string, unknown>,
  position: { x_mm: number; y_mm: number },
  mapId: string | null,
): SpotPreflightRequest {
  return {
    username,
    overrides,
    spot: { ...STAND_IN, map_id: mapId ?? STAND_IN.map_id, x_mm: position.x_mm, y_mm: position.y_mm },
  };
}

/** A fraction, signed, as a percentage. */
export function signedPercent(fraction: number): string {
  const pct = fraction * 100;
  return `${pct > 0 ? "+" : pct < 0 ? "−" : ""}${Math.abs(pct).toFixed(Math.abs(pct) < 10 ? 1 : 0)} %`;
}

/** The backend's sentence without the stand-in label it opens with. */
function withoutLabel(message: string): string {
  return message.replace(/^Spot '[^']*' is /, "");
}

/** The error the run's rows would carry when there is one, else the error
 *  against a centred probe. */
function errorText(answer: SpotPreflight): string | null {
  if (typeof answer.relative_error_rows === "number") return `Rs ${signedPercent(answer.relative_error_rows)} off with the factor this run applies`;
  if (typeof answer.relative_error === "number") return `Rs ${signedPercent(answer.relative_error)} off for assuming a centred probe`;
  return null;
}

export interface PositionFeedback {
  state: PreflightState;
  text: string;
  /** The backend's answer, not the client's estimate. */
  exact: boolean;
}

/** What to show for the pending position. `answer` is the backend's reply for
 *  exactly this position and these settings, or null while it is awaited or
 *  the backend cannot be reached; `local` is the distance-only check. */
export function positionFeedback(local: Preflight | null, localText: string, answer: SpotPreflight | null): PositionFeedback | null {
  if (answer !== null && answer.checked === true) {
    const state: PreflightState = answer.off_sample ? "off" : answer.near_edge ? "caution" : "ok";
    // A warning already states its error; a quiet spot gets the number alone.
    const text = typeof answer.message === "string" && answer.message !== "" ? withoutLabel(answer.message) : (errorText(answer) ?? "");
    return { state, text, exact: true };
  }
  if (local === null) return null;
  return { state: local.state, text: localText, exact: false };
}

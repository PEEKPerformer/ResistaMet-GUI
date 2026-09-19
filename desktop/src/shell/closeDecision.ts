// What a request to close the window should do, given what the backend says
// the session is doing. Kept apart from the component so it can be tested.

import type { SessionStatus } from "../generated/session";

export type CloseDecision =
  /** Nothing is running: close without a word. */
  | { kind: "close" }
  /** A run would be stopped: the operator decides. */
  | { kind: "ask"; mode: SessionStatus["mode"] }
  /** The backend did not say. A run cannot be ruled out, so ask. */
  | { kind: "ask-unknown" };

/** `status` is null when the backend could not be asked or did not answer. */
export function decideClose(status: SessionStatus | null): CloseDecision {
  if (status === null) return { kind: "ask-unknown" };
  // Everything but idle holds the instrument: identifying, running, paused,
  // parked on a prompt, or still on its way down.
  if (status.state === "idle") return { kind: "close" };
  return { kind: "ask", mode: status.mode };
}

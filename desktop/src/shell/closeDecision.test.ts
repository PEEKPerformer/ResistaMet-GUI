import { test } from "node:test";
import assert from "node:assert/strict";
import { decideClose } from "./closeDecision.ts";
import type { SessionStatus } from "../generated/session.ts";

function status(state: SessionStatus["state"], mode: SessionStatus["mode"] = null): SessionStatus {
  return { instrument: null, last_seq: 0, mode, path: null, pending_prompt: null, run_id: null, state };
}

test("an idle session closes without asking", () => {
  assert.deepEqual(decideClose(status("idle")), { kind: "close" });
  // The mode of the last run lingers on an idle session; it is still idle.
  assert.deepEqual(decideClose(status("idle", "sweep")), { kind: "close" });
});

test("every state that holds the instrument asks, naming the mode", () => {
  for (const state of ["identifying", "running", "paused", "awaiting_prompt", "stopping"] as const) {
    assert.deepEqual(decideClose(status(state, "four_point")), { kind: "ask", mode: "four_point" });
  }
});

test("a backend that did not answer is not taken for an idle one", () => {
  assert.deepEqual(decideClose(null), { kind: "ask-unknown" });
});

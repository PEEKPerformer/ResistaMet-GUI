import { test } from "node:test";
import assert from "node:assert/strict";
import { applyEvent, getSessionSnapshot, markPromptAnswered, promptRunId, setStatus } from "./session.ts";
import type { SessionStatus } from "../generated/session.ts";
import type { AnyEvent } from "../generated/events.ts";

function awaiting(promptId: string, runId = "run-1"): SessionStatus {
  return {
    instrument: null,
    last_seq: 5,
    mode: "vdp",
    path: null,
    pending_prompt: { detail: {}, kind: "vdp_geometry", options: ["proceed", "abort"], prompt_id: promptId, requires_human: true },
    run_id: runId,
    state: "awaiting_prompt",
  };
}

test("an answered prompt is gone at once, and a status still in flight cannot bring it back", () => {
  setStatus(awaiting("run-1:vdp_geometry-1"));
  assert.equal(getSessionSnapshot().status?.pending_prompt?.prompt_id, "run-1:vdp_geometry-1");
  markPromptAnswered("run-1:vdp_geometry-1");
  assert.equal(getSessionSnapshot().status?.pending_prompt, null);
  // The poll that was sent before the answer lands after it.
  setStatus(awaiting("run-1:vdp_geometry-1"));
  assert.equal(getSessionSnapshot().status?.pending_prompt, null);
  // The next geometry's prompt is a different one and shows.
  setStatus(awaiting("run-1:vdp_geometry-2"));
  assert.equal(getSessionSnapshot().status?.pending_prompt?.prompt_id, "run-1:vdp_geometry-2");
});

test("prompt_resolved from the stream retires the prompt whoever answered it", () => {
  setStatus(awaiting("run-1:safety_voltage_ack-3"));
  const resolved = {
    type: "prompt_resolved", seq: 9, t: 0, run_id: "run-1", v: 1,
    payload: { prompt_id: "run-1:safety_voltage_ack-3", choice: "proceed" },
  } as unknown as AnyEvent;
  applyEvent(resolved);
  assert.equal(getSessionSnapshot().status?.pending_prompt, null);
});

test("an answer names the run its prompt belongs to", () => {
  setStatus(awaiting("run-4:vdp_geometry-1", "run-4"));
  assert.equal(promptRunId("run-4:vdp_geometry-1"), "run-4");
  assert.equal(promptRunId("some-other-prompt"), null);
});

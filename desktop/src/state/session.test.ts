import { test } from "node:test";
import assert from "node:assert/strict";
import {
  applyEvent,
  dismissOutputNotice,
  getSessionSnapshot,
  markPromptAnswered,
  promptRunId,
  setStatus,
} from "./session.ts";
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

function logEvent(code: string, level = "warning", runId: string | null = "run-1", seq = 20): AnyEvent {
  return { type: "log", seq, t: 0, run_id: runId, v: 1, payload: { level, code, message: code } } as unknown as AnyEvent;
}

function runEnded(payload: Record<string, unknown>, seq = 30): AnyEvent {
  return { type: "run_ended", seq, t: 0, run_id: "run-1", v: 1, payload: { reason: "read_error", ...payload } } as unknown as AnyEvent;
}

const connected = {
  type: "instrument_connected", seq: 3, t: 0, run_id: "run-2", v: 1,
  payload: { address: "GPIB0::24::INSTR", idn: "KEITHLEY,2420,x,y", model: "2420" },
} as unknown as AnyEvent;

test("a run that could not confirm its output off raises the notice, by the warning or by run_ended", () => {
  dismissOutputNotice();
  assert.equal(getSessionSnapshot().outputUnverified, false);
  applyEvent(logEvent("output_unverified"));
  assert.equal(getSessionSnapshot().outputUnverified, true);

  dismissOutputNotice();
  applyEvent(runEnded({ ok: false, output_verified: false }));
  assert.equal(getSessionSnapshot().outputUnverified, true);
});

test("a run that ended normally does not raise it, and an unrelated warning does not clear it", () => {
  dismissOutputNotice();
  applyEvent(runEnded({ ok: true, reason: "target_samples", output_verified: true }));
  applyEvent(runEnded({ ok: true, reason: "user_stop" }));
  assert.equal(getSessionSnapshot().outputUnverified, false);

  applyEvent(logEvent("output_unverified"));
  applyEvent(logEvent("cleanup"));
  applyEvent(logEvent("progress", "info"));
  assert.equal(getSessionSnapshot().outputUnverified, true);
});

test("the notice stays until dismissed, and dismissing it is enough", () => {
  applyEvent(logEvent("output_unverified"));
  assert.equal(getSessionSnapshot().outputUnverified, true);
  dismissOutputNotice();
  assert.equal(getSessionSnapshot().outputUnverified, false);
});

test("the backend turning the output off clears it, from a run or from identify (no run id)", () => {
  applyEvent(logEvent("output_unverified"));
  applyEvent(logEvent("output_off_recovered", "info", "run-2"));
  assert.equal(getSessionSnapshot().outputUnverified, false);

  applyEvent(logEvent("output_unverified"));
  applyEvent(logEvent("output_off_recovered", "info", null, 1));
  assert.equal(getSessionSnapshot().outputUnverified, false);
  assert.equal(getSessionSnapshot().log.at(-1)?.code, "output_off_recovered");
});

test("a run reconnecting to the instrument clears it", () => {
  applyEvent(runEnded({ ok: false, output_verified: false }));
  assert.equal(getSessionSnapshot().outputUnverified, true);
  applyEvent(connected);
  assert.equal(getSessionSnapshot().outputUnverified, false);
});

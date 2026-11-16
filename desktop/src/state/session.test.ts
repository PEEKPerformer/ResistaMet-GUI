import { test } from "node:test";
import assert from "node:assert/strict";
import {
  applyEvent,
  dismissOutputNotice,
  getSessionSnapshot,
  backendRestarted,
  markPromptAnswered,
  promptRunId,
  runPanelSettings,
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
    started_by: null,
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

function runStarted(startedBy: string | null | undefined, runId = "run-5"): AnyEvent {
  const payload: Record<string, unknown> = { mode: "source_v", sample_name: "s", username: "alice", settings: {}, started_at: 0 };
  if (startedBy !== undefined) payload.started_by = startedBy;
  return { type: "run_started", seq: 1, t: 0, run_id: runId, v: 1, payload } as unknown as AnyEvent;
}

test("run_started says who started the run, and the next run says again", () => {
  applyEvent(runStarted("agent"));
  assert.equal(getSessionSnapshot().runStartedBy, "agent");
  applyEvent(runStarted("ui", "run-6"));
  assert.equal(getSessionSnapshot().runStartedBy, "ui");
  // A backend from before the field, or a run started without the API.
  applyEvent(runStarted(undefined, "run-7"));
  assert.equal(getSessionSnapshot().runStartedBy, null);
});

test("after a reload the status says who started the run", () => {
  applyEvent(runStarted(null));
  setStatus({ ...awaiting("run-8:safety_voltage_ack-1", "run-8"), started_by: "agent" });
  assert.equal(getSessionSnapshot().runStartedBy, "agent");
  setStatus({ ...awaiting("run-9:safety_voltage_ack-1", "run-9"), started_by: null });
  assert.equal(getSessionSnapshot().runStartedBy, null);
});

const AGENT_MEASUREMENT = { res_auto_range: false, res_test_current: 1e-3, nplc: 10 };

function runStartedWith(runId: string, startedBy: string | null): AnyEvent {
  const payload = {
    mode: "resistance", sample_name: "s", username: "alice", started_at: 0, started_by: startedBy,
    settings: { measurement: AGENT_MEASUREMENT, file: { data_directory: "x" }, started_by: startedBy },
  };
  return { type: "run_started", seq: 1, t: 0, run_id: runId, v: 1, payload } as unknown as AnyEvent;
}

function runningStatus(runId: string, startedBy: string | null, state: SessionStatus["state"] = "running"): SessionStatus {
  return {
    instrument: null, last_seq: 2, mode: "resistance", path: null, pending_prompt: null,
    run_id: runId, started_by: startedBy, state,
  };
}

test("an agent's run shows its own measurement settings in that mode's panel", () => {
  applyEvent(runStartedWith("run-10", "agent"));
  setStatus(runningStatus("run-10", "agent"));
  assert.deepEqual(runPanelSettings(getSessionSnapshot(), "resistance"), AGENT_MEASUREMENT);
  // Paused, waiting on a prompt or stopping, it is still the run on the bus.
  setStatus(runningStatus("run-10", "agent", "stopping"));
  assert.deepEqual(runPanelSettings(getSessionSnapshot(), "resistance"), AGENT_MEASUREMENT);
  // A run started without the API is not the window's either.
  applyEvent(runStartedWith("run-11", null));
  setStatus(runningStatus("run-11", null));
  assert.deepEqual(runPanelSettings(getSessionSnapshot(), "resistance"), AGENT_MEASUREMENT);
});

test("the window's own run, another mode's panel and an ended run show the form", () => {
  applyEvent(runStartedWith("run-12", "ui"));
  setStatus(runningStatus("run-12", "ui"));
  assert.equal(runPanelSettings(getSessionSnapshot(), "resistance"), null);

  applyEvent(runStartedWith("run-13", "agent"));
  setStatus(runningStatus("run-13", "agent"));
  assert.equal(runPanelSettings(getSessionSnapshot(), "source_v"), null);

  setStatus(runningStatus("run-13", "agent", "idle"));
  assert.equal(runPanelSettings(getSessionSnapshot(), "resistance"), null);
});

test("settings announced for another run are not shown as this one's", () => {
  applyEvent(runStartedWith("run-14", "agent"));
  // A reload late in run-15 lost its run_started; run-14's settings are not its.
  setStatus(runningStatus("run-15", "agent"));
  assert.equal(runPanelSettings(getSessionSnapshot(), "resistance"), null);
  // A new backend reuses run ids, so what the old one announced is dropped.
  applyEvent(runStartedWith("run-16", "agent"));
  backendRestarted();
  setStatus(runningStatus("run-16", "agent"));
  assert.equal(runPanelSettings(getSessionSnapshot(), "resistance"), null);
});

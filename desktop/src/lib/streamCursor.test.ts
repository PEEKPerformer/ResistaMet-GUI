import { test } from "node:test";
import assert from "node:assert/strict";
import { judge } from "./streamCursor.ts";

const at = (runId: string | null, lastSeq: number, hub: number | null = null) => ({ runId, lastSeq, hub });
const ev = (type: string, run_id: string, seq: number, cursor: number | null = null) => ({ type, run_id, seq, cursor });

test("events in order are delivered and an overlap is dropped", () => {
  assert.equal(judge(at("run-1", 10), ev("sample", "run-1", 11), at("run-1", 8)), "deliver");
  // Resumed from 8, already at 10: 9 and 10 can come twice.
  assert.equal(judge(at("run-1", 10), ev("sample", "run-1", 9), at("run-1", 8)), "replay");
});

test("a restarted backend's run-1 is not taken for a replay of the old one", () => {
  // The run starts after the socket is back.
  assert.equal(judge(at("run-1", 800), ev("run_started", "run-1", 1), at("run-1", 800)), "restarted");
  // The run started while the socket was down: the first thing seen is a
  // sample the resume point says this socket would never have been sent.
  assert.equal(judge(at("run-1", 800), ev("sample", "run-1", 37), at("run-1", 800)), "restarted");
});

test("a newer run first seen mid-stream has a missed head", () => {
  assert.equal(judge(at("run-1", 800), ev("sample", "run-2", 40), at("run-1", 800)), "missed-head");
  assert.equal(judge(at("run-1", 800), ev("run_started", "run-2", 1), at("run-1", 800)), "deliver");
  // The history after a reload late in a long run: nothing before it at all.
  assert.equal(judge(at(null, 0), ev("sample", "run-3", 40001), null), "missed-head");
});

test("a backend that stamps a cross-run cursor is followed by it", () => {
  const here = at("run-1", 800, 5000);
  assert.equal(judge(here, ev("sample", "run-1", 801, 5001), here), "deliver");
  // A second run in the same process counts on from the first.
  assert.equal(judge(here, ev("run_started", "run-2", 1, 5001), here), "deliver");
  assert.equal(judge(here, ev("sample", "run-2", 40, 5001), here), "missed-head");
  // Overlap between a resume and what was already delivered.
  assert.equal(judge(at("run-1", 810, 5010), ev("sample", "run-1", 805, 5005), here), "replay");
  // A new process counts from 1: its run-2 is not a replay, whatever the seq.
  assert.equal(judge(here, ev("run_started", "run-2", 1, 12), here), "restarted");
  assert.equal(judge(here, ev("sample", "run-1", 900, 300), here), "restarted");
});

test("events without a cursor fall back to the run's seq", () => {
  const here = at("run-1", 800, 5000);
  assert.equal(judge(here, ev("sample", "run-1", 801), here), "deliver");
  assert.equal(judge(here, ev("sample", "run-1", 37), here), "restarted");
});

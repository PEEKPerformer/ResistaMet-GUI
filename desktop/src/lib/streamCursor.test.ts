import { test } from "node:test";
import assert from "node:assert/strict";
import { judge } from "./streamCursor.ts";

const at = (runId: string | null, lastSeq: number) => ({ runId, lastSeq });
const ev = (type: string, run_id: string, seq: number) => ({ type, run_id, seq });

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

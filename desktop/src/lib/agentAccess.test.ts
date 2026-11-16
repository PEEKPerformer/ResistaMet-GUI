import { test } from "node:test";
import assert from "node:assert/strict";
import { agentAccessNote } from "./agentAccess.ts";

test("nothing is said when what is in force is the stored setting", () => {
  assert.equal(agentAccessNote(true, true), null);
  assert.equal(agentAccessNote(false, false), null);
});

test("access in force without the setting is put down to --allow-agents", () => {
  assert.equal(agentAccessNote(false, true), "On for this session (started with --allow-agents)");
  // A profile that has never stored the key is off.
  assert.equal(agentAccessNote(undefined, true), "On for this session (started with --allow-agents)");
});

test("the setting on without access is put down to another backend", () => {
  assert.equal(agentAccessNote(true, false), "Off: another ResistaMet backend is serving agents");
});

test("nothing is said when GET /agents did not answer", () => {
  assert.equal(agentAccessNote(true, null), null);
  assert.equal(agentAccessNote(false, null), null);
});

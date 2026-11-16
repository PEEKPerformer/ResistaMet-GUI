import { test } from "node:test";
import assert from "node:assert/strict";
import { isMarkKey, type KeyPress } from "./markKey.ts";

const press = (over: Partial<KeyPress> = {}): KeyPress => ({
  key: "m", metaKey: false, ctrlKey: false, altKey: false, repeat: false, targetTag: "BODY", ...over,
});

test("a plain M marks, in either case", () => {
  assert.equal(isMarkKey(press()), true);
  assert.equal(isMarkKey(press({ key: "M" })), true);
  assert.equal(isMarkKey(press({ key: "n" })), false);
});

test("a shortcut, a held key and typing in a field do not mark", () => {
  assert.equal(isMarkKey(press({ metaKey: true })), false);
  assert.equal(isMarkKey(press({ ctrlKey: true })), false);
  assert.equal(isMarkKey(press({ altKey: true })), false);
  assert.equal(isMarkKey(press({ repeat: true })), false);
  assert.equal(isMarkKey(press({ targetTag: "INPUT" })), false);
});

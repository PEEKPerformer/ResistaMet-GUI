import { test } from "node:test";
import assert from "node:assert/strict";
import { CONTAINER, trappedTabStop } from "./focusTrap.ts";

test("Tab wraps at both ends and is left alone in between", () => {
  assert.equal(trappedTabStop(3, 2, false), 0);
  assert.equal(trappedTabStop(3, 0, true), 2);
  assert.equal(trappedTabStop(3, 0, false), null);
  assert.equal(trappedTabStop(3, 1, false), null);
  assert.equal(trappedTabStop(3, 1, true), null);
  assert.equal(trappedTabStop(3, 2, true), null);
});

test("from the dialog itself or from behind it, Tab enters at an end", () => {
  assert.equal(trappedTabStop(3, -1, false), 0);
  assert.equal(trappedTabStop(3, -1, true), 2);
});

test("a single tab stop keeps focus", () => {
  assert.equal(trappedTabStop(1, 0, false), 0);
  assert.equal(trappedTabStop(1, 0, true), 0);
});

test("a dialog with no tab stop holds focus itself", () => {
  assert.equal(trappedTabStop(0, -1, false), CONTAINER);
  assert.equal(trappedTabStop(0, -1, true), CONTAINER);
});

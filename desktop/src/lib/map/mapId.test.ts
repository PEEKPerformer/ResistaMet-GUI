import { test } from "node:test";
import assert from "node:assert/strict";
import { MAP_ID_PATTERN, mapStamp, newMapId, nextFreeIndex, sameOwner, spotLabel } from "./mapId.ts";

const WHEN = new Date(2026, 8, 19, 15, 30, 0);

// The expected strings are what resistamet_gui/schema/map_session.py returns
// for the same inputs.
test("a map id reads stamp, sample, token", () => {
  assert.equal(mapStamp(WHEN), "20260919-153000");
  assert.equal(newMapId("Si wafer #3 (n-type)", WHEN, "ab12"), "20260919-153000_Si_wafer_3_n-type_ab12");
});

test("a sample name with nothing usable is 'sample'", () => {
  assert.equal(newMapId("", WHEN, "ab12"), "20260919-153000_sample_ab12");
  assert.equal(newMapId("  ###  ", WHEN, "ab12"), "20260919-153000_sample_ab12");
});

test("a long sample name is cut so the id fits the backend's pattern", () => {
  const id = newMapId("x".repeat(100), WHEN, "ab12");
  assert.equal(id, `20260919-153000_${"x".repeat(43)}_ab12`);
  assert.equal(id.length, 64);
  assert.match(id, MAP_ID_PATTERN);
});

test("every id matches the pattern, whatever the name", () => {
  for (const name of ["a/b\\c", "..", "naïve Ωfilm", "-_-", "tab\tname", "日本語"]) {
    assert.match(newMapId(name, WHEN, "0f3c"), MAP_ID_PATTERN);
  }
});

test("a label stays on one line and falls back to the automatic name", () => {
  assert.equal(spotLabel("  a\tb\n c  ", 3), "a b c");
  assert.equal(spotLabel(" \n", 3), "Spot 3");
  assert.equal(spotLabel("edge\u2028left", 1), "edge left");
  assert.equal(spotLabel("y".repeat(200), 1).length, 80);
});

test("the next index follows the highest, and a new map starts at 1", () => {
  assert.equal(nextFreeIndex([]), 1);
  assert.equal(nextFreeIndex([1, 2, 3]), 4);
  assert.equal(nextFreeIndex([1, 5]), 6);
  assert.equal(nextFreeIndex([9999]), 9999);
});

test("a map belongs to one operator and one sample name", () => {
  assert.ok(sameOwner({ user: "a", sample: "film " }, { user: "a", sample: "film" }));
  assert.ok(!sameOwner({ user: "a", sample: "film" }, { user: "b", sample: "film" }));
  assert.ok(!sameOwner({ user: "a", sample: "film" }, { user: "a", sample: "film 2" }));
});

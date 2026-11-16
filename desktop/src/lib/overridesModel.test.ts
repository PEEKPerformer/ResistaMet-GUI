import { test } from "node:test";
import assert from "node:assert/strict";
import { overridesOf, readStored, resetToProfile, seeded, withOverride } from "./overridesModel.ts";

test("one operator's values are not another's", () => {
  let all = withOverride({}, "anna", "resistance", "res_test_current", 0.1);
  all = withOverride(all, "ben", "resistance", "res_test_current", 1e-6);
  assert.equal(overridesOf(all, "anna", "resistance")?.res_test_current, 0.1);
  assert.equal(overridesOf(all, "ben", "resistance")?.res_test_current, 1e-6);
  assert.equal(overridesOf(all, "cara", "resistance"), undefined);
  assert.equal(overridesOf(all, "anna", "sweep"), undefined);
});

test("setting the value a key already has changes nothing", () => {
  const all = withOverride({}, "anna", "resistance", "nplc", 1);
  assert.equal(withOverride(all, "anna", "resistance", "nplc", 1), all);
  assert.notEqual(withOverride(all, "anna", "resistance", "nplc", 2), all);
});

test("seeding fills what is missing from that operator's profile only", () => {
  let all = withOverride({}, "anna", "resistance", "res_test_current", 0.1);
  all = seeded(all, "anna", "resistance", ["res_test_current", "nplc", "absent"], { res_test_current: 0.001, nplc: 5 });
  assert.deepEqual(overridesOf(all, "anna", "resistance"), { res_test_current: 0.1, nplc: 5 });
  all = seeded(all, "ben", "resistance", ["res_test_current", "nplc"], { res_test_current: 1e-6, nplc: 1 });
  assert.deepEqual(overridesOf(all, "ben", "resistance"), { res_test_current: 1e-6, nplc: 1 });
  assert.deepEqual(overridesOf(all, "anna", "resistance"), { res_test_current: 0.1, nplc: 5 });
  assert.equal(seeded(all, "ben", "resistance", ["nplc"], { nplc: 9 }), all);
});

test("reset puts the profile's values back in place of what was dialled in", () => {
  let all = withOverride({}, "anna", "resistance", "res_test_current", 0.1);
  all = withOverride(all, "anna", "resistance", "stale_key", true);
  all = withOverride(all, "anna", "sweep", "sweep_points", 11);
  all = withOverride(all, "ben", "resistance", "res_test_current", 1e-6);
  all = resetToProfile(all, "anna", "resistance", ["res_test_current", "nplc"], { res_test_current: 0.001, nplc: 5, other: 1 });
  assert.deepEqual(overridesOf(all, "anna", "resistance"), { res_test_current: 0.001, nplc: 5 });
  assert.deepEqual(overridesOf(all, "anna", "sweep"), { sweep_points: 11 });
  assert.deepEqual(overridesOf(all, "ben", "resistance"), { res_test_current: 1e-6 });
});

test("storage is read only in the operator-then-mode shape", () => {
  assert.deepEqual(readStored(null), {});
  assert.deepEqual(readStored("not json"), {});
  assert.deepEqual(readStored("[1]"), {});
  assert.deepEqual(readStored('{"anna":{"resistance":{"nplc":5}}}'), { anna: { resistance: { nplc: 5 } } });
  // The older store was keyed by mode alone: its numbers are not modes.
  assert.deepEqual(readStored('{"resistance":{"nplc":5,"res_test_current":0.1}}'), { resistance: {} });
});

import { test } from "node:test";
import assert from "node:assert/strict";
import { formatWithUncertainty } from "./format.ts";

test("a mean is carried to its uncertainty's second figure, under one prefix", () => {
  assert.equal(formatWithUncertainty(5.65123, 0.0036, "Ω/sq"), "5.6512 ± 0.0036 Ω/sq");
  assert.equal(formatWithUncertainty(0.00565, 0.000012, "Ω"), "5.650 ± 0.012 mΩ");
  assert.equal(formatWithUncertainty(1234.5, 25, "S/cm"), "1.234 ± 0.025 kS/cm");
  assert.equal(formatWithUncertainty(1234.4, 25, "Ω/sq"), "1234 ± 25 Ω/sq");
});

test("without an uncertainty it is the plain value, and no value is a dash", () => {
  assert.equal(formatWithUncertainty(5.65123, null, "Ω/sq"), "5.651 Ω/sq");
  assert.equal(formatWithUncertainty(5.65123, 0, "Ω/sq"), "5.651 Ω/sq");
  assert.equal(formatWithUncertainty(null, 0.1, "Ω/sq"), "—");
  assert.equal(formatWithUncertainty(NaN, 0.1, "Ω/sq"), "—");
});

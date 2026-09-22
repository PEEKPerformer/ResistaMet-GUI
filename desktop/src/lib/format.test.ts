import { test } from "node:test";
import assert from "node:assert/strict";
import { axisLabels, formatWithUncertainty } from "./format.ts";

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

test("axis labels on 2.5-step ticks say what the ticks are", () => {
  assert.deepEqual(axisLabels([0, 2.5, 5, 7.5, 10]), ["0.0", "2.5", "5.0", "7.5", "10.0"]);
  assert.deepEqual(axisLabels([0, 0.25, 0.5, 0.75, 1]), ["0.00", "0.25", "0.50", "0.75", "1.00"]);
  assert.deepEqual(
    axisLabels([10.07, 10.0725, 10.075, 10.0775, 10.08], "Ω"),
    ["10.0700 Ω", "10.0725 Ω", "10.0750 Ω", "10.0775 Ω", "10.0800 Ω"],
  );
});

test("axis labels carry no more decimals than the ticks need", () => {
  assert.deepEqual(axisLabels([0, 5, 10, 15]), ["0", "5", "10", "15"]);
  assert.deepEqual(axisLabels([0, 0.002, 0.004], "A"), ["0 mA", "2 mA", "4 mA"]);
  // Binary floating point is not a reason for another digit.
  assert.deepEqual(axisLabels([1.1, 1.2, 1.2 + 0.1], "V"), ["1.1 V", "1.2 V", "1.3 V"]);
});

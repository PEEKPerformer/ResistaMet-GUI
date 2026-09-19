import { test } from "node:test";
import assert from "node:assert/strict";
import { parseResistametCsv, sweepLegs, sweepPreview } from "./csv.ts";

const FOUR_POINT = [
  "# resistamet_format_version: 2.0",
  "# mode: four_point",
  "# started_at: 2026-09-19T19:15:34.293032",
  "# units: s,V,A,ohm,,",
  "elapsed_s,V,I,V_over_I,compliance,event",
  "0.0004,0.1,0.001,100,OK,",
  "0.0218,0.1,0.001,100,OK,",
  "# --- run completed ---",
  "# ended_at: 2026-09-19T19:15:34.914006",
  "# total_samples: 2",
  "# spot_stats.rs.mean: 453.2",
  "# spot_stats.end_reason: target_samples",
  "",
].join("\n");

test("the header, the columns and the rows are read", () => {
  const parsed = parseResistametCsv(FOUR_POINT);
  assert.equal(parsed.metadata.mode, "four_point");
  assert.equal(parsed.metadata.started_at, "2026-09-19T19:15:34.293032");
  assert.deepEqual(parsed.columns, ["elapsed_s", "V", "I", "V_over_I", "compliance", "event"]);
  assert.equal(parsed.rows, 2);
  assert.deepEqual(parsed.data.V_over_I, [100, 100]);
  assert.deepEqual(parsed.text.compliance, ["OK", "OK"]);
});

test("what the run wrote after its rows is kept, apart from the header", () => {
  const parsed = parseResistametCsv(FOUR_POINT);
  assert.deepEqual(parsed.footer, {
    ended_at: "2026-09-19T19:15:34.914006",
    total_samples: "2",
    "spot_stats.rs.mean": "453.2",
    "spot_stats.end_reason": "target_samples",
  });
  assert.equal("ended_at" in parsed.metadata, false);
});

test("a file cut off before the end block has an empty footer", () => {
  const cut = FOUR_POINT.split("\n").slice(0, 7).join("\r\n");
  const parsed = parseResistametCsv(cut);
  assert.deepEqual(parsed.footer, {});
  assert.equal(parsed.rows, 2);
});

const SWEEP = (source: string) =>
  [
    "# mode: sweep",
    `# params.source_function: ${source}`,
    "# params.direction: up_down",
    "# units: ,V,A,",
    "point,V_source,I_meas,compliance",
    "0,0,0,OK",
    "1,0.01,0.0001,OK",
    "2,0.02,0.0002,OK",
    "3,0.02,0.0002,OK",
    "4,0.01,0.0001,OK",
    "5,0,0,OK",
  ].join("\n");

test("a sweep file is drawn as current against voltage, a trace per leg", () => {
  for (const source of ["voltage", "current"]) {
    const preview = sweepPreview(parseResistametCsv(SWEEP(source)));
    assert.deepEqual(preview, { x: "V_source", y: "I_meas", legs: [{ from: 0, to: 3 }, { from: 3, to: 6 }] });
  }
});

test("a file that is not a sweep has no I-V preview", () => {
  assert.equal(sweepPreview(parseResistametCsv(FOUR_POINT)), null);
});

test("legs split where the swept quantity turns round", () => {
  assert.deepEqual(sweepLegs([]), []);
  assert.deepEqual(sweepLegs([1]), [{ from: 0, to: 1 }]);
  assert.deepEqual(sweepLegs([0, 1, 2, 3]), [{ from: 0, to: 4 }]);
  assert.deepEqual(sweepLegs([3, 2, 1]), [{ from: 0, to: 3 }]);
  // the turning value measured twice: one point to each leg
  assert.deepEqual(sweepLegs([0, 1, 2, 2, 1, 0]), [{ from: 0, to: 3 }, { from: 3, to: 6 }]);
  // the turning value measured once: it ends the first leg
  assert.deepEqual(sweepLegs([0, 1, 2, 1, 0]), [{ from: 0, to: 3 }, { from: 3, to: 5 }]);
  // a run stopped partway back
  assert.deepEqual(sweepLegs([0, 1, 2, 2, 1]), [{ from: 0, to: 3 }, { from: 3, to: 5 }]);
  assert.deepEqual(sweepLegs([0, 0, 0]), [{ from: 0, to: 3 }]);
});

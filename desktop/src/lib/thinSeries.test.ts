import { test } from "node:test";
import assert from "node:assert/strict";
import { halveRows, newThinning, thin, type Columns, type ThinLimits } from "./thinSeries.ts";

const LIMITS: ThinLimits = { historyMax: 64, tailMin: 48, chunk: 16 };

function run(samples: number, value: (i: number) => number, limits = LIMITS) {
  const columns: Columns = { t: [], values: { r: [], aux: [] }, compliance: [] };
  let state = newThinning();
  let peakRows = 0;
  for (let i = 0; i < samples; i++) {
    columns.t.push(i * 0.01);
    (columns.values.r as number[]).push(value(i));
    (columns.values.aux as number[]).push(i % 7 === 0 ? NaN : -i);
    columns.compliance.push(i === 5 ? "V_COMP" : "OK");
    state = thin(columns, state, limits);
    peakRows = Math.max(peakRows, columns.t.length);
  }
  return { columns, state, peakRows };
}

test("four rows become two that hold each column's extremes in the order they came", () => {
  const columns: Columns = {
    t: [0, 1, 2, 3],
    values: { a: [5, 9, 1, 4], b: [NaN, 2, NaN, 3], c: [NaN, NaN, NaN, NaN] },
    compliance: ["OK", "OK", "I_COMP", "OK"],
  };
  assert.equal(halveRows(columns, 0, 4), 2);
  assert.deepEqual(columns.t, [0, 2]);
  assert.deepEqual(columns.values.a, [9, 1]);
  assert.deepEqual(columns.values.b, [2, 3]);
  assert.ok(Number.isNaN((columns.values.c as number[])[0]));
  assert.deepEqual(columns.compliance, ["I_COMP", "I_COMP"]);
});

test("a short run is left alone", () => {
  const { columns, state } = run(60, (i) => i);
  assert.equal(columns.t.length, 60);
  assert.deepEqual(state, { historyRows: 0, level: 0 });
});

test("a long run stays bounded, aligned, in time order, with its spike and a raw tail", () => {
  const samples = 100_000;
  const { columns, state, peakRows } = run(samples, (i) => (i === 1234 ? 1e6 : Math.sin(i / 50)));
  assert.ok(peakRows <= LIMITS.historyMax + 4 + LIMITS.tailMin + LIMITS.chunk, `peak ${peakRows}`);
  assert.ok(state.level > 0);
  const r = columns.values.r as number[];
  assert.equal(r.length, columns.t.length);
  assert.equal((columns.values.aux as number[]).length, columns.t.length);
  assert.equal(columns.compliance.length, columns.t.length);
  for (let i = 1; i < columns.t.length; i++) assert.ok((columns.t[i] as number) > (columns.t[i - 1] as number));
  assert.equal(Math.max(...r), 1e6);
  assert.equal(columns.compliance[0], "V_COMP");
  // The newest tailMin samples are exactly as they arrived.
  for (let k = 1; k <= LIMITS.tailMin; k++) {
    assert.equal(columns.t[columns.t.length - k], (samples - k) * 0.01);
    assert.equal(r[r.length - k], Math.sin((samples - k) / 50));
  }
});

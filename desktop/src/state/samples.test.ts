import { test } from "node:test";
import assert from "node:assert/strict";
import { applySample, getLatest, getSeries, resetSamples } from "./samples.ts";
import type { Event } from "../generated/events.ts";

function sample(i: number): Event<"sample"> {
  return {
    type: "sample", seq: i + 1, t: 0, run_id: "run-1", v: 1,
    payload: { elapsed_s: i * 0.01, t_unix: 0, compliance: "OK", values: { resistance: 100 + (i % 10) } },
  } as unknown as Event<"sample">;
}

test("a long run's series stops growing but its sample count does not", () => {
  resetSamples("resistance", "run-1");
  const total = 250_000;
  for (let i = 0; i < total; i++) applySample(sample(i));
  const series = getSeries();
  assert.ok(series.t.length < 235_000, `rows ${series.t.length}`);
  assert.equal((series.values.resistance as number[]).length, series.t.length);
  assert.equal(getLatest()?.count, total);
  assert.equal(getLatest()?.thinned, true);
  resetSamples();
  assert.equal(getSeries().thinning.level, 0);
});

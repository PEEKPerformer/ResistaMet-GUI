import { test } from "node:test";
import assert from "node:assert/strict";
import { fitSweep, type SweepLeg } from "./sweepFit.ts";

const close = (actual: number, expected: number, rel = 1e-9) =>
  assert.ok(Math.abs(actual - expected) <= rel * Math.abs(expected), `${actual} is not ${expected}`);

/** A repeatable stand-in for noise: zero mean over a whole cycle. */
const wobble = (i: number) => [1, -1, -1, 1, 0.5, -0.5][i % 6]!;

function leg(direction: "forward" | "reverse", voltages: number[], currents: number[], compliance?: string[]): SweepLeg {
  return { direction, voltages, currents, compliance: compliance ?? voltages.map(() => "OK") };
}

test("a clean line gives R either way round, with the points counted", () => {
  const v = [0, 0.1, 0.2, 0.3, 0.4];
  const i = v.map((x) => x / 100);
  for (const sourced of ["voltage", "current"] as const) {
    const fit = fitSweep([leg("forward", v, i)], sourced);
    close(fit.legs[0]!.r, 100);
    close(fit.pooled!.r, 100);
    assert.equal(fit.pooled!.n, 5);
    assert.equal(fit.pooled!.excluded, 0);
    close(fit.pooled!.r2, 1);
  }
});

test("the measured quantity is regressed on the sourced one", () => {
  // Current sourced exactly, voltage measured with noise: V on I recovers R.
  // I on V, the old way, reads high by 1/R².
  const currents = Array.from({ length: 24 }, (_, k) => k * 1e-4);
  const voltages = currents.map((c, k) => 100 * c + 0.03 * wobble(k));
  const right = fitSweep([leg("forward", voltages, currents)], "current").pooled!;
  const wrong = fitSweep([leg("forward", voltages, currents)], "voltage").pooled!;
  assert.ok(Math.abs(right.r - 100) < 2 * right.u, `R = ${right.r} ± ${right.u}`);
  assert.ok(right.r2 < 0.95, `the noise is large enough to tell: R² = ${right.r2}`);
  close(wrong.r, right.r / right.r2, 1e-9);
  assert.ok(wrong.r - 100 > 5, `the other regression reads ${wrong.r}`);
});

test("points in compliance are left out and counted", () => {
  const v = [0, 0.1, 0.2, 0.3, 0.4, 0.5];
  const i = [0, 0.001, 0.002, 0.003, 0.0035, 0.0035]; // clamps at 3.5 mA
  const comp = ["OK", "OK", "OK", "OK", "COMP", "COMP"];
  const fit = fitSweep([leg("forward", v, i, comp)], "voltage").pooled!;
  close(fit.r, 100);
  assert.equal(fit.n, 4);
  assert.equal(fit.excluded, 2);
  const blind = fitSweep([leg("forward", v, i)], "voltage").pooled!;
  assert.ok(blind.r > 105, `with the clamped points in, R reads ${blind.r}`);
});

test("legs that agree are pooled", () => {
  const currents = Array.from({ length: 12 }, (_, k) => k * 1e-4);
  const forward = leg("forward", currents.map((c, k) => 100 * c + 1e-4 * wobble(k)), currents);
  const back = [...currents].reverse();
  const reverse = leg("reverse", back.map((c, k) => 100 * c + 1e-4 * wobble(k + 3)), back);
  const fit = fitSweep([forward, reverse], "current");
  assert.equal(fit.legs.length, 2);
  assert.deepEqual(fit.legs.map((l) => l.direction), ["forward", "reverse"]);
  assert.ok(fit.pooled !== null);
  assert.equal(fit.pooled.n, 24);
  close(fit.pooled.r, 100, 1e-3);
});

test("legs that disagree are reported apart and not pooled", () => {
  const currents = Array.from({ length: 12 }, (_, k) => k * 1e-4);
  const forward = leg("forward", currents.map((c, k) => 100 * c + 1e-5 * wobble(k)), currents);
  const back = [...currents].reverse();
  const reverse = leg("reverse", back.map((c, k) => 104 * c + 1e-5 * wobble(k)), back); // the sample warmed up
  const fit = fitSweep([forward, reverse], "current");
  close(fit.legs[0]!.r, 100, 1e-3);
  close(fit.legs[1]!.r, 104, 1e-3);
  assert.equal(fit.pooled, null);
});

test("noiseless legs with the same R agree, though they have no scatter", () => {
  const v = [0, 0.1, 0.2, 0.3];
  const i = v.map((x) => x / 100);
  const fit = fitSweep([leg("forward", v, i), leg("reverse", [...v].reverse(), [...i].reverse())], "voltage");
  assert.ok(fit.pooled !== null);
  close(fit.pooled.r, 100);
});

test("too few points, or none that vary, give no resistance", () => {
  assert.deepEqual(fitSweep([], "voltage"), { legs: [], pooled: null });
  const one = fitSweep([leg("forward", [1], [0.01])], "voltage").pooled!;
  assert.ok(Number.isNaN(one.r));
  assert.equal(one.n, 1);
  const flat = fitSweep([leg("forward", [1, 1, 1], [0.01, 0.011, 0.009])], "voltage").pooled!;
  assert.ok(Number.isNaN(flat.r));
  const two = fitSweep([leg("forward", [0, 1], [0, 0.01])], "voltage").pooled!;
  close(two.r, 100);
  assert.ok(Number.isNaN(two.u));
  const gaps = fitSweep([leg("forward", [0, NaN, 1, 2], [0, 0.005, NaN, 0.02])], "voltage").pooled!;
  close(gaps.r, 100);
  assert.equal(gaps.excluded, 2);
});

import { test } from "node:test";
import assert from "node:assert/strict";
import type { SpotPreflight } from "../../generated/maps";
import { positionFeedback, preflightBody, signedPercent } from "./spotPreflight.ts";

const BASE: SpotPreflight = { angle_deg: 0, edge_warn_pct: 1, sample: { shape: "circle", diameter_mm: 50 }, spacing_mm: 1.016 };

test("the body is a Start body without the run", () => {
  assert.deepEqual(preflightBody("ada", { fpp_spacing_cm: 0.1 }, { x_mm: 1, y_mm: -2 }, null), {
    username: "ada",
    overrides: { fpp_spacing_cm: 0.1 },
    spot: { map_id: "preflight", index: 0, label: "next", x_mm: 1, y_mm: -2 },
  });
  assert.equal(preflightBody("ada", {}, { x_mm: 0, y_mm: 0 }, "20260919-153000_Si_ab12").spot?.map_id, "20260919-153000_Si_ab12");
});

test("a fraction reads as a signed percentage", () => {
  assert.equal(signedPercent(0.0123), "+1.2 %");
  assert.equal(signedPercent(-0.25), "−25 %");
  assert.equal(signedPercent(0), "0.0 %");
});

test("without an answer the distance check stands, marked as an estimate", () => {
  assert.deepEqual(positionFeedback({ state: "caution", clearanceS: 2 }, "nearest tip 2.0 s from the edge", null), {
    state: "caution",
    text: "nearest tip 2.0 s from the edge",
    exact: false,
  });
  assert.equal(positionFeedback(null, "", null), null);
});

test("an answer that checked nothing leaves the distance check in place", () => {
  const feedback = positionFeedback({ state: "ok", clearanceS: 9 }, "local", { ...BASE, checked: false });
  assert.deepEqual(feedback, { state: "ok", text: "local", exact: false });
});

test("off the sample is the backend's word, without the stand-in label", () => {
  const answer: SpotPreflight = { ...BASE, checked: true, off_sample: true, message: "Spot 'next' is off the sample: a probe tip is 0.52 s beyond the edge." };
  assert.deepEqual(positionFeedback({ state: "ok", clearanceS: 1 }, "local", answer), {
    state: "off",
    text: "off the sample: a probe tip is 0.52 s beyond the edge.",
    exact: true,
  });
});

test("near an edge is a caution carrying the backend's sentence", () => {
  const message = "Spot 'next' is 1.5 s from the edge: the geometry factor this run applies (4.532) differs from the factor at the spot (4.4) by 2.9 % (threshold 1 %). No position correction is applied.";
  const feedback = positionFeedback(null, "", { ...BASE, checked: true, near_edge: true, relative_error_rows: 0.029, message });
  assert.equal(feedback?.state, "caution");
  assert.ok(feedback?.text.startsWith("1.5 s from the edge: "));
});

test("a quiet spot shows the error the rows would carry, else the centred one", () => {
  const rows = positionFeedback(null, "", { ...BASE, checked: true, relative_error: 0.004, relative_error_rows: -0.002, message: null });
  assert.deepEqual(rows, { state: "ok", text: "Rs −0.2 % off with the factor this run applies", exact: true });
  const centre = positionFeedback(null, "", { ...BASE, checked: true, relative_error: 0.004, message: null });
  assert.equal(centre?.text, "Rs +0.4 % off for assuming a centred probe");
});

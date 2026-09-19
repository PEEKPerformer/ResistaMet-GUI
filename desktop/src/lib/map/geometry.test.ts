import { test } from "node:test";
import assert from "node:assert/strict";
import {
  calibratedScale,
  edgeClearanceS,
  fitViewport,
  initialRegistration,
  nearestSiteCells,
  niceLength,
  niceTicks,
  num,
  outlineFromSettings,
  photoTransform,
  preflight,
  probeTips,
  toFigure,
  toSample,
  type Outline,
  type Point,
} from "./geometry.ts";

const close = (a: number, b: number, tolerance = 1e-9) => assert.ok(Math.abs(a - b) <= tolerance, `${a} vs ${b}`);

const SQUARE: Outline = { shape: "rectangle", widthMm: 20, lengthMm: 20 };
const DISC: Outline = { shape: "circle", diameterMm: 20 };

test("the outline is the fpp_sample_* keys once a shape is chosen", () => {
  assert.deepEqual(outlineFromSettings({ fpp_sample_shape: "circle", fpp_sample_diameter_mm: 50.8 }), { shape: "circle", diameterMm: 50.8 });
  assert.deepEqual(outlineFromSettings({ fpp_sample_shape: "rectangle", fpp_sample_width_mm: 10, fpp_sample_length_mm: 30 }), {
    shape: "rectangle",
    widthMm: 10,
    lengthMm: 30,
  });
});

test("a chosen shape without its dimensions cannot be described", () => {
  assert.equal(outlineFromSettings({ fpp_sample_shape: "circle", fpp_sample_diameter_mm: 0 }), null);
  assert.equal(outlineFromSettings({ fpp_sample_shape: "rectangle", fpp_sample_width_mm: 10 }), null);
});

test("unbounded falls back to the legacy keys, as the backend does", () => {
  assert.deepEqual(outlineFromSettings({ fpp_sample_shape: "unbounded", fpp_diameter_cm: 0 }), { shape: "unbounded" });
  assert.deepEqual(outlineFromSettings({}), { shape: "unbounded" });
  assert.deepEqual(outlineFromSettings({ fpp_diameter_cm: 2, fpp_geometry: "circle" }), { shape: "circle", diameterMm: 20 });
  // The legacy diameter of a rectangle is the side across the array (y).
  assert.deepEqual(outlineFromSettings({ fpp_diameter_cm: 1, fpp_geometry: "rectangle_3" }), { shape: "rectangle", widthMm: 10, lengthMm: 30 });
});

test("the tips are 1.5 s and 0.5 s either side of the centre, along the array", () => {
  const along = probeTips({ x: 2, y: 1 }, 0, 1);
  assert.deepEqual(along.map((t) => t.x), [0.5, 1.5, 2.5, 3.5]);
  assert.deepEqual(along.map((t) => t.y), [1, 1, 1, 1]);
  // 90 degrees is anticlockwise from +x: the last tip is *up* the sample.
  const up = probeTips({ x: 0, y: 0 }, 90, 2);
  close(up[3].y, 3);
  close(up[0].y, -3);
  close(up[3].x, 0);
});

// Reference values from calculations_geometry.*_edge_clearance.
test("edge clearance agrees with the backend", () => {
  close(edgeClearanceS(DISC, { x: 7, y: 0 }, 0, 1)!, 1.5);
  close(edgeClearanceS(DISC, { x: 3, y: 4 }, 30, 1)!, 3.5934230173937696);
  close(edgeClearanceS(SQUARE, { x: 0, y: 7 }, 0, 1)!, 3.0);
  close(edgeClearanceS({ shape: "rectangle", widthMm: 10, lengthMm: 30 }, { x: 12, y: -2 }, 90, 1.016)!, 1.4527559055118109);
  close(edgeClearanceS(SQUARE, { x: 9, y: 0 }, 0, 1)!, -0.5);
  assert.equal(edgeClearanceS({ shape: "unbounded" }, { x: 0, y: 0 }, 0, 1), null);
});

test("pre-flight: off the sample, near an edge, clear, and no edges at all", () => {
  assert.equal(preflight(SQUARE, { x: 0, y: 0 }, 0, 1).state, "ok");
  assert.equal(preflight(SQUARE, { x: 0, y: 7 }, 0, 1).state, "caution");
  assert.equal(preflight(SQUARE, { x: 9, y: 0 }, 0, 1).state, "off");
  // A tip exactly on the edge is off: the factor diverges there.
  assert.equal(preflight(SQUARE, { x: 8.5, y: 0 }, 0, 1).state, "off");
  assert.equal(preflight({ shape: "unbounded" }, { x: 99, y: 99 }, 0, 1).state, "none");
});

test("a centred probe on a small sample is not a caution", () => {
  const small: Outline = { shape: "circle", diameterMm: 8 };
  assert.equal(preflight(small, { x: 0, y: 0 }, 0, 1).state, "ok");
  assert.equal(preflight(small, { x: 2, y: 0 }, 0, 1).state, "caution");
});

test("y is up on the sample and down in the figure", () => {
  const view = fitViewport({ x: 10, y: 10 }, { x: 0, y: 0, width: 200, height: 200 });
  assert.equal(view.pxPerMm, 10);
  // The sample's top-right quadrant is the figure's top-right: larger x,
  // *smaller* y.
  assert.deepEqual(toFigure(view, { x: 5, y: 5 }), { x: 150, y: 50 });
  assert.deepEqual(toFigure(view, { x: 0, y: -10 }), { x: 100, y: 200 });
  // A click near the top of the figure is a positive sample y.
  assert.deepEqual(toSample(view, { x: 100, y: 20 }), { x: 0, y: 8 });
});

test("toSample undoes toFigure", () => {
  const view = { originX: 312.5, originY: 256, pxPerMm: 17.3 };
  for (const p of [{ x: 0, y: 0 }, { x: -3.2, y: 7.7 }, { x: 12, y: -0.01 }] as Point[]) {
    const back = toSample(view, toFigure(view, p));
    close(back.x, p.x);
    close(back.y, p.y);
  }
});

test("the viewport keeps the aspect of the sample", () => {
  const view = fitViewport({ x: 20, y: 5 }, { x: 10, y: 10, width: 400, height: 300 });
  assert.equal(view.pxPerMm, 10); // limited by the 40 mm length, not the 10 mm width
  assert.deepEqual([view.originX, view.originY], [210, 160]);
  assert.equal(fitViewport({ x: 20, y: 5 }, { x: 0, y: 0, width: 400, height: 300 }, 5).pxPerMm, 8);
});

test("scale bar lengths are 1, 2 or 5 times a power of ten", () => {
  assert.equal(niceLength(7.3), 5);
  assert.equal(niceLength(2), 2);
  assert.equal(niceLength(0.19), 0.1);
  assert.equal(niceLength(38), 20);
  assert.equal(niceLength(0), 0);
});

test("ticks are round numbers inside the range", () => {
  assert.deepEqual(niceTicks(0, 10), [0, 2, 4, 6, 8, 10]);
  assert.deepEqual(niceTicks(5.651, 5.659), [5.652, 5.654, 5.656, 5.658]);
  assert.deepEqual(niceTicks(3, 3), [3]);
});

test("a photograph turns anticlockwise on the sample, which is negative in SVG", () => {
  const view = { originX: 100, originY: 100, pxPerMm: 10 };
  const transform = photoTransform(view, { mmPerPx: 0.05, centreXmm: 1, centreYmm: 2, rotationDeg: 15 }, 400, 200);
  assert.equal(transform, "translate(110 80) rotate(-15) scale(0.5) translate(-200 -100)");
});

test("a new photograph covers the outline", () => {
  const reg = initialRegistration({ x: 10, y: 10 }, 4000, 2000);
  assert.equal(reg.mmPerPx, 0.01); // the 2000 px side has to span 20 mm
  assert.deepEqual([reg.centreXmm, reg.centreYmm, reg.rotationDeg], [0, 0, 0]);
});

test("two points a known distance apart set the scale", () => {
  const reg = { mmPerPx: 1, centreXmm: 0, centreYmm: 0, rotationDeg: 0 };
  // 300 px apart under the provisional 1 mm/px, really 15 mm.
  close(calibratedScale(reg, { x: -100, y: 0 }, { x: 200, y: 0 }, 15)!, 0.05);
  assert.equal(calibratedScale(reg, { x: 1, y: 1 }, { x: 1, y: 1 }, 15), null);
  assert.equal(calibratedScale(reg, { x: 0, y: 0 }, { x: 1, y: 0 }, 0), null);
});

test("nearest-spot cells split the bounds along the perpendicular bisector", () => {
  const bounds: Point[] = [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 10 }, { x: 0, y: 10 }];
  const [left, right] = nearestSiteCells([{ x: 2, y: 5 }, { x: 8, y: 5 }], bounds);
  assert.deepEqual(left!.map((p) => p.x).sort((a, b) => a - b), [0, 0, 5, 5]);
  assert.deepEqual(right!.map((p) => p.x).sort((a, b) => a - b), [5, 5, 10, 10]);
  // One site owns everything.
  assert.deepEqual(nearestSiteCells([{ x: 1, y: 1 }], bounds), [bounds]);
});

test("numbers are written without exponents or trailing zeros", () => {
  assert.equal(num(1.5), "1.5");
  assert.equal(num(2), "2");
  assert.equal(num(-0.0001), "0");
  assert.equal(num(1e-7, 6), "0");
  assert.equal(num(0.0254, 6), "0.0254");
});

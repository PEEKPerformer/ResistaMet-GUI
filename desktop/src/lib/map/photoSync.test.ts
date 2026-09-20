import { test } from "node:test";
import assert from "node:assert/strict";
import type { MapImage } from "../../generated/maps";
import { placementFor, registrationBody, uploadRefusal } from "./photoSync.ts";

const REG = { mmPerPx: 0.05, centreXmm: 1.5, centreYmm: -2, rotationDeg: 12 };
const IMAGE: MapImage = { file: "m_sample.png", sha256: "ab", bytes: 10 };

test("a placement maps onto the backend's field names", () => {
  assert.deepEqual(registrationBody({ registration: REG, calibrated: true, naturalWidth: 800, naturalHeight: 600 }, "ab"), {
    sha256: "ab",
    image_width_px: 800,
    image_height_px: 600,
    mm_per_px: 0.05,
    centre_x_mm: 1.5,
    centre_y_mm: -2,
    rotation_deg: 12,
    calibrated: true,
  });
});

test("the backend's placement wins when it fits the image", () => {
  const registration = registrationBody({ registration: REG, calibrated: false, naturalWidth: 800, naturalHeight: 600 }, "ab");
  const placed = placementFor({ ...IMAGE, registration }, { width: 800, height: 600 }, null, { x: 10, y: 10 });
  assert.deepEqual(placed, { registration: REG, calibrated: false });
});

test("a placement for another pixel size is not applied", () => {
  const registration = registrationBody({ registration: REG, calibrated: true, naturalWidth: 400, naturalHeight: 300 }, "ab");
  const placed = placementFor({ ...IMAGE, registration }, { width: 800, height: 600 }, null, { x: 10, y: 10 });
  assert.deepEqual(placed, { registration: { mmPerPx: 1 / 30, centreXmm: 0, centreYmm: 0, rotationDeg: 0 }, calibrated: true });
});

test("without one, this browser's record of the same file is used", () => {
  const remembered = { sha256: "ab", registration: REG, calibrated: true };
  assert.deepEqual(placementFor(IMAGE, { width: 800, height: 600 }, remembered, null), { registration: REG, calibrated: true });
  const other = placementFor(IMAGE, { width: 800, height: 600 }, { ...remembered, sha256: "cd" }, null);
  assert.deepEqual(other, { registration: { mmPerPx: 1, centreXmm: 0, centreYmm: 0, rotationDeg: 0 }, calibrated: false });
});

test("only a different image on the map offers Replace", () => {
  assert.equal(uploadRefusal(409, "exists").conflict, true);
  assert.deepEqual(uploadRefusal(415, "image/gif is not accepted"), { conflict: false, notice: "Kept in this browser only: image/gif is not accepted" });
  assert.equal(uploadRefusal(0, "").conflict, false);
});

// The photograph's copy beside the runs: the shapes the backend's image
// routes take and return, and what the client makes of them. No fetch here.

import type { MapImage, MapImageRegistration } from "../../generated/maps";
import { initialRegistration, type Point, type Registration } from "./geometry.ts";

/** How long a placement has to rest before it is sent. */
export const REGISTRATION_DEBOUNCE_MS = 600;

export interface Placement {
  registration: Registration;
  calibrated: boolean;
}

/** PUT /maps/{id}/registration for a photograph placed like this. */
export function registrationBody(
  photo: Placement & { naturalWidth: number; naturalHeight: number },
  sha256: string,
): MapImageRegistration {
  const { mmPerPx, centreXmm, centreYmm, rotationDeg } = photo.registration;
  return {
    sha256,
    image_width_px: photo.naturalWidth,
    image_height_px: photo.naturalHeight,
    mm_per_px: mmPerPx,
    centre_x_mm: centreXmm,
    centre_y_mm: centreYmm,
    rotation_deg: rotationDeg,
    calibrated: photo.calibrated,
  };
}

/** Where a photograph fetched from the backend goes: where the backend says,
 *  if it holds a placement for an image of this size; else where this
 *  browser last had the same file; else fitted to the outline. */
export function placementFor(
  image: MapImage,
  size: { width: number; height: number },
  remembered: (Placement & { sha256: string | null }) | null,
  halfExtents: Point | null,
): Placement {
  const reg = image.registration;
  if (reg && reg.image_width_px === size.width && reg.image_height_px === size.height && reg.mm_per_px > 0) {
    return {
      registration: { mmPerPx: reg.mm_per_px, centreXmm: reg.centre_x_mm, centreYmm: reg.centre_y_mm, rotationDeg: reg.rotation_deg },
      calibrated: reg.calibrated === true,
    };
  }
  if (remembered !== null && remembered.sha256 === image.sha256) {
    return { registration: remembered.registration, calibrated: remembered.calibrated };
  }
  return halfExtents
    ? { registration: initialRegistration(halfExtents, size.width, size.height), calibrated: true }
    : { registration: { mmPerPx: 1, centreXmm: 0, centreYmm: 0, rotationDeg: 0 }, calibrated: false };
}

export interface UploadRefusal {
  /** The map holds a different image: Replace would go through. */
  conflict: boolean;
  notice: string;
}

/** What a refused upload means. The photograph stays in the browser either
 *  way. `status` 0 is no answer at all. */
export function uploadRefusal(status: number, detail: string): UploadRefusal {
  if (status === 409) return { conflict: true, notice: "This map has a different photo beside its runs." };
  if (status === 413 || status === 415) return { conflict: false, notice: `Kept in this browser only: ${detail}` };
  return { conflict: false, notice: `Kept in this browser only (${detail || "the backend did not answer"}).` };
}

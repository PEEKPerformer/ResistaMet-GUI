// The optional photograph under the map.
//
// The image is loaded from a file the operator picks and shown through an
// object URL. Nothing is uploaded and the bytes are not copied anywhere: what
// is kept, in localStorage under the map's id, is where the image sits on the
// sample (four numbers), its pixel size, its file name and its SHA-256. After
// a reload the image is gone and the numbers are not; picking the same file
// again -- same hash -- puts it back where it was.
//
// Copying the image beside the runs, as the spots design wants, needs a
// backend route that does not exist yet.

import { useSyncExternalStore } from "react";
import { initialRegistration, type Point, type Registration } from "../lib/map/geometry";

/** What is kept about a photograph: everything but the pixels. */
export interface PhotoRecord {
  name: string;
  /** Hex SHA-256 of the file, or null where WebCrypto is unavailable. */
  sha256: string | null;
  naturalWidth: number;
  naturalHeight: number;
  registration: Registration;
  /** The scale is real: fitted to an outline, or set from two points. */
  calibrated: boolean;
}

export interface PhotoState {
  /** The loaded image, or null. */
  photo: (PhotoRecord & { url: string; file: File }) | null;
  /** The map the photograph is stored with; null while no run has named one. */
  mapId: string | null;
  /** A stored record whose image is not loaded (after a reload). */
  stored: PhotoRecord | null;
  error: string | null;
}

const KEY_PREFIX = "resistamet.map.photo.";

let state: PhotoState = { photo: null, mapId: null, stored: null, error: null };
const listeners = new Set<() => void>();

function publish(next: PhotoState): void {
  state = next;
  for (const listener of listeners) listener();
}

export function getMapPhoto(): PhotoState {
  return state;
}

export function useMapPhoto(): PhotoState {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getMapPhoto,
    getMapPhoto,
  );
}

function readRecord(mapId: string): PhotoRecord | null {
  try {
    const stored = localStorage.getItem(KEY_PREFIX + mapId);
    if (!stored) return null;
    const r = JSON.parse(stored) as Partial<PhotoRecord>;
    const reg = r.registration;
    if (
      typeof r.name !== "string" ||
      typeof r.naturalWidth !== "number" ||
      typeof r.naturalHeight !== "number" ||
      !reg ||
      !(reg.mmPerPx > 0)
    ) {
      return null;
    }
    return {
      name: r.name,
      sha256: typeof r.sha256 === "string" ? r.sha256 : null,
      naturalWidth: r.naturalWidth,
      naturalHeight: r.naturalHeight,
      registration: { mmPerPx: reg.mmPerPx, centreXmm: reg.centreXmm || 0, centreYmm: reg.centreYmm || 0, rotationDeg: reg.rotationDeg || 0 },
      calibrated: r.calibrated === true,
    };
  } catch {
    return null;
  }
}

function persist(): void {
  if (state.mapId === null) return;
  try {
    if (state.photo) {
      const { name, sha256, naturalWidth, naturalHeight, registration, calibrated } = state.photo;
      const record: PhotoRecord = { name, sha256, naturalWidth, naturalHeight, registration, calibrated };
      localStorage.setItem(KEY_PREFIX + state.mapId, JSON.stringify(record));
    } else if (state.stored === null) {
      localStorage.removeItem(KEY_PREFIX + state.mapId);
    }
  } catch {
    // storage full or unavailable; the photograph still works for this session
  }
}

async function sha256Hex(file: File): Promise<string | null> {
  if (typeof crypto === "undefined" || !crypto.subtle) return null;
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

function naturalSize(url: string): Promise<{ width: number; height: number }> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve({ width: image.naturalWidth, height: image.naturalHeight });
    image.onerror = () => reject(new Error("not an image this browser can show"));
    image.src = url;
  });
}

/** Load a photograph. `halfExtents` is the outline's, or null for a sample
 *  with no outline, where the photograph is the canvas and has no scale until
 *  the operator sets one. */
export async function addPhoto(file: File, halfExtents: Point | null): Promise<void> {
  const url = URL.createObjectURL(file);
  try {
    const [size, sha256] = await Promise.all([naturalSize(url), sha256Hex(file)]);
    if (!(size.width > 0 && size.height > 0)) throw new Error("the image has no size");
    // The same file as before goes back where it was.
    const known = state.stored ?? state.photo;
    const same = known !== null && sha256 !== null && known.sha256 === sha256 && known.naturalWidth === size.width && known.naturalHeight === size.height;
    const registration = same
      ? known.registration
      : halfExtents
        ? initialRegistration(halfExtents, size.width, size.height)
        : { mmPerPx: 1, centreXmm: 0, centreYmm: 0, rotationDeg: 0 };
    const calibrated = same ? known.calibrated : halfExtents !== null;
    if (state.photo) URL.revokeObjectURL(state.photo.url);
    publish({
      ...state,
      photo: { file, url, name: file.name, sha256, naturalWidth: size.width, naturalHeight: size.height, registration, calibrated },
      stored: null,
      error: null,
    });
    persist();
  } catch (e) {
    URL.revokeObjectURL(url);
    publish({ ...state, error: `${file.name}: ${e instanceof Error ? e.message : String(e)}` });
  }
}

export function removePhoto(): void {
  if (state.photo) URL.revokeObjectURL(state.photo.url);
  publish({ ...state, photo: null, stored: null, error: null });
  persist();
}

export function setRegistration(patch: Partial<Registration>, calibrated?: boolean): void {
  if (!state.photo) return;
  publish({
    ...state,
    photo: { ...state.photo, registration: { ...state.photo.registration, ...patch }, calibrated: calibrated ?? state.photo.calibrated },
  });
  persist();
}

/** Follow the map. `mapId` is the active map, or null when the next run will
 *  start one; `fresh` says that is because the operator asked for a new map
 *  of the same sample, not because the sample changed. */
export function followMap(mapId: string | null, fresh: boolean): void {
  if (mapId === state.mapId) return;
  if (mapId !== null && state.mapId === null && state.photo) {
    // The photograph was there before the map's first run: it is this map's.
    publish({ ...state, mapId });
    persist();
    return;
  }
  if (mapId === null && fresh && state.photo) {
    // A new map of the same sample keeps the photograph, as a draft.
    publish({ ...state, mapId: null, stored: null });
    return;
  }
  if (state.photo) URL.revokeObjectURL(state.photo.url);
  publish({ photo: null, mapId, stored: mapId === null ? null : readRecord(mapId), error: null });
}

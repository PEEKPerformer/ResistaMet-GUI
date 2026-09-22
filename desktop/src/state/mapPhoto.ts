// The optional photograph under the map.
//
// The image is loaded from a file the operator picks and shown through an
// object URL. Once the map has an id the bytes are copied beside its runs
// (PUT /maps/{id}/image) and the placement follows every change, so a reload
// or another PC shows the same picture in the same place.
//
// The browser's own record stays as the fallback for a backend that refuses
// or cannot be reached: in localStorage under the map's id, where the image
// sits on the sample (four numbers), its pixel size, its file name and its
// SHA-256. Picking the same file again -- same hash -- puts it back.

import { useSyncExternalStore } from "react";
import type { ApiClient } from "../lib/api";
import { ApiError } from "../lib/api";
import type { MapImage } from "../generated/maps";
import { initialRegistration, type Point, type Registration } from "../lib/map/geometry";
import { placementFor, registrationBody, REGISTRATION_DEBOUNCE_MS, uploadRefusal } from "../lib/map/photoSync";

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
  /** Where the loaded image is kept: beside the runs ("stored"), in this
   *  browser only ("local"), or in this browser while the map holds a
   *  different one ("conflict", which Replace resolves). */
  server: "local" | "stored" | "conflict";
  /** Why the image is not beside the runs, when the backend said. */
  notice: string | null;
}

const KEY_PREFIX = "resistamet.map.photo.";

let state: PhotoState = { photo: null, mapId: null, stored: null, error: null, server: "local", notice: null };

/** Who the image routes are asked as. null: nothing leaves the browser. */
let backend: { api: ApiClient; user: string } | null = null;
/** Maps whose stored photograph the operator took off the map: it is not
 *  fetched again this session. The backend has no route to delete one. */
const dismissed = new Set<string>();
let loading: string | null = null;
let registrationTimer: ReturnType<typeof setTimeout> | undefined;

export function setPhotoBackend(next: { api: ApiClient; user: string } | null): void {
  backend = next;
}
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
      server: "local",
      notice: null,
    });
    persist();
    if (state.mapId !== null) dismissed.delete(state.mapId);
    void upload(false);
  } catch (e) {
    URL.revokeObjectURL(url);
    publish({ ...state, error: `${file.name}: ${e instanceof Error ? e.message : String(e)}` });
  }
}

export function removePhoto(): void {
  if (state.photo) URL.revokeObjectURL(state.photo.url);
  if (state.mapId !== null) dismissed.add(state.mapId);
  publish({ ...state, photo: null, stored: null, error: null, server: "local", notice: null });
  persist();
}

/** Copy the loaded image beside the map's runs, then its placement. */
async function upload(replace: boolean): Promise<void> {
  const { photo, mapId } = state;
  if (backend === null || photo === null || mapId === null) return;
  const { file } = photo;
  const current = () => state.photo?.file === file && state.mapId === mapId;
  try {
    const stored = await backend.api.putMapImage(mapId, backend.user, file, file.type || "application/octet-stream", replace);
    if (!current() || !state.photo) return;
    // The backend's hash is of the bytes it holds, and is there even where
    // WebCrypto is not.
    publish({ ...state, photo: { ...state.photo, sha256: stored.sha256 }, server: "stored", notice: null });
    persist();
    await pushRegistration();
  } catch (e) {
    if (!current()) return;
    const refusal = uploadRefusal(e instanceof ApiError ? e.status : 0, e instanceof Error ? e.message : String(e));
    publish({ ...state, server: refusal.conflict ? "conflict" : "local", notice: refusal.notice });
  }
}

/** Move the map's other image aside and store this one. */
export function replaceStoredPhoto(): void {
  void upload(true);
}

async function pushRegistration(): Promise<void> {
  const { photo, mapId } = state;
  if (backend === null || photo === null || mapId === null || photo.sha256 === null || state.server !== "stored") return;
  try {
    await backend.api.putMapRegistration(mapId, backend.user, registrationBody(photo, photo.sha256));
  } catch (e) {
    if (state.photo?.file === photo.file) publish({ ...state, notice: `Placement kept in this browser only (${e instanceof Error ? e.message : String(e)}).` });
  }
}

/** Show the photograph the backend holds for this map, where it was put.
 *  Does nothing while an image is loaded or after the operator removed it. */
export async function loadStoredPhoto(mapId: string, image: MapImage, halfExtents: Point | null): Promise<void> {
  if (backend === null || state.photo !== null || state.mapId !== mapId || dismissed.has(mapId) || loading === mapId) return;
  loading = mapId;
  try {
    const blob = await backend.api.mapImage(mapId, backend.user);
    const file = new File([blob], image.file, { type: blob.type });
    const url = URL.createObjectURL(file);
    try {
      const size = await naturalSize(url);
      if (state.photo !== null || state.mapId !== mapId || dismissed.has(mapId)) throw new Error("superseded");
      const placed = placementFor(image, size, state.stored, halfExtents);
      publish({
        ...state,
        photo: { file, url, name: state.stored?.name ?? image.file, sha256: image.sha256, naturalWidth: size.width, naturalHeight: size.height, ...placed },
        stored: null,
        error: null,
        server: "stored",
        notice: null,
      });
      persist();
    } catch (e) {
      URL.revokeObjectURL(url);
      throw e;
    }
  } catch {
    // The browser's record, if any, stays on offer as before.
  } finally {
    loading = null;
  }
}

export function setRegistration(patch: Partial<Registration>, calibrated?: boolean): void {
  if (!state.photo) return;
  publish({
    ...state,
    photo: { ...state.photo, registration: { ...state.photo.registration, ...patch }, calibrated: calibrated ?? state.photo.calibrated },
  });
  persist();
  clearTimeout(registrationTimer);
  registrationTimer = setTimeout(() => void pushRegistration(), REGISTRATION_DEBOUNCE_MS);
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
    void upload(false);
    return;
  }
  if (mapId === null && fresh && state.photo) {
    // A new map of the same sample keeps the photograph, as a draft.
    publish({ ...state, mapId: null, stored: null, server: "local", notice: null });
    return;
  }
  if (state.photo) URL.revokeObjectURL(state.photo.url);
  publish({ photo: null, mapId, stored: mapId === null ? null : readRecord(mapId), error: null, server: "local", notice: null });
}

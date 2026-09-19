// Four-point spots and the map they belong to.
//
// A spot is a run: the backend records it in the run's file, works out its
// statistics, and assembles the map from the files (GET /maps/{id}). This
// store holds what the UI adds to that -- which map the next run joins, what
// the next spot is called and where it is -- and caches what the backend
// says: the map, the last spot's statistics, the last geometry warning.
// No arithmetic on measured values happens here.
//
// Which map: the rule of the PySide6 window (schema/map_session.py). A map is
// the runs of one operator on one sample name. A new one starts when the
// application starts, when the operator or the sample name changes, and on
// "New map". The id is minted when a run needs it, not while a name is typed.
// A reload of the page is not an application start: the current map is kept
// in localStorage, and sessionStorage -- which a reload keeps and a restart
// does not -- tells the two apart.

import { useSyncExternalStore } from "react";
import type { Event, GeometryWarningPayload, SpotCompletePayload } from "../generated/events";
import type { SpotMap } from "../generated/maps";
import type { SpotRequest } from "../generated/settings";
import { newMapId, nextFreeIndex, randomToken, sameOwner, spotLabel, type MapOwner } from "../lib/map/mapId";

/** Open question 3 of the spots design. false: a map outlives the
 *  application, and coming back to a sample continues its map. */
const NEW_MAP_ON_APP_START = true;

const CURRENT_KEY = "resistamet.map.current";
const SESSION_KEY = "resistamet.map.session";

export interface CurrentMap extends MapOwner {
  mapId: string;
}

export interface SpotsState {
  /** The map the next run joins, if it is for the same operator and sample. */
  current: CurrentMap | null;
  /** GET /maps/{current.mapId}, or null before the map's first run. */
  map: SpotMap | null;
  mapError: string | null;
  /** The spot of the four-point run in progress, as run_started reported it. */
  running: SpotRequest | null;
  /** The backend's statistics for the last four-point run. */
  lastSpot: SpotCompletePayload | null;
  /** What the backend said about the position of the run in progress, or of
   *  the last one. */
  warning: GeometryWarningPayload | null;
  /** Bumped by every spot_complete: the map has changed on disk. */
  completions: number;
  /** The Spot name field. Empty: the automatic name. */
  label: string;
  /** The next run measures this spot again instead of a new one. */
  redo: { index: number; label: string } | null;
  /** Where the next spot is, mm from the sample's centre, y up. */
  pending: { x_mm: number; y_mm: number } | null;
}

function loadCurrent(): CurrentMap | null {
  try {
    const freshStart = sessionStorage.getItem(SESSION_KEY) === null;
    sessionStorage.setItem(SESSION_KEY, "1");
    if (freshStart && NEW_MAP_ON_APP_START) {
      localStorage.removeItem(CURRENT_KEY);
      return null;
    }
    const stored = localStorage.getItem(CURRENT_KEY);
    const parsed = stored ? (JSON.parse(stored) as Partial<CurrentMap>) : null;
    if (parsed && typeof parsed.mapId === "string" && typeof parsed.user === "string" && typeof parsed.sample === "string") {
      return { mapId: parsed.mapId, user: parsed.user, sample: parsed.sample };
    }
  } catch {
    // no storage: every load starts a new map
  }
  return null;
}

function saveCurrent(current: CurrentMap | null): void {
  try {
    if (current) localStorage.setItem(CURRENT_KEY, JSON.stringify(current));
    else localStorage.removeItem(CURRENT_KEY);
  } catch {
    // fine
  }
}

let state: SpotsState = {
  current: loadCurrent(),
  map: null,
  mapError: null,
  running: null,
  lastSpot: null,
  warning: null,
  completions: 0,
  label: "",
  redo: null,
  pending: null,
};

const listeners = new Set<() => void>();

function publish(next: SpotsState): void {
  state = next;
  for (const listener of listeners) listener();
}

export function getSpots(): SpotsState {
  return state;
}

export function useSpots(): SpotsState {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getSpots,
    getSpots,
  );
}

/** The current map's id, if it belongs to this operator and sample. */
export function activeMapId(s: SpotsState, owner: MapOwner | null): string | null {
  return owner !== null && s.current !== null && sameOwner(s.current, owner) ? s.current.mapId : null;
}

/** The map as last fetched, if it is the active one. */
export function activeMap(s: SpotsState, owner: MapOwner | null): SpotMap | null {
  const id = activeMapId(s, owner);
  return id !== null && s.map !== null && s.map.map_id === id ? s.map : null;
}

/** The map the next run by `owner` joins, minted now if there is none. */
export function ensureMapId(owner: MapOwner): string {
  const existing = activeMapId(state, owner);
  if (existing !== null) return existing;
  const current = { user: owner.user, sample: owner.sample.trim(), mapId: newMapId(owner.sample, new Date(), randomToken()) };
  saveCurrent(current);
  publish({ ...state, current, map: null, mapError: null, redo: null });
  return current.mapId;
}

/** The index the next run carries: the spot being redone, or a new one. */
export function nextIndex(s: SpotsState, map: SpotMap | null): number {
  return s.redo?.index ?? nextFreeIndex((map?.spots ?? []).map((spot) => spot.index));
}

/** The spot block of a four-point run that starts now. `map` is the map as
 *  just fetched, so the index is free even if a run ended a moment ago. */
export function spotForRun(owner: MapOwner, map: SpotMap | null): SpotRequest {
  const mapId = ensureMapId(owner);
  const index = nextIndex(state, map !== null && map.map_id === mapId ? map : null);
  const spot: SpotRequest = { map_id: mapId, index, label: spotLabel(state.label, index) };
  if (state.pending) {
    spot.x_mm = state.pending.x_mm;
    spot.y_mm = state.pending.y_mm;
  }
  return spot;
}

/** Forget the map: the next run starts a new one. */
export function startNewMap(): void {
  saveCurrent(null);
  publish({ ...state, current: null, map: null, mapError: null, lastSpot: null, warning: null, label: "", redo: null, pending: null });
}

export function setMap(map: SpotMap | null, error: string | null = null): void {
  publish({ ...state, map, mapError: error });
}

export function setSpotLabel(label: string): void {
  if (state.label !== label) publish({ ...state, label });
}

/** Redo takes the spot's name and, when it had one, its position: the same
 *  spot is the same place unless the operator moves it. Cancelling gives
 *  both back. */
export function setRedo(redo: SpotsState["redo"], position: SpotsState["pending"] = null): void {
  publish({ ...state, redo, label: redo ? redo.label : "", pending: redo ? (position ?? state.pending) : null });
}

/** Moving the spot answers a refusal: what the backend said about the old
 *  position is not about the new one. */
export function setPending(pending: SpotsState["pending"]): void {
  publish({ ...state, pending, warning: state.warning?.refused ? null : state.warning });
}

// --- events ---------------------------------------------------------------

export function applySpotRunStarted(event: Event<"run_started">): void {
  if (event.payload.mode !== "four_point") return;
  publish({ ...state, running: spotOf(event.payload.settings?.spot), lastSpot: null, warning: null });
}

export function applySpotRunEnded(): void {
  if (state.running !== null) publish({ ...state, running: null });
}

function spotOf(raw: unknown): SpotRequest | null {
  if (typeof raw !== "object" || raw === null) return null;
  const spot = raw as Partial<SpotRequest>;
  if (typeof spot.map_id !== "string" || typeof spot.index !== "number" || typeof spot.label !== "string") return null;
  return spot as SpotRequest;
}

export function applyGeometryWarning(event: Event<"geometry_warning">): void {
  publish({ ...state, warning: event.payload });
}

/** The run's file is closed and its spot is in the map: the name, the redo
 *  and the position have been used. A refused spot keeps them, so the
 *  operator can move it and try again. */
export function applySpotComplete(event: Event<"spot_complete">): void {
  publish({ ...state, lastSpot: event.payload, completions: state.completions + 1, label: "", redo: null, pending: null });
}

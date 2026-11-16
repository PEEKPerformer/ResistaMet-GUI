// How the operator likes the map shown. UI-only, kept across restarts.

import { useSyncExternalStore } from "react";
import type { LabelMode, Quantity } from "../lib/map/figure";

export interface MapViewState {
  /** null: open when there is something to see (an outline or a photo). */
  open: boolean | null;
  /** null: conductivity when the map has any, else sheet resistance. */
  quantity: Quantity | null;
  /** Open question 4 of the spots design: labels are off until asked for. */
  labels: LabelMode;
  /** Nearest-spot (Voronoi) fill. Not an interpolation, and off by default. */
  cells: boolean;
  /** Whether an exported figure includes the photograph. */
  exportPhoto: boolean;
}

const STORAGE_KEY = "resistamet.map.view";
const DEFAULTS: MapViewState = { open: null, quantity: null, labels: "none", cells: false, exportPhoto: true };

function load(): MapViewState {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored ? { ...DEFAULTS, ...(JSON.parse(stored) as Partial<MapViewState>) } : DEFAULTS;
  } catch {
    return DEFAULTS;
  }
}

let state = load();
const listeners = new Set<() => void>();

export function useMapView(): MapViewState {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => state,
    () => state,
  );
}

export function setMapView(patch: Partial<MapViewState>): void {
  state = { ...state, ...patch };
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // fine
  }
  for (const listener of listeners) listener();
}

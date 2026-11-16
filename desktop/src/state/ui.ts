// UI-only state: which mode is showing, who the operator is, what the sample
// is called, whether the log is open. Persisted to localStorage so a restart
// lands where the operator left off — the PySide6 app remembers the last user
// the same way.

import { useSyncExternalStore } from "react";
import type { Mode } from "../generated/settings";

export type View = Mode | "results";

export interface UiState {
  view: View;
  username: string | null;
  sampleName: string;
  logOpen: boolean;
  theme: "dark" | "light" | "system";
}

const STORAGE_KEY = "resistamet.ui";

function load(): UiState {
  const defaults: UiState = {
    view: "resistance",
    username: null,
    sampleName: "",
    logOpen: true,
    theme: "dark",
  };
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored ? { ...defaults, ...(JSON.parse(stored) as Partial<UiState>) } : defaults;
  } catch {
    return defaults;
  }
}

let state: UiState = load();
const listeners = new Set<() => void>();

function publish(next: UiState): void {
  state = next;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // storage full or unavailable; the session still works
  }
  for (const listener of listeners) listener();
}

export function useUi(): UiState {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => state,
    () => state,
  );
}

export function getUi(): UiState {
  return state;
}

export function setView(view: View): void {
  if (state.view !== view) publish({ ...state, view });
}

export function setUsername(username: string | null): void {
  if (state.username !== username) publish({ ...state, username });
}

export function setSampleName(sampleName: string): void {
  if (state.sampleName !== sampleName) publish({ ...state, sampleName });
}

export function setLogOpen(logOpen: boolean): void {
  if (state.logOpen !== logOpen) publish({ ...state, logOpen });
}

export function setTheme(theme: UiState["theme"]): void {
  if (state.theme !== theme) publish({ ...state, theme });
}

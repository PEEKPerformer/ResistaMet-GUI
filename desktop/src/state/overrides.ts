// The tab values: what the operator has dialled in per mode, on top of the
// profile. The PySide6 app keeps these in the widgets; here they are a small
// store so switching modes and back does not lose them, persisted to
// localStorage so a restart does not either.

import { useSyncExternalStore } from "react";
import type { Mode } from "../generated/settings";

type Overrides = Record<string, unknown>;
type All = Partial<Record<Mode, Overrides>>;

const STORAGE_KEY = "resistamet.overrides";

function load(): All {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored ? (JSON.parse(stored) as All) : {};
  } catch {
    return {};
  }
}

let all: All = load();
const listeners = new Set<() => void>();

function publish(next: All): void {
  all = next;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // fine
  }
  for (const listener of listeners) listener();
}

const EMPTY: Overrides = {};

export function useOverrides(mode: Mode): Overrides {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => all[mode] ?? EMPTY,
    () => all[mode] ?? EMPTY,
  );
}

export function setOverride(mode: Mode, key: string, value: unknown): void {
  const current = all[mode] ?? {};
  if (current[key] === value) return;
  publish({ ...all, [mode]: { ...current, [key]: value } });
}

/** Seed missing keys from a profile without clobbering what was dialled in. */
export function seedOverrides(mode: Mode, keys: string[], profileMeasurement: Overrides): void {
  const current = all[mode] ?? {};
  let changed = false;
  const next = { ...current };
  for (const key of keys) {
    if (!(key in next) && key in profileMeasurement) {
      next[key] = profileMeasurement[key];
      changed = true;
    }
  }
  if (changed) publish({ ...all, [mode]: next });
}

export function resetOverrides(mode: Mode): void {
  const next = { ...all };
  delete next[mode];
  publish(next);
}

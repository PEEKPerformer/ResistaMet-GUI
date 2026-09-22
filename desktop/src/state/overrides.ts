// The tab values: what an operator has dialled in per mode, on top of their
// profile. The PySide6 app keeps these in the widgets; here they are a small
// store so switching modes and back does not lose them, persisted to
// localStorage so a restart does not either.
//
// They belong to the operator. The lab PC is shared, and the next person to
// pick their name must get their own currents and compliance limits, not the
// ones left on the tab. Every function here acts on the operator selected in
// state/ui.ts unless told otherwise.

import { useSyncExternalStore } from "react";
import type { Mode } from "../generated/settings";
import {
  overridesOf,
  readStored,
  resetToProfile,
  seeded,
  withOverride,
  type AllOverrides,
  type Overrides,
} from "../lib/overridesModel";
import { getUi, useUi } from "./ui";

// The store under "resistamet.overrides" was keyed by mode alone. Its values
// are nobody's in particular, so it is not read.
const STORAGE_KEY = "resistamet.overrides.v2";

function load(): AllOverrides {
  try {
    return readStored(localStorage.getItem(STORAGE_KEY));
  } catch {
    return {};
  }
}

let all: AllOverrides = load();
const listeners = new Set<() => void>();

function publish(next: AllOverrides): void {
  if (next === all) return;
  all = next;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // fine
  }
  for (const listener of listeners) listener();
}

const EMPTY: Overrides = {};

const operator = (): string => getUi().username ?? "";

/** The selected operator's values for a mode; follows a change of operator. */
export function useOverrides(mode: Mode): Overrides {
  const user = useUi().username ?? "";
  const snapshot = () => overridesOf(all, user, mode) ?? EMPTY;
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    snapshot,
    snapshot,
  );
}

export function setOverride(mode: Mode, key: string, value: unknown): void {
  publish(withOverride(all, operator(), mode, key, value));
}

/** Seed missing keys from a profile without clobbering what was dialled in.
 *
 *  `username` is whose profile it is. A caller that fetched the profile
 *  should pass the name it asked for: the operator may have changed while
 *  the request was out, and one operator's profile must not seed another's
 *  tab. */
export function seedOverrides(
  mode: Mode,
  keys: string[],
  profileMeasurement: Overrides,
  username: string = operator(),
): void {
  publish(seeded(all, username, mode, keys, profileMeasurement));
}

/** Reset to profile: drop what was dialled in for this mode and take the
 *  profile's values, which is also how a profile edit reaches a tab that was
 *  seeded before it. */
export function resetOverrides(
  mode: Mode,
  keys: string[],
  profileMeasurement: Overrides,
  username: string = operator(),
): void {
  publish(resetToProfile(all, username, mode, keys, profileMeasurement));
}

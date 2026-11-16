// The tab values as data: what each operator has dialled in for each mode, on
// top of their profile. state/overrides.ts holds one of these and persists it.

export type Overrides = Record<string, unknown>;

/** Operator, then mode. One operator's test current is not another's. */
export type AllOverrides = Record<string, Record<string, Overrides>>;

export function overridesOf(all: AllOverrides, user: string, mode: string): Overrides | undefined {
  return all[user]?.[mode];
}

function withMode(all: AllOverrides, user: string, mode: string, values: Overrides): AllOverrides {
  return { ...all, [user]: { ...(all[user] ?? {}), [mode]: values } };
}

/** One value changed. The same object back when it already had that value. */
export function withOverride(all: AllOverrides, user: string, mode: string, key: string, value: unknown): AllOverrides {
  const current = overridesOf(all, user, mode) ?? {};
  if (key in current && current[key] === value) return all;
  return withMode(all, user, mode, { ...current, [key]: value });
}

/** Keys the tab does not have yet, filled from the operator's profile. What
 *  was dialled in stays. The same object back when nothing was missing. */
export function seeded(all: AllOverrides, user: string, mode: string, keys: string[], profile: Overrides): AllOverrides {
  const current = overridesOf(all, user, mode) ?? {};
  const missing = keys.filter((key) => !(key in current) && key in profile);
  if (missing.length === 0) return all;
  const next = { ...current };
  for (const key of missing) next[key] = profile[key];
  return withMode(all, user, mode, next);
}

/** The tab put back to the operator's profile: everything dialled in for
 *  this mode is dropped and the profile's values take its place. */
export function resetToProfile(all: AllOverrides, user: string, mode: string, keys: string[], profile: Overrides): AllOverrides {
  const values: Overrides = {};
  for (const key of keys) if (key in profile) values[key] = profile[key];
  return withMode(all, user, mode, values);
}

/** What was read from storage, kept only if it has the operator-then-mode
 *  shape. Anything else, such as the older store keyed by mode alone whose
 *  values belong to nobody in particular, is dropped. */
export function readStored(text: string | null): AllOverrides {
  if (!text) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return {};
  }
  const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
  if (!isRecord(parsed)) return {};
  const all: AllOverrides = {};
  for (const [user, modes] of Object.entries(parsed)) {
    if (!isRecord(modes)) continue;
    const kept: Record<string, Overrides> = {};
    for (const [mode, values] of Object.entries(modes)) if (isRecord(values)) kept[mode] = values;
    all[user] = kept;
  }
  return all;
}

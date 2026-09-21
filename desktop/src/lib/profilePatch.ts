// What the Settings dialog sends on Save.

type Sections = Record<string, Record<string, unknown>>;

/** Keys of `measurement` that describe this computer, not the operator. The
 *  Instrument section stores them itself, after the instrument has answered;
 *  a profile Save never carries them. */
export const MACHINE_LOCAL_KEYS: readonly string[] = ["gpib_address", "visa_library", "gpib_interface"];

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** The keys of `draft` that differ from the profile as loaded, by section.
 *  Sections with nothing to send are left out. */
export function profilePatch(loaded: Sections, draft: Sections): Sections {
  const patch: Sections = {};
  for (const [section, values] of Object.entries(draft)) {
    // A reply may carry non-section keys (a null, a list); only objects hold settings.
    if (values === null || typeof values !== "object" || Array.isArray(values)) continue;
    const before = loaded[section] ?? {};
    const changed: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(values)) {
      if (section === "measurement" && MACHINE_LOCAL_KEYS.includes(key)) continue;
      if (!(key in before) || !same(before[key], value)) changed[key] = value;
    }
    if (Object.keys(changed).length > 0) patch[section] = changed;
  }
  return patch;
}

/** The draft with this computer's keys taken from `stored`, for after the
 *  Instrument section has saved them: unsaved edits elsewhere are kept. */
export function withMachineLocal(draft: Sections, stored: Sections): Sections {
  const measurement = { ...(draft.measurement ?? {}) };
  for (const key of MACHINE_LOCAL_KEYS) {
    const value = stored.measurement?.[key];
    if (value !== undefined) measurement[key] = value;
  }
  return { ...draft, measurement };
}

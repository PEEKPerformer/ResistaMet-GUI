// Which map a four-point run belongs to, and which spot of it.
//
// The same rule the PySide6 window follows
// (`resistamet_gui/schema/map_session.py`): a map is the runs one operator
// makes on one sample name between two resets. The id is minted when a run
// needs it, not while the name is being typed, so it carries the time of the
// map's first run and the name that run was filed under.
//
// Pure: the clock and the random token are arguments.

/** Bounds of the backend's SpotRequest. */
export const MAX_MAP_ID_LENGTH = 64;
export const MAX_LABEL_LENGTH = 80;
export const MAX_INDEX = 9999;

/** The first spot of a map, as in the PySide6 window's counter. */
export const FIRST_INDEX = 1;

export const MAP_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

export function autoLabel(index: number): string {
  return `Spot ${index}`;
}

/** `text` as a label the backend accepts, or the automatic name.
 *
 *  The label goes into a `# key: value` header line: control characters and
 *  the Unicode line separators become spaces, runs of whitespace collapse,
 *  and the result is cut to the model's length. */
export function spotLabel(text: string, index: number): string {
  const cleaned = text
    .replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g, " ")
    .split(/\s+/)
    .filter(Boolean)
    .join(" ")
    .slice(0, MAX_LABEL_LENGTH)
    .trim();
  return cleaned || autoLabel(index);
}

/** The index after the highest one in use. A redone spot keeps its own. */
export function nextFreeIndex(indices: Iterable<number>): number {
  let highest = FIRST_INDEX - 1;
  for (const index of indices) if (Number.isInteger(index) && index > highest) highest = index;
  return Math.min(highest + 1, MAX_INDEX);
}

function pad(value: number, width = 2): string {
  return String(value).padStart(width, "0");
}

/** Local time as the operator reads the clock: YYYYMMDD-HHMMSS. */
export function mapStamp(now: Date): string {
  return (
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  );
}

/** Four hex digits. Two maps started in the same second must not share an id. */
export function randomToken(): string {
  const bytes = new Uint8Array(2);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** A fresh map id: `<YYYYMMDD-HHMMSS>_<sample>_<token>`.
 *
 *  The sample name is reduced to the characters a map id allows and cut so
 *  the whole id fits; a name with none of them left is `sample`. */
export function newMapId(sampleName: string, now: Date, token: string): string {
  const stamp = mapStamp(now);
  const safeToken = /^[A-Za-z0-9]{1,8}$/.test(token) ? token : "0000";
  const room = MAX_MAP_ID_LENGTH - stamp.length - safeToken.length - 2;
  const trim = (s: string) => s.replace(/^[_-]+|[_-]+$/g, "");
  const name = trim(trim(sampleName.replace(/[^A-Za-z0-9_-]+/g, "_")).slice(0, room)) || "sample";
  return `${stamp}_${name}_${safeToken}`;
}

/** Who and what a map was minted for. */
export interface MapOwner {
  user: string;
  sample: string;
}

export function sameOwner(a: MapOwner, b: MapOwner): boolean {
  return a.user === b.user && a.sample.trim() === b.sample.trim();
}

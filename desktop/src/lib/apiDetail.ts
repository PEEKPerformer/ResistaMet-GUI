// The `detail` of a refused request, as text an operator can read.
//
// The backend's own refusals are strings. Request validation (a field of the
// wrong type, a sample name that is too long, an unknown field) is FastAPI's,
// and its detail is a list of {loc, msg, ...}; shown raw that was
// "[object Object]" or a screen of JSON. A few routes answer with an object
// that a caller parses (the profile PATCH and its issues): that stays JSON.

interface ValidationEntry {
  loc?: unknown;
  msg?: unknown;
}

/** Where the value came from is not the operator's concern; the field is. */
const LOCATION_ROOTS = new Set(["body", "query", "path", "header"]);

function describeEntry(entry: unknown): string {
  if (typeof entry === "string") return entry;
  if (typeof entry !== "object" || entry === null) return String(entry);
  const { loc, msg } = entry as ValidationEntry;
  const message = typeof msg === "string" ? msg : JSON.stringify(entry);
  const path = Array.isArray(loc)
    ? loc.filter((part, i) => !(i === 0 && LOCATION_ROOTS.has(String(part)))).join(".")
    : "";
  return path ? `${path}: ${message}` : message;
}

export function describeDetail(detail: unknown, fallback: string): string {
  if (detail === undefined || detail === null) return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(describeEntry).join("; ") || fallback;
  return JSON.stringify(detail);
}

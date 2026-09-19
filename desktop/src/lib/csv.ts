// Reading a ResistaMet CSV back: the #-prefixed metadata header, the column
// row, the numeric rows, and the #-prefixed block a finished run appends.
// Enough for a preview, not a general CSV parser.

export interface ParsedCsv {
  metadata: Record<string, string>;
  /** What the run wrote after its rows: when and why it ended, four-point
   *  spot statistics, the van der Pauw result. Empty for a run that was cut
   *  off before it could finish the file. */
  footer: Record<string, string>;
  columns: string[];
  /** Column -> numeric values (NaN where the cell was not a number). */
  data: Record<string, number[]>;
  /** Non-numeric columns (compliance, event) kept as text. */
  text: Record<string, string[]>;
  rows: number;
}

export function parseResistametCsv(source: string): ParsedCsv {
  const metadata: Record<string, string> = {};
  const footer: Record<string, string> = {};
  const lines = source.split(/\r?\n/);
  let i = 0;
  for (; i < lines.length; i++) {
    const line = lines[i] ?? "";
    if (!line.startsWith("#")) break;
    readMetadataLine(line, metadata);
  }
  while (i < lines.length && (lines[i] ?? "").trim() === "") i++;
  const columns = splitRow(lines[i] ?? "");
  i++;

  const data: Record<string, number[]> = {};
  const text: Record<string, string[]> = {};
  const numericColumn = columns.map(() => true);
  const hasValue = columns.map(() => false);
  const cells: string[][] = [];
  for (; i < lines.length; i++) {
    const line = lines[i] ?? "";
    if (line.trim() === "") continue;
    if (line.startsWith("#")) {
      readMetadataLine(line, footer);
      continue;
    }
    const row = splitRow(line);
    cells.push(row);
    row.forEach((cell, c) => {
      if (cell === "") return;
      hasValue[c] = true;
      if (numericColumn[c] && cellNumber(cell) === null) numericColumn[c] = false;
    });
  }
  columns.forEach((name, c) => {
    // A column with no value at all (no event was marked) is not a quantity.
    if (numericColumn[c] && hasValue[c]) data[name] = cells.map((row) => cellNumber(row[c] ?? "") ?? NaN);
    else text[name] = cells.map((row) => row[c] ?? "");
  });
  return { metadata, footer, columns, data, text, rows: cells.length };
}

/** A cell as a number, or null when it is text. The backend writes Python's
 *  "nan" and "inf" for a quantity it could not compute, such as conductivity
 *  with no thickness given; those are numbers, and one of them must not turn
 *  the whole column into text. */
function cellNumber(cell: string): number | null {
  const special = /^([-+]?)(nan|inf|infinity)$/i.exec(cell);
  if (special) return special[2]!.toLowerCase() === "nan" ? NaN : special[1] === "-" ? -Infinity : Infinity;
  const value = Number(cell);
  return cell === "" || Number.isNaN(value) ? null : value;
}

/** "# key: value" into `into`. A line without a key, such as the marker that
 *  opens the end block, carries nothing. */
function readMetadataLine(line: string, into: Record<string, string>): void {
  const body = line.slice(1).trim();
  const colon = body.indexOf(":");
  if (colon > 0) into[body.slice(0, colon).trim()] = body.slice(colon + 1).trim();
}

function splitRow(line: string): string[] {
  // Fields are never quoted in these files except the event label, which may
  // hold a semicolon-joined list; a minimal quote-aware split covers it.
  const out: string[] = [];
  let current = "";
  let quoted = false;
  for (const ch of line) {
    if (ch === '"') quoted = !quoted;
    else if (ch === "," && !quoted) {
      out.push(current);
      current = "";
    } else current += ch;
  }
  out.push(current);
  return out.map((s) => s.trim());
}

/** How a sweep file is drawn: current against voltage, one trace per leg. */
export interface SweepPreview {
  x: string;
  y: string;
  /** Row ranges, `to` exclusive: one for a one-way sweep, two for up-down. */
  legs: { from: number; to: number }[];
}

/** The I-V axes of a sweep file, or null for any other file. A sweep file
 *  has no elapsed time; its rows are the sweep's points in order. */
export function sweepPreview(parsed: ParsedCsv): SweepPreview | null {
  const voltage = parsed.data["V_source"];
  const current = parsed.data["I_meas"];
  if (!voltage || !current) return null;
  const sourced = parsed.metadata["params.source_function"] === "current" ? current : voltage;
  return { x: "V_source", y: "I_meas", legs: sweepLegs(sourced) };
}

/** Split a swept quantity where it turns round. The leg that comes back
 *  starts at the first point after the last step forward, so a turning value
 *  that is measured twice gives one point to each leg. */
export function sweepLegs(sourced: number[]): { from: number; to: number }[] {
  const legs: { from: number; to: number }[] = [];
  let from = 0;
  let heading = 0;
  let lastStep = 0; // index reached by the last step in the leg's direction
  for (let i = 1; i < sourced.length; i++) {
    const step = Math.sign(sourced[i]! - sourced[i - 1]!);
    if (step === 0 || Number.isNaN(step)) continue;
    if (heading !== 0 && step !== heading) {
      legs.push({ from, to: lastStep + 1 });
      from = lastStep + 1;
    }
    heading = step;
    lastStep = i;
  }
  if (sourced.length > 0) legs.push({ from, to: sourced.length });
  return legs;
}

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
      if (numericColumn[c] && cell !== "" && Number.isNaN(Number(cell))) numericColumn[c] = false;
    });
  }
  columns.forEach((name, c) => {
    if (numericColumn[c]) data[name] = cells.map((row) => (row[c] === "" || row[c] === undefined ? NaN : Number(row[c])));
    else text[name] = cells.map((row) => row[c] ?? "");
  });
  return { metadata, footer, columns, data, text, rows: cells.length };
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

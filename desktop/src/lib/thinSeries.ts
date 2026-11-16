// Keeping a long run's live series within what a webview can hold.
//
// The plot's arrays grew by one row per sample for as long as the run went
// on: a week at 100 Hz is 60 million rows per column. The file the backend
// writes is the record; the screen needs the recent samples as they came and
// the shape of everything before them.
//
// So the arrays are two regions. The tail is the newest samples, untouched.
// The history before it is thinned: every four rows become two, holding each
// column's minimum and maximum of the four in the order they occurred, so a
// spike or a dropout stays on the plot however long ago it was. When the
// history outgrows its limit the whole of it is thinned again, which keeps
// its resolution uniform over the run rather than fading with age.

export interface Columns {
  t: number[];
  values: Record<string, number[]>;
  compliance: string[];
}

export interface Thinning {
  /** Rows at the front of the arrays that belong to the history. */
  historyRows: number;
  /** How many times the history has been halved: one row stands for
   *  2^level samples. 0 = nothing has been thinned yet. */
  level: number;
}

export interface ThinLimits {
  /** The history is halved when it has more rows than this. */
  historyMax: number;
  /** Raw samples always kept at the end. */
  tailMin: number;
  /** Samples moved from the tail to the history at a time. A power of two,
   *  so it can be halved down to the history's level. */
  chunk: number;
}

/** About 230 000 rows per column at most: some 16 minutes of tail at 100 Hz,
 *  more than a day at 1 Hz. */
export const LIVE_LIMITS: ThinLimits = { historyMax: 100_000, tailMin: 3 * 32_768, chunk: 32_768 };

export function newThinning(): Thinning {
  return { historyRows: 0, level: 0 };
}

/** Call after appending. Mutates the columns in place; returns the new
 *  bookkeeping (the same object when there was nothing to do). */
export function thin(columns: Columns, state: Thinning, limits: ThinLimits = LIVE_LIMITS): Thinning {
  let { historyRows, level } = state;
  if (columns.t.length - historyRows < limits.tailMin + limits.chunk) return state;
  // Move the oldest chunk of the tail into the history, at its level.
  let end = historyRows + limits.chunk;
  for (let i = 0; i < level; i++) end = halveRows(columns, historyRows, end);
  historyRows = end;
  if (historyRows > limits.historyMax) {
    // A remainder of up to three rows stays as it is: still an envelope.
    const whole = historyRows - (historyRows % 4);
    historyRows -= whole - halveRows(columns, 0, whole);
    level += 1;
  }
  return { historyRows, level };
}

/** Replace rows [from, to) by half as many; returns the new `to`. The span
 *  is a multiple of four rows. */
export function halveRows(columns: Columns, from: number, to: number): number {
  const rows = to - from;
  if (rows < 4) return to;
  const t: number[] = [];
  const compliance: string[] = [];
  for (let b = from; b + 4 <= to; b += 4) {
    t.push(columns.t[b] as number, columns.t[b + 2] as number);
    let worst = "OK";
    for (let i = b; i < b + 4; i++) {
      const c = columns.compliance[i] ?? "OK";
      if (c !== "OK") {
        worst = c;
        break;
      }
    }
    compliance.push(worst, worst);
  }
  columns.t.splice(from, rows);
  insert(columns.t, from, t);
  columns.compliance.splice(from, rows);
  insert(columns.compliance, from, compliance);
  for (const column of Object.values(columns.values)) {
    const out: number[] = [];
    for (let b = from; b + 4 <= to; b += 4) {
      let lo = -1;
      let hi = -1;
      for (let i = b; i < b + 4; i++) {
        const v = column[i] as number;
        if (Number.isNaN(v)) continue;
        if (lo < 0 || v < (column[lo] as number)) lo = i;
        if (hi < 0 || v > (column[hi] as number)) hi = i;
      }
      if (lo < 0) out.push(NaN, NaN);
      else if (lo <= hi) out.push(column[lo] as number, column[hi] as number);
      else out.push(column[hi] as number, column[lo] as number);
    }
    column.splice(from, rows);
    insert(column, from, out);
  }
  return from + rows / 2;
}

/** splice(...items) spreads onto the stack, which overflows for long arrays. */
function insert<T>(target: T[], at: number, items: T[]): void {
  const moved = target.splice(at);
  for (const item of items) target.push(item);
  for (const item of moved) target.push(item);
}

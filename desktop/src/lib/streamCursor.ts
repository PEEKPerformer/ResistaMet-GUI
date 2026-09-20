// Where the event stream is, and what to make of the next event.
//
// `seq` counts within a run and run ids count within a backend process, so
// (run id, seq) says where the client is only while the process lives. A
// restarted backend hands out run-1 again; its events look like replays of
// the old run-1 unless something tells them apart. Two things can:
// run_started is emitted once per run, and a socket that resumed from
// (run, n) is never sent that run's events up to n again.

export interface Cursor {
  runId: string | null;
  lastSeq: number;
}

/** The position the open socket resumed from; null for the HTTP history. */
export type ResumePoint = Cursor | null;

export type Verdict =
  /** Next in order: deliver it. */
  | "deliver"
  /** Seen already (a resume overlaps what was delivered): drop it. */
  | "replay"
  /** Same run id, but it cannot be the run the cursor is on: the backend
   *  restarted and is counting from 1 again. */
  | "restarted"
  /** First sight of a newer run, and not from its beginning. */
  | "missed-head";

interface Positioned {
  type: string;
  run_id?: string | null;
  seq: number;
}

export function judge(cursor: Cursor, event: Positioned, resume: ResumePoint): Verdict {
  const runId = event.run_id ?? null;
  if (runId !== cursor.runId) return runId !== null && event.seq > 1 ? "missed-head" : "deliver";
  if (event.seq > cursor.lastSeq) return "deliver";
  if (event.type === "run_started") return "restarted";
  if (resume !== null && resume.runId === runId && event.seq <= resume.lastSeq) return "restarted";
  return "replay";
}

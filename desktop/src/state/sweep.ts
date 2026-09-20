// The I-V sweep's results: one or two segments of (V, I) the instrument
// returned in bulk. Small and low-rate, so a plain React-visible store.

import { useSyncExternalStore } from "react";
import type { Event } from "../generated/events";
import { fitSweep, type Sourced, type SweepFit } from "../lib/sweepFit";

export interface SweepSegment {
  direction: "forward" | "reverse";
  voltages: number[];
  currents: number[];
  compliance: string[];
}

export interface SweepResult {
  runId: string | null;
  segments: SweepSegment[];
}

let result: SweepResult = { runId: null, segments: [] };
const listeners = new Set<() => void>();

function publish(next: SweepResult): void {
  result = next;
  for (const listener of listeners) listener();
}

export function useSweep(): SweepResult {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => result,
    () => result,
  );
}

export function resetSweep(runId: string | null): void {
  publish({ runId, segments: [] });
}

export function applySweepSegment(event: Event<"sweep_segment">): void {
  const { payload } = event;
  // A replayed segment from an earlier run must not join this run's curve.
  if (result.runId !== null && event.run_id && event.run_id !== result.runId) return;
  publish({
    runId: event.run_id ?? null,
    segments: [
      ...result.segments,
      {
        direction: payload.direction ?? "forward",
        voltages: payload.voltages ?? [],
        currents: payload.currents ?? [],
        compliance: payload.compliance ?? [],
      },
    ],
  });
}

/** The sweep's resistance: each leg fitted on its own, the measured quantity
 *  on the sourced one, points in compliance left out (lib/sweepFit.ts).
 *
 *  `r` and `r2` are the single figure for a view that shows one: the pooled
 *  fit, which exists only when the legs agree. When they do not they are NaN,
 *  and `fit.legs` has the two resistances to show instead. `n` is the number
 *  of points behind `r`.
 *
 *  `sourced` is the run's sweep_source. It decides which way the regression
 *  runs, and nothing in a segment says which it was. */
export function fitResistance(
  segments: SweepSegment[],
  sourced: Sourced = "voltage",
): { r: number; r2: number; n: number; fit: SweepFit } {
  const fit = fitSweep(segments, sourced);
  const pooled = fit.pooled;
  return { r: pooled?.r ?? NaN, r2: pooled?.r2 ?? NaN, n: pooled?.n ?? 0, fit };
}

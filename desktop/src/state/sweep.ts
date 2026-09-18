// The I-V sweep's results: one or two segments of (V, I) the instrument
// returned in bulk. Small and low-rate, so a plain React-visible store.

import { useSyncExternalStore } from "react";
import type { Event } from "../generated/events";

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

/** Least-squares slope dI/dV over all points, as a resistance. NaN when
 *  there are not two distinct voltages. */
export function fitResistance(segments: SweepSegment[]): { r: number; r2: number } {
  const xs: number[] = [];
  const ys: number[] = [];
  for (const s of segments) {
    s.voltages.forEach((v, i) => {
      const c = s.currents[i];
      if (Number.isFinite(v) && c !== undefined && Number.isFinite(c)) {
        xs.push(v);
        ys.push(c);
      }
    });
  }
  const n = xs.length;
  if (n < 2) return { r: NaN, r2: NaN };
  const mx = xs.reduce((a, b) => a + b, 0) / n;
  const my = ys.reduce((a, b) => a + b, 0) / n;
  let sxy = 0;
  let sxx = 0;
  let syy = 0;
  for (let i = 0; i < n; i++) {
    const dx = xs[i]! - mx;
    const dy = ys[i]! - my;
    sxy += dx * dy;
    sxx += dx * dx;
    syy += dy * dy;
  }
  if (sxx === 0) return { r: NaN, r2: NaN };
  const slope = sxy / sxx; // dI/dV in siemens
  const r2 = syy === 0 ? 1 : (sxy * sxy) / (sxx * syy);
  return { r: slope === 0 ? Infinity : 1 / slope, r2 };
}

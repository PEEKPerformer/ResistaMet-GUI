// The live sample buffers the plots draw from.
//
// Deliberately outside React. At 50 Hz a re-render per sample would spend the
// UI's whole budget on reconciliation; instead samples are appended to plain
// arrays here and the plot component pulls them on its own frame timer.
// Anything a panel needs at low rate — the latest reading, the count — is
// exposed through a small versioned snapshot that only React-visible changes
// bump.

import { useSyncExternalStore } from "react";
import type { Event } from "../generated/events";

export interface SampleSeries {
  /** Which mode produced these samples; a view for another mode shows none. */
  mode: string | null;
  runId: string | null;
  /** Elapsed seconds since run start, one per sample. */
  t: number[];
  /** Per-key value arrays, same length as t. Missing values are NaN. */
  values: Record<string, number[]>;
  compliance: string[];
  marks: { t: number; label: string }[];
}

export interface LatestSample {
  mode: string | null;
  elapsedS: number;
  values: Record<string, unknown>;
  compliance: string;
  derived: Record<string, unknown> | null;
  count: number;
}

let series: SampleSeries = { mode: null, runId: null, t: [], values: {}, compliance: [], marks: [] };
let latest: LatestSample | null = null;
let version = 0;

const listeners = new Set<() => void>();

function notify(): void {
  version += 1;
  for (const listener of listeners) listener();
}

/** Plots read this directly; it is mutated in place between renders. */
export function getSeries(): SampleSeries {
  return series;
}

export function getLatest(): LatestSample | null {
  return latest;
}

export function resetSamples(mode: string | null = null, runId: string | null = null): void {
  series = { mode, runId, t: [], values: {}, compliance: [], marks: [] };
  latest = null;
  notify();
}

export function applySample(event: Event<"sample">): void {
  const { payload } = event;
  const n = series.t.length;
  series.t.push(payload.elapsed_s);
  series.compliance.push(payload.compliance ?? "OK");
  const values: Record<string, unknown> = { ...(payload.values ?? {}) };
  // Derived 4PP quantities join the columns under a derived_ prefix so the
  // plot and the statistics can treat them like any other trace.
  if (payload.derived) {
    for (const [key, raw] of Object.entries(payload.derived)) {
      if (typeof raw === "number") values[`derived_${key}`] = raw;
    }
  }
  for (const [key, raw] of Object.entries(values)) {
    const value = typeof raw === "number" ? raw : NaN;
    let column = series.values[key];
    if (!column) {
      // A key that appears late (aux fault text becoming numeric, say) gets
      // NaN for the samples before it so every column stays aligned with t.
      column = new Array<number>(n).fill(NaN);
      series.values[key] = column;
    }
    column.push(value);
  }
  for (const [key, column] of Object.entries(series.values)) {
    if (!(key in values)) column.push(NaN);
  }
  if (payload.event_marker) {
    series.marks.push({ t: payload.elapsed_s, label: payload.event_marker });
  }
  latest = {
    mode: series.mode,
    elapsedS: payload.elapsed_s,
    values,
    compliance: payload.compliance ?? "OK",
    derived: (payload.derived as Record<string, unknown> | null | undefined) ?? null,
    count: n + 1,
  };
  // Panels showing the readout are throttled by their own timers; this
  // notification is cheap (a counter bump) and lets them know there is news.
  notify();
}

/** Low-rate view for readouts: re-renders when a sample arrives, at most as
 *  often as React schedules. Components that must not render at 50 Hz should
 *  sample this on a timer instead. */
export function useLatestSample(): LatestSample | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => latest,
    () => latest,
  );
}

export function getSamplesVersion(): number {
  return version;
}

// Four-point probe spots: the operator measures several places on a sample
// and wants the per-spot sheet resistance side by side. The backend's spot
// model is a later step; until then spots are summarised here from the
// samples of the run that was open when Save spot was pressed.

import { useSyncExternalStore } from "react";

export interface Spot {
  id: number;
  name: string;
  sample: string;
  n: number;
  rsMean: number;
  rsSd: number;
  rhoMean: number;
  sigmaMean: number;
  savedAt: number;
}

let spots: Spot[] = [];
let nextId = 1;
const listeners = new Set<() => void>();

function publish(next: Spot[]): void {
  spots = next;
  for (const listener of listeners) listener();
}

export function useSpots(): Spot[] {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => spots,
    () => spots,
  );
}

export function addSpot(spot: Omit<Spot, "id" | "savedAt">): void {
  publish([...spots, { ...spot, id: nextId++, savedAt: Date.now() }]);
}

export function removeSpot(id: number): void {
  publish(spots.filter((s) => s.id !== id));
}

export function clearSpots(): void {
  publish([]);
}

/** Mean and sample standard deviation of the finite values. */
export function meanSd(values: number[]): { mean: number; sd: number; n: number } {
  const finite = values.filter((v) => Number.isFinite(v));
  const n = finite.length;
  if (n === 0) return { mean: NaN, sd: NaN, n: 0 };
  const mean = finite.reduce((a, b) => a + b, 0) / n;
  if (n < 2) return { mean, sd: NaN, n };
  const variance = finite.reduce((a, v) => a + (v - mean) ** 2, 0) / (n - 1);
  return { mean, sd: Math.sqrt(variance), n };
}

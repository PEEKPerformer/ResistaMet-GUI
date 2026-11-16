// The van der Pauw run as it unfolds: each geometry's readings as the operator
// works through the four wirings, then the F76 result.

import { useSyncExternalStore } from "react";
import type { Event, VdpGeometryCompletePayload, VdpResultPayload } from "../generated/events";

export interface VdpState {
  runId: string | null;
  geometries: VdpGeometryCompletePayload[];
  result: VdpResultPayload | null;
}

let state: VdpState = { runId: null, geometries: [], result: null };
const listeners = new Set<() => void>();

function publish(next: VdpState): void {
  state = next;
  for (const listener of listeners) listener();
}

export function useVdp(): VdpState {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => state,
    () => state,
  );
}

export function resetVdp(runId: string | null): void {
  publish({ runId, geometries: [], result: null });
}

/** Events belong to the run that run_started announced. A replay after a
 *  reconnect can carry the previous run's geometries and result; those must
 *  not land on top of the run in progress. (Bench: an aborted run showed the
 *  previous run's complete result badged Done.) */
function forThisRun(runId: string | null | undefined): boolean {
  return state.runId === null || runId === undefined || runId === null || runId === state.runId;
}

export function applyVdpGeometry(event: Event<"vdp_geometry_complete">): void {
  if (!forThisRun(event.run_id)) return;
  publish({ ...state, runId: event.run_id ?? state.runId, geometries: [...state.geometries, event.payload] });
}

export function applyVdpResult(event: Event<"vdp_result">): void {
  if (!forThisRun(event.run_id)) return;
  publish({ ...state, runId: event.run_id ?? state.runId, result: event.payload });
}

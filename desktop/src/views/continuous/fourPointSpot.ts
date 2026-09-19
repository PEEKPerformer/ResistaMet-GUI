// Between the four-point view and the backend's map: fetch it, keep the
// store's copy current, and build the spot a run starts with.

import { useEffect } from "react";
import type { ApiClient } from "../../lib/api";
import { ApiError } from "../../lib/api";
import type { SpotMap } from "../../generated/maps";
import type { SpotRequest } from "../../generated/settings";
import type { MapOwner } from "../../lib/map/mapId";
import { outlineFromSettings, preflight, type Preflight } from "../../lib/map/geometry";
import { activeMapId, getSpots, setMap, spotForRun, useSpots } from "../../state/spots";

/** GET the map into the store. A 404 is a map whose first run has not
 *  finished (or was refused): no spots yet, not an error. */
export async function fetchMap(api: ApiClient, user: string, mapId: string): Promise<SpotMap | null> {
  try {
    const map = await api.map(mapId, user);
    if (getSpots().current?.mapId === mapId) setMap(map);
    return map;
  } catch (e) {
    const missing = e instanceof ApiError && e.status === 404;
    if (getSpots().current?.mapId === mapId) setMap(null, missing ? null : e instanceof Error ? e.message : String(e));
    return null;
  }
}

/** Refetch when the map changes identity and whenever a spot completes. */
export function useMapSync(api: ApiClient, owner: MapOwner | null): void {
  const spots = useSpots();
  const mapId = activeMapId(spots, owner);
  const user = owner?.user ?? null;
  const { completions } = spots;
  useEffect(() => {
    if (mapId === null || user === null) return;
    void fetchMap(api, user, mapId);
  }, [api, mapId, user, completions]);
}

/** The spot of a run that starts now. The map is fetched first, so the index
 *  is free even when the previous run ended a moment ago. */
export async function prepareSpot(api: ApiClient, owner: MapOwner): Promise<SpotRequest> {
  const mapId = activeMapId(getSpots(), owner);
  const map = mapId === null ? null : await fetchMap(api, owner.user, mapId);
  return spotForRun(owner, map);
}

/** What the client can say about a position before Start: exact for a tip
 *  off the sample, a guess for near an edge. null without a position or
 *  without an outline to hold it against. */
export function preflightFor(measurement: Record<string, unknown>, pending: { x_mm: number; y_mm: number } | null): Preflight | null {
  if (pending === null) return null;
  const outline = outlineFromSettings(measurement);
  if (outline === null) return null;
  const spacingCm = typeof measurement.fpp_spacing_cm === "number" ? measurement.fpp_spacing_cm : 0.1016;
  const angleDeg = typeof measurement.fpp_array_angle_deg === "number" ? measurement.fpp_array_angle_deg : 0;
  return preflight(outline, { x: pending.x_mm, y: pending.y_mm }, angleDeg, spacingCm * 10);
}

/** "0.5 s beyond the edge", in the backend's words for the same thing. */
export function describeClearance(p: Preflight): string {
  if (p.clearanceS === null) return "";
  return p.state === "off"
    ? `a probe tip is ${Math.abs(p.clearanceS).toFixed(2)} s beyond the edge`
    : `nearest tip ${p.clearanceS.toFixed(1)} s from the edge`;
}

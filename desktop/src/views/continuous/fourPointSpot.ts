// Between the four-point view and the backend's map: fetch it, keep the
// store's copy current, and build the spot a run starts with.

import { useEffect } from "react";
import type { ApiClient } from "../../lib/api";
import { ApiError } from "../../lib/api";
import type { SpotMap } from "../../generated/maps";
import type { SpotRequest } from "../../generated/settings";
import type { MapOwner } from "../../lib/map/mapId";
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

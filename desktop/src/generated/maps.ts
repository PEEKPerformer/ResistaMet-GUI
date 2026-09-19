/* eslint-disable */
// GENERATED from contracts/*.schema.json by desktop/scripts/generate-types.mjs.
// Do not edit. Regenerate with `npm run gen:types`.

/**
 * A map: its spots in index order and the spread between them.
 */
export interface SpotMap {
  map_id: string;
  rho: InterSpotStats;
  rs: InterSpotStats;
  sigma: InterSpotStats;
  skipped?: SkippedRun[];
  spots?: MapSpot[];
}
/**
 * The spread of one quantity between the spots of a map.
 *
 * Over the spots' means, each spot counting once. ``sd`` is the sample
 * standard deviation and needs two spots; ``rsd_pct`` is ``sd / |mean|``.
 */
export interface InterSpotStats {
  mean?: number | null;
  n: number;
  rsd_pct?: number | null;
  sd?: number | null;
}
/**
 * A run that names this map but cannot stand for a spot.
 */
export interface SkippedRun {
  file: string;
  reason: string;
}
/**
 * One spot of a map: the newest run with that index.
 */
export interface MapSpot {
  angle_deg?: number | null;
  edge_clearance_s?: number | null;
  file: string;
  index: number;
  label: string;
  relative_error?: number | null;
  sample?: string | null;
  started_at?: string | null;
  stats: SpotStats;
  superseded?: string[];
  x_mm?: number | null;
  y_mm?: number | null;
}
/**
 * The statistics block of a four-point run, as its file footer has it.
 *
 * ``n`` samples entered the statistics; ``n_excluded`` were in compliance
 * and left out, because they record a bound rather than a measurement.
 */
export interface SpotStats {
  n: number;
  n_excluded?: number;
  rho: QuantityStats;
  rs: QuantityStats;
  sigma: QuantityStats;
}
/**
 * One derived quantity over a spot's samples (``session.spot_stats``).
 *
 * ``n`` counts the finite values. Every other field is null on the wire when
 * it does not exist: all of them with no finite value, ``sd`` and
 * ``rsd_pct`` with fewer than two.
 */
export interface QuantityStats {
  mean?: number | null;
  n: number;
  rsd_pct?: number | null;
  sd?: number | null;
  u_inst?: number | null;
  u_stat?: number | null;
  u_total?: number | null;
}

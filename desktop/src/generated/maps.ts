/* eslint-disable */
// GENERATED from contracts/*.schema.json by desktop/scripts/generate-types.mjs.
// Do not edit. Regenerate with `npm run gen:types`.

/**
 * A map: its spots in index order and the spread between them.
 */
export interface SpotMap {
  image?: MapImage | null;
  map_id: string;
  rho: InterSpotStats;
  rs: InterSpotStats;
  sigma: InterSpotStats;
  skipped?: SkippedRun[];
  spots?: MapSpot[];
}
/**
 * The photograph kept beside a map's runs.
 *
 * ``sha256`` and ``bytes`` are those of the file as it is on disk now.
 * ``registration`` is absent until a client has stored one, and when the
 * stored one was fitted to a different image than the file holds.
 */
export interface MapImage {
  bytes: number;
  file: string;
  registration?: MapImageRegistration | null;
  sha256: string;
}
/**
 * Where a map's photograph sits on the sample: numbers, never pixels.
 *
 * The image's centre is at (``centre_x_mm``, ``centre_y_mm``) in sample
 * coordinates (millimetres from the sample's centre, y up), one image pixel
 * is ``mm_per_px`` wide, and the image is turned ``rotation_deg``
 * anticlockwise as seen on the sample. ``sha256`` names the image the
 * numbers were fitted to, so they cannot end up beside another picture.
 * ``calibrated`` says the scale is real -- fitted to the outline or set from
 * two points -- rather than the placeholder a fresh image starts with.
 */
export interface MapImageRegistration {
  calibrated?: boolean;
  calibration?: TwoPointCalibration | null;
  centre_x_mm: number;
  centre_y_mm: number;
  image_height_px: number;
  image_width_px: number;
  mm_per_px: number;
  rotation_deg: number;
  sha256: string;
}
/**
 * Two image points a known distance apart: the scale of a photograph of
 * a sample that has no outline to fit. Pixel coordinates of the image as it
 * was taken, x to the right and y down, origin at its top-left corner.
 */
export interface TwoPointCalibration {
  distance_mm: number;
  x1_px: number;
  x2_px: number;
  y1_px: number;
  y2_px: number;
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
  relative_error_rows?: number | null;
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
  end_reason?: string | null;
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

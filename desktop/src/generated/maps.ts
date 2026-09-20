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

/**
 * What a four-point run with these settings would record about a spot.
 *
 * ``geometry_factor`` is the closed-form lateral factor with the probe at
 * the centre of ``sample``, pointing along ``angle_deg``: pi / ln 2 for an
 * unbounded sheet, absent when the probe does not fit on the sample.
 * ``factor_rows`` is the lateral factor the run's rows would apply (the
 * table look-up or K*alpha, thickness term divided out), absent when the
 * rows have no finite Rs.
 *
 * The rest describes the spot's position and is absent -- ``checked`` false
 * -- when no spot was given, the spot has no position or the sample no
 * edges. The values are the ones the file header's ``spot`` block and the
 * ``geometry_warning`` event carry; the errors are fractions.
 */
export interface SpotPreflight {
  angle_deg: number;
  checked?: boolean;
  compared_with?: ("rows" | "centre") | null;
  edge_clearance_s?: number | null;
  edge_warn_pct: number;
  factor_centre?: number | null;
  factor_here?: number | null;
  factor_rows?: number | null;
  geometry_factor?: number | null;
  message?: string | null;
  near_edge?: boolean;
  off_sample?: boolean;
  relative_error?: number | null;
  relative_error_rows?: number | null;
  sample: SampleGeometry;
  spacing_mm: number;
}
/**
 * The lateral outline of a thin sample with insulating edges.
 *
 * ``unbounded`` means a sheet large enough that its edges do not matter,
 * which is what the software assumed before it knew about outlines.
 */
export interface SampleGeometry {
  diameter_mm?: number | null;
  length_mm?: number | null;
  shape?: "unbounded" | "circle" | "rectangle";
  width_mm?: number | null;
}

/**
 * A four-point run request without the run: whose profile, which
 * overrides, and -- optionally -- which spot. ``user`` is accepted for
 * ``username``, the name the map routes use.
 */
export interface SpotPreflightRequest {
  overrides?: {
    [k: string]: unknown | undefined;
  };
  spot?: SpotRequest | null;
  username: string;
}
/**
 * One placement of the probe, as the client describes it.
 *
 * The position is optional -- a spot can be a label and nothing more -- but
 * ``x_mm`` and ``y_mm`` only mean something together. ``angle_deg`` is the
 * direction of the probe array, anticlockwise from +x; absent, the run uses
 * the ``fpp_array_angle_deg`` setting.
 */
export interface SpotRequest {
  angle_deg?: number | null;
  index: number;
  label: string;
  map_id: string;
  x_mm?: number | null;
  y_mm?: number | null;
}

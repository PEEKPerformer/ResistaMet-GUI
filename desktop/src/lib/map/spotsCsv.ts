// The numbers behind the figure, as a CSV to keep beside it. Every value is
// the backend's (GET /maps/{id}); nothing is computed here but the
// percentage form of the edge error.

import type { InterSpotStats, MapSpot, SpotMap } from "../../generated/maps";
import { edgeError } from "./figure.ts";

export const SPOTS_CSV_COLUMNS = [
  "index",
  "label",
  "x_mm",
  "y_mm",
  "angle_deg",
  "n",
  "rs_ohm_per_sq",
  "u_rs_ohm_per_sq",
  "rs_rsd_pct",
  "rho_ohm_cm",
  "u_rho_ohm_cm",
  "sigma_S_per_cm",
  "u_sigma_S_per_cm",
  "edge_error_pct",
  "edge_clearance_s",
  "file",
] as const;

function cell(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "";
  return /[",\r\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
}

function row(spot: MapSpot): string {
  const error = edgeError(spot);
  return [
    spot.index,
    spot.label,
    spot.x_mm,
    spot.y_mm,
    spot.angle_deg,
    spot.stats.n,
    spot.stats.rs.mean,
    spot.stats.rs.u_total,
    spot.stats.rs.rsd_pct,
    spot.stats.rho.mean,
    spot.stats.rho.u_total,
    spot.stats.sigma.mean,
    spot.stats.sigma.u_total,
    error === null ? null : error * 100,
    spot.edge_clearance_s,
    spot.file,
  ]
    .map(cell)
    .join(",");
}

function between(name: string, stats: InterSpotStats): string {
  return `# between_spots.${name}: n=${stats.n} mean=${cell(stats.mean)} sd=${cell(stats.sd)} rsd_pct=${cell(stats.rsd_pct)}`;
}

export interface CsvContext {
  sample: string;
  operator: string;
  exportedAt: Date;
}

/** `#` header lines in the style of the run files, then one row per spot. */
export function spotsCsv(map: SpotMap, context: CsvContext): string {
  const oneLine = (text: string) => text.replace(/[\r\n]+/g, " ");
  const lines = [
    `# map_id: ${map.map_id}`,
    `# sample: ${oneLine(context.sample)}`,
    `# operator: ${oneLine(context.operator)}`,
    `# exported_at: ${context.exportedAt.toISOString()}`,
    "# u_*: combined standard uncertainty (statistical and instrument), as in each run's footer",
    "# edge_error_pct: error in Rs from assuming a centred probe; not applied to any value here",
    between("rs", map.rs),
    between("rho", map.rho),
    between("sigma", map.sigma),
    SPOTS_CSV_COLUMNS.join(","),
    ...(map.spots ?? []).map(row),
  ];
  return lines.join("\n") + "\n";
}

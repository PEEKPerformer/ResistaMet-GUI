// Resistance from an I-V sweep by least squares.
//
// Ordinary least squares puts all the error in the quantity being fitted, so
// the measured quantity is regressed on the sourced one: I on V when voltage
// is sourced (R = 1/slope), V on I when current is sourced (R = slope). The
// other way round, the slope is low by the factor R², and the resistance
// read from it is off by the same factor — 11 % at R² = 0.9.
//
// A point taken in compliance is the limit, not the sample, and is left out.
// Each leg of an up-down sweep is fitted on its own: a sample that heats or
// charges gives two different lines, and one line through both describes
// neither. The legs are pooled only when they agree.

export type Sourced = "voltage" | "current";

export interface SweepLeg {
  direction: "forward" | "reverse";
  voltages: number[];
  currents: number[];
  /** Per point: "OK", or anything else when the source was in compliance. */
  compliance: string[];
}

export interface LineFit {
  /** Resistance in ohms; NaN when the points do not give one. */
  r: number;
  /** Standard uncertainty of r from the scatter about the line; NaN below
   *  three points, where there is no scatter to judge by. */
  u: number;
  r2: number;
  /** Points the fit used. */
  n: number;
  /** Points left out: in compliance, or not a number. */
  excluded: number;
}

export interface SweepFit {
  legs: (LineFit & { direction: "forward" | "reverse" })[];
  /** One fit through every leg's points. Null when there are two or more
   *  legs and they do not agree within their combined uncertainty (k = 2),
   *  or their uncertainty cannot be judged. */
  pooled: LineFit | null;
}

const NO_FIT = { r: NaN, u: NaN, r2: NaN };

/** Two legs closer than this fraction of R agree whatever their scatter:
 *  noiseless data has none, and rounding alone must not split it. */
const AGREE_FLOOR = 1e-9;

interface Points {
  x: number[];
  y: number[];
  excluded: number;
}

function usable(leg: SweepLeg, sourced: Sourced): Points {
  const points: Points = { x: [], y: [], excluded: 0 };
  leg.voltages.forEach((v, i) => {
    const c = leg.currents[i];
    const inCompliance = (leg.compliance[i] ?? "OK") !== "OK";
    if (inCompliance || !Number.isFinite(v) || c === undefined || !Number.isFinite(c)) {
      points.excluded++;
      return;
    }
    points.x.push(sourced === "voltage" ? v : c);
    points.y.push(sourced === "voltage" ? c : v);
  });
  return points;
}

function fitLine({ x, y, excluded }: Points, sourced: Sourced): LineFit {
  const n = x.length;
  if (n < 2) return { ...NO_FIT, n, excluded };
  const mx = x.reduce((a, b) => a + b, 0) / n;
  const my = y.reduce((a, b) => a + b, 0) / n;
  let sxy = 0;
  let sxx = 0;
  let syy = 0;
  for (let i = 0; i < n; i++) {
    const dx = x[i]! - mx;
    const dy = y[i]! - my;
    sxy += dx * dy;
    sxx += dx * dx;
    syy += dy * dy;
  }
  if (sxx === 0) return { ...NO_FIT, n, excluded };
  const slope = sxy / sxx;
  const r2 = syy === 0 ? 1 : (sxy * sxy) / (sxx * syy);
  // Residual variance on n - 2 degrees of freedom, then the slope's.
  const residual = Math.max(0, syy - slope * sxy);
  const uSlope = n > 2 ? Math.sqrt(residual / (n - 2) / sxx) : NaN;
  if (sourced === "current") return { r: slope, u: uSlope, r2, n, excluded };
  // R = 1/slope, so u(R) = u(slope) / slope².
  if (slope === 0) return { r: Infinity, u: NaN, r2, n, excluded };
  return { r: 1 / slope, u: uSlope / (slope * slope), r2, n, excluded };
}

function agree(a: LineFit, b: LineFit): boolean {
  if (!Number.isFinite(a.r) || !Number.isFinite(b.r)) return false;
  const difference = Math.abs(a.r - b.r);
  if (difference <= AGREE_FLOOR * Math.max(Math.abs(a.r), Math.abs(b.r))) return true;
  const combined = Math.hypot(a.u, b.u);
  return Number.isFinite(combined) && difference <= 2 * combined;
}

export function fitSweep(legs: SweepLeg[], sourced: Sourced): SweepFit {
  const points = legs.map((leg) => usable(leg, sourced));
  const fits = legs.map((leg, i) => ({ direction: leg.direction, ...fitLine(points[i]!, sourced) }));
  const first = fits[0];
  if (first === undefined) return { legs: [], pooled: null };
  if (!fits.every((fit) => agree(first, fit)) && fits.length > 1) return { legs: fits, pooled: null };
  const all: Points = {
    x: points.flatMap((p) => p.x),
    y: points.flatMap((p) => p.y),
    excluded: points.reduce((sum, p) => sum + p.excluded, 0),
  };
  return { legs: fits, pooled: fitLine(all, sourced) };
}

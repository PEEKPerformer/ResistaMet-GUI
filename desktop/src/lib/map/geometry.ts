// The sample outline, the probe on it, and the two coordinate systems.
//
// SAMPLE coordinates are what the backend stores: millimetres from the
// centre of the sample, x to the right, **y up**, a rectangle's length along
// x and its width along y, an array angle anticlockwise from +x.
//
// FIGURE coordinates are SVG user units: x to the right, **y down**, origin
// at the figure's top-left corner.
//
// Exactly two functions cross between them, `toFigure` and `toSample`, and
// both negate y. Nothing else in the map code may.
//
// What is here is distance-to-edge only. The geometry factor itself is the
// backend's (`calculations_geometry.py`) and is not ported: the client says
// "about this close to an edge", the backend says what it costs.

export interface Point {
  x: number;
  y: number;
}

export type Outline =
  | { shape: "unbounded" }
  | { shape: "circle"; diameterMm: number }
  | { shape: "rectangle"; widthMm: number; lengthMm: number };

/** Legacy `fpp_geometry` value -> length / width of the rectangle it names. */
const LEGACY_ASPECT: Record<string, number> = { square: 1, rectangle_2: 2, rectangle_3: 3, rectangle_4: 4 };

function positive(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}

/** The outline the backend will check a spot against, from the run's
 *  measurement settings. Mirrors `schema/spots.py`
 *  `sample_geometry_from_settings`: the `fpp_sample_*` keys once a shape is
 *  chosen, otherwise the legacy `fpp_geometry` / `fpp_diameter_cm` pair, so
 *  the map shows the same sample the position check sees.
 *
 *  null when the chosen shape's dimensions have not been entered. */
export function outlineFromSettings(m: Record<string, unknown>): Outline | null {
  const shape = typeof m.fpp_sample_shape === "string" ? m.fpp_sample_shape : "unbounded";
  if (shape === "circle") {
    const diameterMm = positive(m.fpp_sample_diameter_mm);
    return diameterMm === null ? null : { shape: "circle", diameterMm };
  }
  if (shape === "rectangle") {
    const widthMm = positive(m.fpp_sample_width_mm);
    const lengthMm = positive(m.fpp_sample_length_mm);
    return widthMm === null || lengthMm === null ? null : { shape: "rectangle", widthMm, lengthMm };
  }
  const lateralCm = positive(m.fpp_diameter_cm);
  if (lateralCm === null) return { shape: "unbounded" };
  const lateralMm = lateralCm * 10;
  const legacy = typeof m.fpp_geometry === "string" ? m.fpp_geometry : "circle";
  if (legacy === "circle") return { shape: "circle", diameterMm: lateralMm };
  const aspect = LEGACY_ASPECT[legacy];
  if (aspect === undefined) return null;
  return { shape: "rectangle", widthMm: lateralMm, lengthMm: aspect * lateralMm };
}

/** Half extents of the outline along x and y, mm. null for unbounded. */
export function outlineHalfExtents(outline: Outline): Point | null {
  if (outline.shape === "circle") return { x: outline.diameterMm / 2, y: outline.diameterMm / 2 };
  if (outline.shape === "rectangle") return { x: outline.lengthMm / 2, y: outline.widthMm / 2 };
  return null;
}

/** The four tip positions, first current tip first: centre + (k - 1.5) s
 *  along the array direction, k = 0..3 (`calculations_geometry.probe_tips`). */
export function probeTips(centre: Point, angleDeg: number, spacingMm: number): [Point, Point, Point, Point] {
  const angle = (angleDeg * Math.PI) / 180;
  const dx = Math.cos(angle) * spacingMm;
  const dy = Math.sin(angle) * spacingMm;
  const tip = (k: number): Point => ({ x: centre.x + (k - 1.5) * dx, y: centre.y + (k - 1.5) * dy });
  return [tip(0), tip(1), tip(2), tip(3)];
}

/** Smallest distance from any tip to the edge, in probe spacings. Zero or
 *  negative: a tip is on or beyond the edge. null on an unbounded sample.
 *  The same definition as the backend's `*_edge_clearance`. */
export function edgeClearanceS(outline: Outline, centre: Point, angleDeg: number, spacingMm: number): number | null {
  if (outline.shape === "unbounded" || !(spacingMm > 0)) return null;
  const tips = probeTips(centre, angleDeg, spacingMm);
  let smallest = Number.POSITIVE_INFINITY;
  for (const tip of tips) {
    const distance =
      outline.shape === "circle"
        ? outline.diameterMm / 2 - Math.hypot(tip.x, tip.y)
        : Math.min(outline.lengthMm / 2 - Math.abs(tip.x), outline.widthMm / 2 - Math.abs(tip.y));
    if (distance < smallest) smallest = distance;
  }
  return smallest / spacingMm;
}

/** Nearest tip within this many spacings of an edge: caution. On a 20 s
 *  square the centred factor is already 1.6 % off at 5 s (design doc, §2). */
export const CAUTION_CLEARANCE_S = 5;

/** ...but never more than this fraction of the clearance a centred probe
 *  has, or every position on a small sample would be a caution. */
const CAUTION_FRACTION_OF_CENTRE = 0.6;

export type PreflightState = "none" | "ok" | "caution" | "off";

export interface Preflight {
  state: PreflightState;
  /** null when the sample has no edges. */
  clearanceS: number | null;
}

/** Immediate, approximate feedback on a position before Start. `off` is
 *  exact (the backend refuses on the same test); `caution` is a guess at
 *  where the backend's relative error will pass its threshold. */
export function preflight(outline: Outline, centre: Point, angleDeg: number, spacingMm: number): Preflight {
  const clearanceS = edgeClearanceS(outline, centre, angleDeg, spacingMm);
  if (clearanceS === null) return { state: "none", clearanceS: null };
  if (!(clearanceS > 0)) return { state: "off", clearanceS };
  const centred = edgeClearanceS(outline, { x: 0, y: 0 }, angleDeg, spacingMm) ?? 0;
  const threshold = Math.min(CAUTION_CLEARANCE_S, CAUTION_FRACTION_OF_CENTRE * centred);
  return { state: clearanceS < threshold ? "caution" : "ok", clearanceS };
}

// --- sample <-> figure -----------------------------------------------------

/** Where the sample's origin sits in the figure, and the scale. */
export interface Viewport {
  originX: number;
  originY: number;
  pxPerMm: number;
}

export interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** Sample mm (y up) -> figure units (y down). */
export function toFigure(view: Viewport, p: Point): Point {
  return { x: view.originX + p.x * view.pxPerMm, y: view.originY - p.y * view.pxPerMm };
}

/** Figure units (y down) -> sample mm (y up). The inverse of `toFigure`. */
export function toSample(view: Viewport, p: Point): Point {
  return { x: (p.x - view.originX) / view.pxPerMm, y: (view.originY - p.y) / view.pxPerMm };
}

/** Centre a region of `halfExtents` (mm) in `box`, leaving `marginMm` around it. */
export function fitViewport(halfExtents: Point, box: Box, marginMm = 0): Viewport {
  const spanX = 2 * (halfExtents.x + marginMm);
  const spanY = 2 * (halfExtents.y + marginMm);
  const pxPerMm = Math.min(box.width / spanX, box.height / spanY);
  return { originX: box.x + box.width / 2, originY: box.y + box.height / 2, pxPerMm };
}

/** Positions are kept to 0.01 mm, so one reads the same on the screen, in
 *  the request and in the file, and a nudge never accumulates float noise. */
export function roundMm(value: number, stepMm = 0.01): number {
  return Math.round(value / stepMm) * stepMm || 0;
}

/** The longest of 1, 2, 5 x 10^n that is no longer than `maxMm`. */
export function niceLength(maxMm: number): number {
  if (!(maxMm > 0)) return 0;
  const decade = 10 ** Math.floor(Math.log10(maxMm));
  for (const step of [5, 2, 1]) if (step * decade <= maxMm * (1 + 1e-9)) return step * decade;
  return decade;
}

/** Evenly spaced round numbers inside [min, max]. */
export function niceTicks(min: number, max: number, target = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [];
  if (max <= min) return [min];
  const raw = (max - min) / target;
  const decade = 10 ** Math.floor(Math.log10(raw));
  const step = ([1, 2, 5, 10].find((m) => m * decade >= raw) ?? 10) * decade;
  const ticks: number[] = [];
  for (let k = Math.ceil(min / step - 1e-9); k * step <= max * (1 + 1e-12) + step * 1e-9; k++) {
    ticks.push(Number((k * step).toPrecision(12)));
  }
  return ticks;
}

// --- the photograph ---------------------------------------------------------

/** Where a photograph sits on the sample: numbers, never baked into pixels.
 *  The image's centre is at (`centreXmm`, `centreYmm`) in sample coordinates,
 *  one image pixel is `mmPerPx` wide, and the image is turned `rotationDeg`
 *  anticlockwise as seen on the sample. */
export interface Registration {
  mmPerPx: number;
  centreXmm: number;
  centreYmm: number;
  rotationDeg: number;
}

/** The SVG transform that puts an image of `naturalWidth` x `naturalHeight`
 *  pixels, drawn at the origin in its own pixel units, onto the figure.
 *  SVG's rotate() is clockwise on screen because y points down, hence the
 *  minus sign. */
export function photoTransform(view: Viewport, reg: Registration, naturalWidth: number, naturalHeight: number): string {
  const centre = toFigure(view, { x: reg.centreXmm, y: reg.centreYmm });
  const scale = reg.mmPerPx * view.pxPerMm;
  return (
    `translate(${num(centre.x)} ${num(centre.y)}) rotate(${num(-reg.rotationDeg)}) ` +
    `scale(${num(scale, 6)}) translate(${num(-naturalWidth / 2)} ${num(-naturalHeight / 2)})`
  );
}

/** Start with the whole image covering the outline, centred. */
export function initialRegistration(halfExtents: Point, naturalWidth: number, naturalHeight: number): Registration {
  const mmPerPx = Math.max((2 * halfExtents.x) / naturalWidth, (2 * halfExtents.y) / naturalHeight);
  return { mmPerPx, centreXmm: 0, centreYmm: 0, rotationDeg: 0 };
}

/** Millimetres per image pixel from two points a known distance apart. The
 *  points are in sample coordinates under the current registration, so the
 *  new scale is the old one corrected by the ratio. */
export function calibratedScale(reg: Registration, a: Point, b: Point, distanceMm: number): number | null {
  const apparent = Math.hypot(b.x - a.x, b.y - a.y);
  if (!(apparent > 0) || !(distanceMm > 0)) return null;
  return reg.mmPerPx * (distanceMm / apparent);
}

// --- nearest-spot cells -----------------------------------------------------

/** The cell of each site: the part of `bounds` (a convex polygon) nearer to
 *  it than to any other site. Half-plane clipping, O(n^2) per map, which is
 *  nothing for the few dozen spots a hand-placed map has. Works in any
 *  Euclidean frame; the caller clips the result to the outline. */
export function nearestSiteCells(sites: Point[], bounds: Point[]): Point[][] {
  return sites.map((site, i) => {
    let cell = bounds;
    for (let j = 0; j < sites.length && cell.length > 0; j++) {
      if (j === i) continue;
      const other = sites[j]!;
      if (other.x === site.x && other.y === site.y) continue;
      cell = clipToNearer(cell, site, other);
    }
    return cell;
  });
}

/** Keep the part of `polygon` that is at least as near to `a` as to `b`. */
function clipToNearer(polygon: Point[], a: Point, b: Point): Point[] {
  const nx = b.x - a.x;
  const ny = b.y - a.y;
  const limit = (nx * (a.x + b.x) + ny * (a.y + b.y)) / 2;
  const side = (p: Point) => nx * p.x + ny * p.y - limit; // <= 0: nearer to a
  const out: Point[] = [];
  for (let k = 0; k < polygon.length; k++) {
    const p = polygon[k]!;
    const q = polygon[(k + 1) % polygon.length]!;
    const sp = side(p);
    const sq = side(q);
    if (sp <= 0) out.push(p);
    if ((sp < 0 && sq > 0) || (sp > 0 && sq < 0)) {
      const t = sp / (sp - sq);
      out.push({ x: p.x + t * (q.x - p.x), y: p.y + t * (q.y - p.y) });
    }
  }
  return out;
}

/** A number as SVG wants it: no exponent, no trailing noise. */
export function num(value: number, decimals = 2): string {
  if (!Number.isFinite(value)) return "0";
  const fixed = value.toFixed(decimals);
  const text = fixed.includes(".") ? fixed.replace(/\.?0+$/, "") : fixed;
  return text === "-0" || text === "" ? "0" : text;
}

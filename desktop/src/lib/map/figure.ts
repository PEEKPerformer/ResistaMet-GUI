// The map figure, laid out: sample outline to scale, the photograph under
// it, one marker per spot coloured by the chosen quantity, a colour bar, a
// scale bar and a title. Pure: a model in, a Scene out.
//
// Imports carry their .ts extension in this directory so that `node --test`
// can run the modules as they are (see package.json, "test").

import type { MapSpot } from "../../generated/maps";
import { axisLabels, formatEngineering, prefixFor, toSignificant } from "../format.ts";
import { colorScale, inkOn, viridis, type ColorScale } from "./colormap.ts";
import {
  fitViewport,
  nearestSiteCells,
  niceLength,
  niceTicks,
  num,
  outlineHalfExtents,
  photoTransform,
  probeTips,
  toFigure,
  type Box,
  type Outline,
  type Point,
  type Registration,
  type Viewport,
} from "./geometry.ts";
import type { Clip, Prim, Scene } from "./scene.ts";

export type Quantity = "rs" | "rho" | "sigma";

export const QUANTITIES: Record<Quantity, { symbol: string; unit: string; name: string }> = {
  rs: { symbol: "Rs", unit: "Ω/sq", name: "Sheet resistance" },
  rho: { symbol: "ρ", unit: "Ω·cm", name: "Resistivity" },
  sigma: { symbol: "σ", unit: "S/cm", name: "Conductivity" },
};

export function spotValue(spot: MapSpot, quantity: Quantity): number | null {
  const mean = spot.stats[quantity].mean;
  return typeof mean === "number" && Number.isFinite(mean) ? mean : null;
}

/** What the figure shows first: conductivity when the map has any (the
 *  thickness was entered), sheet resistance otherwise. */
export function defaultQuantity(spots: MapSpot[]): Quantity {
  return spots.some((s) => spotValue(s, "sigma") !== null) ? "sigma" : "rs";
}

/** The error in the file's own Rs when the backend could work it out,
 *  else the error against the centre of the outline. A fraction. */
export function edgeError(spot: Pick<MapSpot, "relative_error" | "relative_error_rows">): number | null {
  for (const value of [spot.relative_error_rows, spot.relative_error]) {
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

export function hasPosition(spot: Pick<MapSpot, "x_mm" | "y_mm">): spot is { x_mm: number; y_mm: number } {
  return typeof spot.x_mm === "number" && typeof spot.y_mm === "number" && Number.isFinite(spot.x_mm) && Number.isFinite(spot.y_mm);
}

export interface Palette {
  background: string;
  mapBackground: string;
  sampleFill: string;
  outline: string;
  text: string;
  muted: string;
  markerStroke: string;
  warn: string;
  /** Behind text and lines drawn over a photograph. */
  halo: string;
  fontFamily: string;
}

/** The screen: the app's tokens, so the map follows the theme. */
export const SCREEN_PALETTE: Palette = {
  background: "transparent",
  mapBackground: "var(--bg-inset)",
  sampleFill: "var(--bg-active)",
  outline: "var(--fg-muted)",
  text: "var(--fg)",
  muted: "var(--fg-muted)",
  markerStroke: "var(--fg)",
  warn: "var(--warn)",
  halo: "var(--bg-inset)",
  fontFamily: "inherit",
};

/** The exported file: dark on white, whatever the app's theme. */
export const PRINT_PALETTE: Palette = {
  background: "#ffffff",
  mapBackground: "#ffffff",
  sampleFill: "#eef0f3",
  outline: "#2b3038",
  text: "#14181f",
  muted: "#55606f",
  markerStroke: "#14181f",
  warn: "#b97a0a",
  halo: "#ffffff",
  fontFamily: "Helvetica, Arial, sans-serif",
};

export type LabelMode = "none" | "name" | "value";

export interface FigurePhoto {
  href: string;
  naturalWidth: number;
  naturalHeight: number;
  registration: Registration;
  /** false: an unbounded canvas whose scale has not been set; no mm anywhere. */
  calibrated: boolean;
}

export interface FigureModel {
  title: string;
  subtitle: string;
  outline: Outline;
  spacingMm: number;
  arrayAngleDeg: number;
  edgeWarnPct: number;
  spots: MapSpot[];
  quantity: Quantity;
  labels: LabelMode;
  /** Nearest-spot (Voronoi) fill. Off unless asked for. */
  cells: boolean;
  photo: FigurePhoto | null;
  palette: Palette;
}

export interface FigureLayout {
  scene: Scene;
  view: Viewport;
  mapBox: Box;
  scale: ColorScale;
  /** Spots that are drawn, in drawing order, with their figure positions. */
  placed: { spot: MapSpot; at: Point }[];
}

export const FIGURE_WIDTH = 640;
export const FIGURE_HEIGHT = 460;
const MAP_BOX: Box = { x: 16, y: 56, width: 504, height: 360 };
const MARKER_RADIUS = 7;
const BAR = { x: MAP_BOX.x + MAP_BOX.width + 24, y: MAP_BOX.y + 28, width: 14, height: MAP_BOX.height - 56 };
const BAR_STRIPS = 64;

/** What the viewport must show, in mm either side of the origin. */
export function figureHalfExtents(outline: Outline, photo: FigurePhoto | null, spots: MapSpot[]): Point {
  const bounded = outlineHalfExtents(outline);
  if (bounded) return bounded;
  if (photo) {
    return {
      x: (photo.naturalWidth * photo.registration.mmPerPx) / 2,
      y: (photo.naturalHeight * photo.registration.mmPerPx) / 2,
    };
  }
  let reach = 0;
  for (const s of spots) if (hasPosition(s)) reach = Math.max(reach, Math.abs(s.x_mm), Math.abs(s.y_mm));
  const half = reach > 0 ? reach * 1.25 : 10;
  return { x: half, y: half };
}

export function figureViewport(outline: Outline, photo: FigurePhoto | null, spots: MapSpot[]): Viewport {
  const half = figureHalfExtents(outline, photo, spots);
  // A bounded sample gets air around it; a photograph fills the box.
  const margin = outline.shape === "unbounded" ? 0 : 0.06 * Math.max(half.x, half.y);
  return fitViewport(half, MAP_BOX, margin);
}

export function layoutFigure(model: FigureModel): FigureLayout {
  const { palette, outline, photo } = model;
  const view = figureViewport(outline, photo, model.spots);
  const prims: Prim[] = [];
  const clips: Clip[] = [{ id: "map-box", kind: "rect", ...MAP_BOX }];
  const overPhoto = photo !== null;
  const realScale = outline.shape !== "unbounded" || (photo?.calibrated ?? model.spots.some(hasPosition));

  prims.push({ kind: "rect", ...MAP_BOX, rx: 6, fill: palette.mapBackground });

  // --- the sample, and the photograph under its outline ---------------------
  const origin = toFigure(view, { x: 0, y: 0 });
  const half = outlineHalfExtents(outline);
  if (half && !overPhoto) prims.push(...outlinePrims(outline, view, { fill: palette.sampleFill }));
  if (photo) {
    prims.push({
      kind: "image",
      href: photo.href,
      width: photo.naturalWidth,
      height: photo.naturalHeight,
      transform: photoTransform(view, photo.registration, photo.naturalWidth, photo.naturalHeight),
      clip: "map-box",
    });
  }

  // --- spots -----------------------------------------------------------------
  const drawn = model.spots.filter(hasPosition);
  const scale = colorScale(drawn.map((s) => spotValue(s, model.quantity)));
  const placed = drawn.map((spot) => ({ spot, at: toFigure(view, { x: spot.x_mm!, y: spot.y_mm! }) }));

  if (model.cells && placed.length > 1) {
    if (outline.shape === "circle") clips.push({ id: "map-sample", kind: "circle", cx: origin.x, cy: origin.y, r: half!.x * view.pxPerMm });
    else if (outline.shape === "rectangle") {
      clips.push({
        id: "map-sample",
        kind: "rect",
        x: origin.x - half!.x * view.pxPerMm,
        y: origin.y - half!.y * view.pxPerMm,
        width: 2 * half!.x * view.pxPerMm,
        height: 2 * half!.y * view.pxPerMm,
      });
    }
    const corners: Point[] = [
      { x: MAP_BOX.x, y: MAP_BOX.y },
      { x: MAP_BOX.x + MAP_BOX.width, y: MAP_BOX.y },
      { x: MAP_BOX.x + MAP_BOX.width, y: MAP_BOX.y + MAP_BOX.height },
      { x: MAP_BOX.x, y: MAP_BOX.y + MAP_BOX.height },
    ];
    const cells = nearestSiteCells(placed.map((p) => p.at), corners);
    cells.forEach((cell, i) => {
      if (cell.length < 3) return;
      prims.push({
        kind: "polygon",
        points: cell.map((p) => [p.x, p.y]),
        fill: scale.color(spotValue(placed[i]!.spot, model.quantity)),
        opacity: overPhoto ? 0.5 : 0.85,
        clip: half ? "map-sample" : "map-box",
      });
    });
  }

  if (half) {
    if (overPhoto) prims.push(...outlinePrims(outline, view, { stroke: palette.halo, strokeWidth: 3.5, opacity: 0.7 }));
    prims.push(...outlinePrims(outline, view, { stroke: palette.outline, strokeWidth: 1.25 }));
  }

  for (const { spot, at } of placed) {
    const value = spotValue(spot, model.quantity);
    const fill = scale.color(value);
    // The probe's footprint, to scale: three spacings along the array.
    const tips = probeTips({ x: spot.x_mm!, y: spot.y_mm! }, spot.angle_deg ?? model.arrayAngleDeg, model.spacingMm);
    const first = toFigure(view, tips[0]);
    const last = toFigure(view, tips[3]);
    if (realScale && Math.hypot(last.x - first.x, last.y - first.y) > 2 * MARKER_RADIUS + 4) {
      if (overPhoto) prims.push({ kind: "line", x1: first.x, y1: first.y, x2: last.x, y2: last.y, stroke: palette.halo, strokeWidth: 3.5, opacity: 0.7 });
      prims.push({ kind: "line", x1: first.x, y1: first.y, x2: last.x, y2: last.y, stroke: palette.markerStroke, strokeWidth: 1.25 });
    }
    const error = edgeError(spot);
    if (error !== null && Math.abs(error) * 100 > model.edgeWarnPct) {
      prims.push({ kind: "circle", cx: at.x, cy: at.y, r: MARKER_RADIUS + 3.5, stroke: palette.warn, strokeWidth: 1.5, dash: "3 2" });
    }
    prims.push({ kind: "circle", cx: at.x, cy: at.y, r: MARKER_RADIUS, fill, stroke: palette.markerStroke, strokeWidth: 1.25, spot: spot.index });
    prims.push({ kind: "text", x: at.x, y: at.y + 3, text: String(spot.index), size: 8.5, fill: inkOn(fill), anchor: "middle", weight: 600 });
    const label =
      model.labels === "name" ? spot.label : model.labels === "value" ? formatEngineering(value ?? NaN, QUANTITIES[model.quantity].unit) : "";
    if (label) {
      const leftHalf = at.x < MAP_BOX.x + MAP_BOX.width * 0.75;
      prims.push({
        kind: "text",
        x: at.x + (leftHalf ? MARKER_RADIUS + 5 : -(MARKER_RADIUS + 5)),
        y: at.y + 4,
        text: label,
        size: 11,
        fill: palette.text,
        anchor: leftHalf ? "start" : "end",
        halo: palette.halo,
      });
    }
  }

  // --- title -----------------------------------------------------------------
  prims.push({ kind: "text", x: MAP_BOX.x, y: 24, text: model.title, size: 15, fill: palette.text, weight: 600 });
  prims.push({ kind: "text", x: MAP_BOX.x, y: 42, text: model.subtitle, size: 11, fill: palette.muted });

  // --- colour bar --------------------------------------------------------------
  const q = QUANTITIES[model.quantity];
  if (Number.isFinite(scale.min)) {
    const { prefix, scale: unitScale } = prefixFor(Math.max(Math.abs(scale.min), Math.abs(scale.max)), q.unit);
    prims.push({ kind: "text", x: BAR.x, y: BAR.y - 10, text: `${q.symbol} (${prefix}${q.unit})`, size: 11, fill: palette.text, weight: 600 });
    const strip = BAR.height / BAR_STRIPS;
    for (let k = 0; k < BAR_STRIPS; k++) {
      // The top of the bar is the top of the scale.
      const t = scale.flat ? 0.5 : 1 - (k + 0.5) / BAR_STRIPS;
      prims.push({ kind: "rect", x: BAR.x, y: BAR.y + k * strip, width: BAR.width, height: strip + 0.5, fill: viridis(t) });
    }
    prims.push({ kind: "rect", x: BAR.x, y: BAR.y, width: BAR.width, height: BAR.height, stroke: palette.outline, strokeWidth: 0.75 });
    const ticks = scale.flat ? [scale.min] : niceTicks(scale.min, scale.max);
    // One prefix for the whole bar, stated once in its title.
    const suffix = ` ${prefix}${q.unit}`;
    const texts = scale.flat
      ? [toSignificant(scale.min / unitScale, 4)]
      : axisLabels(ticks, q.unit).map((t) => (t.endsWith(suffix) ? t.slice(0, -suffix.length) : t));
    ticks.forEach((tick, i) => {
      const t = scale.flat ? 0.5 : (tick - scale.min) / (scale.max - scale.min);
      const y = BAR.y + BAR.height * (1 - t);
      prims.push({ kind: "line", x1: BAR.x + BAR.width, y1: y, x2: BAR.x + BAR.width + 4, y2: y, stroke: palette.outline, strokeWidth: 0.75 });
      prims.push({ kind: "text", x: BAR.x + BAR.width + 7, y: y + 3.5, text: texts[i] ?? "", size: 10.5, fill: palette.text });
    });
  }

  // --- scale bar and footnotes ---------------------------------------------------
  const footY = MAP_BOX.y + MAP_BOX.height + 20;
  if (realScale) {
    const lengthMm = niceLength((0.3 * MAP_BOX.width) / view.pxPerMm);
    const lengthPx = lengthMm * view.pxPerMm;
    prims.push({ kind: "line", x1: MAP_BOX.x, y1: footY - 4, x2: MAP_BOX.x + lengthPx, y2: footY - 4, stroke: palette.text, strokeWidth: 2 });
    for (const x of [MAP_BOX.x, MAP_BOX.x + lengthPx]) {
      prims.push({ kind: "line", x1: x, y1: footY - 8, x2: x, y2: footY, stroke: palette.text, strokeWidth: 1.25 });
    }
    prims.push({ kind: "text", x: MAP_BOX.x + lengthPx + 8, y: footY, text: `${num(lengthMm, 3)} mm`, size: 11, fill: palette.text });
  }
  const notes = [`s = ${num(model.spacingMm, 3)} mm`, `array ${num(model.arrayAngleDeg, 1)}°`];
  if (!realScale) notes.unshift("scale not set");
  prims.push({ kind: "text", x: MAP_BOX.x + MAP_BOX.width, y: footY, text: notes.join(" · "), size: 11, fill: palette.muted, anchor: "end" });
  if (model.cells && placed.length > 1) {
    prims.push({
      kind: "text",
      x: MAP_BOX.x + MAP_BOX.width,
      y: footY + 16,
      text: "Fill: nearest spot (Voronoi cells), not an interpolation",
      size: 10.5,
      fill: palette.muted,
      anchor: "end",
    });
  }

  return {
    scene: { width: FIGURE_WIDTH, height: FIGURE_HEIGHT, fontFamily: palette.fontFamily, background: palette.background, clips, prims },
    view,
    mapBox: MAP_BOX,
    scale,
    placed,
  };
}

function outlinePrims(outline: Outline, view: Viewport, paint: { fill?: string; stroke?: string; strokeWidth?: number; opacity?: number }): Prim[] {
  const origin = toFigure(view, { x: 0, y: 0 });
  if (outline.shape === "circle") {
    return [{ kind: "circle", cx: origin.x, cy: origin.y, r: (outline.diameterMm / 2) * view.pxPerMm, ...paint }];
  }
  if (outline.shape === "rectangle") {
    // Length along x, width along y: the backend's convention.
    const w = outline.lengthMm * view.pxPerMm;
    const h = outline.widthMm * view.pxPerMm;
    return [{ kind: "rect", x: origin.x - w / 2, y: origin.y - h / 2, width: w, height: h, ...paint }];
  }
  return [];
}

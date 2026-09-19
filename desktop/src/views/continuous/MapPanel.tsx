// The four-point map: the sample outline to scale and one marker per spot,
// colored by what was measured there. Optional, and out of the way: with no
// outline to draw the panel starts collapsed.
//
// The drawing is a Scene laid out by lib/map/figure.ts; this file decides
// what goes into it and handles the pointer.

import { useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import type { MapSpot } from "../../generated/maps";
import type { MapOwner } from "../../lib/map/mapId";
import { outlineFromSettings, probeTips, roundMm, toFigure, toSample, type Outline, type Preflight, type Viewport } from "../../lib/map/geometry";
import {
  defaultQuantity,
  edgeError,
  FIGURE_HEIGHT,
  FIGURE_WIDTH,
  hasPosition,
  layoutFigure,
  QUANTITIES,
  SCREEN_PALETTE,
  type FigureModel,
  type LabelMode,
  type Quantity,
} from "../../lib/map/figure";
import { formatWithUncertainty } from "../../lib/format";
import { activeMap, activeMapId, setPending, useSpots } from "../../state/spots";
import { setMapView, useMapView } from "../../state/mapView";
import { Button, Panel, Select, Toggle } from "../../components/ui";
import { EngineeringInput } from "../../components/ui/EngineeringInput";
import { Icons } from "../../components/icons";
import { SceneSvg } from "../../components/map/SceneSvg";
import { describeClearance, preflightFor } from "./fourPointSpot";
import styles from "./MapPanel.module.css";

interface Props {
  owner: MapOwner | null;
  /** The measurement settings the next run would use. */
  measurement: Record<string, unknown>;
  /** A run is going: the position belongs to it and cannot be moved. */
  running: boolean;
  /** The view's Start, offered again beside the position: the map sits a
   *  scroll away from the button at the top. */
  start: { enabled: boolean; run: () => void };
}

/** Arrow keys move the pending spot by this much; with Shift, ten times it.
 *  A click lands on the same grid: a pixel is about that wide, and "7.0" reads
 *  better than "6.97" in a file. Typed coordinates keep 0.01 mm. */
const NUDGE_MM = 0.1;

function numberOf(settings: Record<string, unknown>, key: string, fallback: number): number {
  const value = settings[key];
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/** Everything the figure needs that is not the operator's taste. */
export function useFigureInputs(owner: MapOwner | null, measurement: Record<string, unknown>) {
  const spots = useSpots();
  const map = activeMap(spots, owner);
  const mapId = activeMapId(spots, owner);
  const outline = useMemo(() => outlineFromSettings(measurement), [measurement]);
  return {
    map,
    mapId,
    outline,
    spacingMm: numberOf(measurement, "fpp_spacing_cm", 0.1016) * 10,
    // One array angle per map (open question 2 of the spots design): the
    // setting, sent with no per-spot angle, so the backend applies it too.
    arrayAngleDeg: numberOf(measurement, "fpp_array_angle_deg", 0),
    edgeWarnPct: numberOf(measurement, "fpp_edge_warn_pct", 1),
  };
}

export function figureTitles(sample: string, quantity: Quantity, spots: MapSpot[], mapId: string | null): { title: string; subtitle: string } {
  const q = QUANTITIES[quantity];
  const dated = spots.find((s) => typeof s.started_at === "string")?.started_at?.slice(0, 10);
  const date = dated ?? new Date().toISOString().slice(0, 10);
  return {
    title: sample.trim() || "Sample",
    subtitle: [`${q.name}, ${q.symbol}`, date, mapId].filter(Boolean).join(" · "),
  };
}

export function MapPanel({ owner, measurement, running, start }: Props) {
  const view = useMapView();
  const { pending } = useSpots();
  const svgRef = useRef<SVGSVGElement>(null);
  const { map, mapId, outline, spacingMm, arrayAngleDeg, edgeWarnPct } = useFigureInputs(owner, measurement);
  const [hovered, setHovered] = useState<number | null>(null);

  const spots = map?.spots ?? [];
  const quantity = view.quantity ?? defaultQuantity(spots);
  const bounded = outline !== null && outline.shape !== "unbounded";
  const open = view.open ?? bounded;
  const unplaced = spots.filter((s) => !hasPosition(s)).length;

  const layout = useMemo(() => {
    const model: FigureModel = {
      ...figureTitles(owner?.sample ?? "", quantity, spots, mapId),
      outline: outline ?? { shape: "unbounded" },
      spacingMm,
      arrayAngleDeg,
      edgeWarnPct,
      spots,
      quantity,
      labels: view.labels,
      cells: view.cells,
      photo: null,
      palette: SCREEN_PALETTE,
    };
    return layoutFigure(model);
    // `spots` is a fresh array every render while there is no map.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [owner?.sample, quantity, map, mapId, outline, spacingMm, arrayAngleDeg, edgeWarnPct, view.labels, view.cells]);

  // A position means something on an outline with real dimensions.
  const canPlace = bounded && !running;
  const check = preflightFor(measurement, pending);

  const place = (e: PointerEvent<SVGSVGElement>) => {
    const svg = svgRef.current;
    if (!canPlace || !svg || e.button !== 0) return;
    // The SVG keeps its aspect, so client pixels scale evenly into figure units.
    const rect = svg.getBoundingClientRect();
    const fx = ((e.clientX - rect.left) / rect.width) * FIGURE_WIDTH;
    const fy = ((e.clientY - rect.top) / rect.height) * FIGURE_HEIGHT;
    const box = layout.mapBox;
    if (fx < box.x || fx > box.x + box.width || fy < box.y || fy > box.y + box.height) return;
    const at = toSample(layout.view, { x: fx, y: fy });
    setPending({ x_mm: roundMm(at.x, NUDGE_MM), y_mm: roundMm(at.y, NUDGE_MM) });
  };

  const nudge = (e: KeyboardEvent<SVGSVGElement>) => {
    if (!canPlace) return;
    if (e.key === "Escape" || e.key === "Delete" || e.key === "Backspace") {
      if (pending) setPending(null);
      return;
    }
    // Up is +y: the sample's y points up the screen.
    const direction = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] }[e.key];
    if (!direction) return;
    e.preventDefault();
    const step = NUDGE_MM * (e.shiftKey ? 10 : 1);
    const from = pending ?? { x_mm: 0, y_mm: 0 };
    setPending({ x_mm: roundMm(from.x_mm + direction[0]! * step), y_mm: roundMm(from.y_mm + direction[1]! * step) });
  };

  const hoveredSpot = hovered === null ? null : (layout.placed.find((p) => p.spot.index === hovered) ?? null);

  return (
    <Panel
      title={
        <button type="button" className={styles.disclosure} aria-expanded={open} onClick={() => setMapView({ open: !open })}>
          {open ? <Icons.chevronDown size={14} /> : <Icons.chevronUp size={14} style={{ transform: "rotate(90deg)" }} />}
          Map
          {!open ? <span className={styles.summary}>{describe(outline)}</span> : null}
        </button>
      }
      actions={
        open ? (
          <div className={styles.tools}>
            <Select
              aria-label="Quantity"
              className={styles.pick}
              value={quantity}
              onChange={(e) => setMapView({ quantity: e.target.value as Quantity })}
            >
              {(Object.keys(QUANTITIES) as Quantity[]).map((q) => (
                <option key={q} value={q}>
                  {QUANTITIES[q].symbol}
                </option>
              ))}
            </Select>
            <Select aria-label="Labels" className={styles.pick} value={view.labels} onChange={(e) => setMapView({ labels: e.target.value as LabelMode })}>
              <option value="none">No labels</option>
              <option value="name">Names</option>
              <option value="value">Values</option>
            </Select>
            <label className={styles.toggle} title="Fill each spot's nearest-neighbor (Voronoi) cell with its color. Not an interpolation.">
              <Toggle checked={view.cells} onChange={(cells) => setMapView({ cells })} label="Nearest-spot fill" />
              Cells
            </label>
          </div>
        ) : undefined
      }
      className={styles.panel}
      bodyClassName={open ? styles.body : styles.closed}
    >
      {open ? (
        <>
          <div className={styles.canvas}>
            <SceneSvg
              scene={layout.scene}
              svgRef={svgRef}
              onSpot={setHovered}
              role="application"
              aria-label="Map of the sample. Click to place the next spot; arrow keys move it by 0.1 mm, with Shift by 1 mm."
              tabIndex={canPlace ? 0 : -1}
              onPointerDown={place}
              onKeyDown={nudge}
              style={{ cursor: canPlace ? "crosshair" : "default" }}
            >
              {pending && bounded ? (
                <PendingMarker view={layout.view} at={pending} angleDeg={arrayAngleDeg} spacingMm={spacingMm} state={check?.state ?? "none"} />
              ) : null}
            </SceneSvg>
            {hoveredSpot ? <SpotTip spot={hoveredSpot.spot} quantity={quantity} x={hoveredSpot.at.x} y={hoveredSpot.at.y} /> : null}
          </div>
          {bounded ? (
            <div className={styles.position}>
              <span className={styles.positionLabel}>Next spot</span>
              <span className={styles.coordinate}>
              <EngineeringInput
                value={pending?.x_mm ?? null}
                unit="mm"
                nullable
                disabled={!canPlace}
                placeholder="x"
                onChange={(x) => setPending(x === null ? null : { x_mm: roundMm(x), y_mm: pending?.y_mm ?? 0 })}
              />
              </span>
              <span className={styles.coordinate}>
              <EngineeringInput
                value={pending?.y_mm ?? null}
                unit="mm"
                nullable
                disabled={!canPlace}
                placeholder="y"
                onChange={(y) => setPending(y === null ? null : { x_mm: pending?.x_mm ?? 0, y_mm: roundMm(y) })}
              />
              </span>
              {pending ? (
                <>
                  <Button size="sm" variant="ghost" disabled={!canPlace} onClick={() => setPending(null)}>
                    Clear
                  </Button>
                  <Button size="sm" variant="primary" disabled={!start.enabled} onClick={start.run}>
                    <Icons.play size={12} /> Start here
                  </Button>
                </>
              ) : null}
              <span className={check ? positionTone(check, styles) : styles.faint}>
                {check ? `${describeClearance(check)}${check.state === "caution" ? "; the backend reports the error at Start" : ""}` : "No position: click the map, or Start without one."}
              </span>
            </div>
          ) : null}
          <div className={styles.status}>
            {outline === null ? <span className={styles.warn}>Enter the sample's dimensions in Settings ▸ Sample to draw its outline.</span> : null}
            {unplaced > 0 ? (
              <span>
                {unplaced} of {spots.length} {spots.length === 1 ? "spot" : "spots"} not drawn: no position.
              </span>
            ) : null}
          </div>
        </>
      ) : null}
    </Panel>
  );
}

function positionTone(check: Preflight, css: Record<string, string>): string | undefined {
  return check.state === "off" ? css.danger : check.state === "caution" ? css.warn : css.faint;
}

const STATE_COLOR: Record<Preflight["state"], string> = {
  none: "var(--accent)",
  ok: "var(--accent)",
  caution: "var(--warn)",
  off: "var(--danger)",
};

/** The spot the next run measures: the four tips to scale, along the array. */
function PendingMarker({ view, at, angleDeg, spacingMm, state }: { view: Viewport; at: { x_mm: number; y_mm: number }; angleDeg: number; spacingMm: number; state: Preflight["state"] }) {
  const color = STATE_COLOR[state];
  const centre = toFigure(view, { x: at.x_mm, y: at.y_mm });
  const tips = probeTips({ x: at.x_mm, y: at.y_mm }, angleDeg, spacingMm).map((tip) => toFigure(view, tip));
  const tipRadius = Math.max(2, Math.min(4, 0.12 * spacingMm * view.pxPerMm));
  return (
    <g pointerEvents="none" style={{ color }}>
      <circle cx={centre.x} cy={centre.y} r={11} style={{ fill: "none", stroke: "var(--bg-inset)", strokeWidth: 4, opacity: 0.7 }} />
      <circle cx={centre.x} cy={centre.y} r={11} style={{ fill: "none", stroke: "currentColor", strokeWidth: 1.5, strokeDasharray: "4 3" }} />
      <line x1={tips[0]!.x} y1={tips[0]!.y} x2={tips[3]!.x} y2={tips[3]!.y} style={{ stroke: "currentColor", strokeWidth: 1.25 }} />
      {tips.map((tip, k) => (
        <circle key={k} cx={tip.x} cy={tip.y} r={tipRadius} style={{ fill: "currentColor", stroke: "var(--bg-inset)", strokeWidth: 1 }} />
      ))}
    </g>
  );
}

function describe(outline: Outline | null): string {
  if (outline === null) return "dimensions missing";
  if (outline.shape === "circle") return `circle, ${outline.diameterMm} mm`;
  if (outline.shape === "rectangle") return `${outline.lengthMm} × ${outline.widthMm} mm`;
  return "no outline";
}

function SpotTip({ spot, quantity, x, y }: { spot: MapSpot; quantity: Quantity; x: number; y: number }) {
  const error = edgeError(spot);
  const q = QUANTITIES[quantity];
  // Positioned in the figure's own units, as percentages of the canvas.
  const right = x > FIGURE_WIDTH * 0.6;
  return (
    <div
      className={styles.tip}
      style={{
        top: `${(y / FIGURE_HEIGHT) * 100}%`,
        ...(right ? { right: `${(1 - x / FIGURE_WIDTH) * 100}%`, marginRight: 14 } : { left: `${(x / FIGURE_WIDTH) * 100}%`, marginLeft: 14 }),
      }}
      role="tooltip"
    >
      <strong>
        #{spot.index} {spot.label}
      </strong>
      <span className="num">Rs {formatWithUncertainty(spot.stats.rs.mean, spot.stats.rs.u_total, "Ω/sq")}</span>
      {quantity !== "rs" ? (
        <span className="num">
          {q.symbol} {formatWithUncertainty(spot.stats[quantity].mean, spot.stats[quantity].u_total, q.unit)}
        </span>
      ) : null}
      <span className="num">
        RSD {typeof spot.stats.rs.rsd_pct === "number" ? `${spot.stats.rs.rsd_pct.toFixed(2)} %` : "—"} · n {spot.stats.n}
      </span>
      <span className="num">
        x {spot.x_mm?.toFixed(2)} · y {spot.y_mm?.toFixed(2)} mm
      </span>
      {error !== null ? <span className="num">edge {(error * 100).toFixed(1)} % (not applied)</span> : null}
    </div>
  );
}


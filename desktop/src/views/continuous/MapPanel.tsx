// The four-point map: the sample outline to scale and one marker per spot,
// colored by what was measured there. Optional, and out of the way: with no
// outline to draw the panel starts collapsed.
//
// The drawing is a Scene laid out by lib/map/figure.ts; this file decides
// what goes into it and handles the pointer.

import { useMemo, useState } from "react";
import type { MapSpot } from "../../generated/maps";
import type { MapOwner } from "../../lib/map/mapId";
import { outlineFromSettings, type Outline } from "../../lib/map/geometry";
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
import { activeMap, activeMapId, useSpots } from "../../state/spots";
import { setMapView, useMapView } from "../../state/mapView";
import { Panel, Select, Toggle } from "../../components/ui";
import { Icons } from "../../components/icons";
import { SceneSvg } from "../../components/map/SceneSvg";
import styles from "./MapPanel.module.css";

interface Props {
  owner: MapOwner | null;
  /** The measurement settings the next run would use. */
  measurement: Record<string, unknown>;
}

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

export function MapPanel({ owner, measurement }: Props) {
  const view = useMapView();
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
            <SceneSvg scene={layout.scene} onSpot={setHovered} role="img" aria-label="Map of the sample with the measured spots" />
            {hoveredSpot ? <SpotTip spot={hoveredSpot.spot} quantity={quantity} x={hoveredSpot.at.x} y={hoveredSpot.at.y} /> : null}
          </div>
          <div className={styles.status}>
            {outline === null ? <span className={styles.warn}>Enter the sample's dimensions in Settings ▸ Sample to draw its outline.</span> : null}
            {unplaced > 0 ? (
              <span>
                {unplaced} of {spots.length} {spots.length === 1 ? "spot has" : "spots have"} no position and {unplaced === 1 ? "is" : "are"} in the table only.
              </span>
            ) : null}
          </div>
        </>
      ) : null}
    </Panel>
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


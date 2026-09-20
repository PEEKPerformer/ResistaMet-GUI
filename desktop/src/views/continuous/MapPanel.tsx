// The four-point map: the sample outline to scale and one marker per spot,
// colored by what was measured there. Optional, and out of the way: with no
// outline to draw the panel starts collapsed.
//
// The drawing is a Scene laid out by lib/map/figure.ts; this file decides
// what goes into it and handles the pointer.

import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import type { MapSpot } from "../../generated/maps";
import type { MapOwner } from "../../lib/map/mapId";
import {
  calibratedScale,
  initialRegistration,
  outlineFromSettings,
  outlineHalfExtents,
  probeTips,
  roundMm,
  toFigure,
  toSample,
  type Outline,
  type Point,
  type Preflight,
  type Viewport,
} from "../../lib/map/geometry";
import {
  defaultQuantity,
  edgeError,
  FIGURE_HEIGHT,
  FIGURE_WIDTH,
  hasPosition,
  layoutFigure,
  PRINT_PALETTE,
  QUANTITIES,
  SCREEN_PALETTE,
  type FigureModel,
  type LabelMode,
  type Quantity,
} from "../../lib/map/figure";
import { downloadAll, figureFiles, fileToDataUrl } from "../../lib/map/exportFigure";
import { spotsCsv } from "../../lib/map/spotsCsv";
import { formatWithUncertainty } from "../../lib/format";
import { activeMap, activeMapId, setPending, useSpots } from "../../state/spots";
import { setMapView, useMapView } from "../../state/mapView";
import { addPhoto, followMap, removePhoto, setRegistration, useMapPhoto } from "../../state/mapPhoto";
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

/** What the pointer does on the map. */
type Tool = "place" | "align" | "scale";

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
  const { pending, current } = useSpots();
  const { photo, stored: storedPhoto, error: photoError } = useMapPhoto();
  const svgRef = useRef<SVGSVGElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [tool, setTool] = useState<Tool>("place");
  const [scalePoints, setScalePoints] = useState<Point[]>([]);
  const [scaleDistance, setScaleDistance] = useState<number | null>(null);
  const drag = useRef<{ x: number; y: number } | null>(null);
  /** null, "busy", "done", or what went wrong. */
  const [exporting, setExporting] = useState<string | null>(null);
  const { map, mapId, outline, spacingMm, arrayAngleDeg, edgeWarnPct } = useFigureInputs(owner, measurement);
  const [hovered, setHovered] = useState<number | null>(null);

  const spots = map?.spots ?? [];
  const quantity = view.quantity ?? defaultQuantity(spots);
  const bounded = outline !== null && outline.shape !== "unbounded";
  const open = view.open ?? (bounded || photo !== null || storedPhoto !== null);

  // The photograph is stored with the map. A map that ended because the
  // operator asked for a new one keeps it; another sample does not.
  useEffect(() => followMap(mapId, current === null), [mapId, current]);
  // A tool that has lost its photograph has nothing to act on.
  useEffect(() => {
    if (photo === null) setTool("place");
  }, [photo]);
  const unplaced = spots.filter((s) => !hasPosition(s)).length;

  const model = useMemo<FigureModel>(() => {
    return {
      ...figureTitles(owner?.sample ?? "", quantity, spots, mapId),
      outline: outline ?? { shape: "unbounded" },
      spacingMm,
      arrayAngleDeg,
      edgeWarnPct,
      spots,
      quantity,
      labels: view.labels,
      cells: view.cells,
      photo: photo
        ? {
            href: photo.url,
            naturalWidth: photo.naturalWidth,
            naturalHeight: photo.naturalHeight,
            // With no outline the photograph is the canvas: upright, centered,
            // and only its scale means anything.
            registration: bounded ? photo.registration : { mmPerPx: photo.registration.mmPerPx, centreXmm: 0, centreYmm: 0, rotationDeg: 0 },
            calibrated: photo.calibrated,
          }
        : null,
      palette: SCREEN_PALETTE,
    };
    // `spots` is a fresh array every render while there is no map.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [owner?.sample, quantity, map, mapId, outline, spacingMm, arrayAngleDeg, edgeWarnPct, view.labels, view.cells, photo, bounded]);
  const layout = useMemo(() => layoutFigure(model), [model]);

  // A position is millimetres, so it needs a real scale: an outline with
  // dimensions, or a photograph whose scale has been set.
  const scaled = bounded || (outline !== null && photo !== null && photo.calibrated);
  const canPlace = scaled && !running && tool === "place";
  const check = preflightFor(measurement, pending);

  /** The pointer in figure units. The SVG keeps its aspect, so client pixels
   *  scale evenly. null outside the map box. */
  const figurePoint = (e: { clientX: number; clientY: number }): Point | null => {
    const svg = svgRef.current;
    if (!svg) return null;
    const rect = svg.getBoundingClientRect();
    const fx = ((e.clientX - rect.left) / rect.width) * FIGURE_WIDTH;
    const fy = ((e.clientY - rect.top) / rect.height) * FIGURE_HEIGHT;
    const box = layout.mapBox;
    return fx < box.x || fx > box.x + box.width || fy < box.y || fy > box.y + box.height ? null : { x: fx, y: fy };
  };

  const pointerDown = (e: PointerEvent<SVGSVGElement>) => {
    if (e.button !== 0) return;
    const at = figurePoint(e);
    if (at === null) return;
    if (tool === "align" && photo) {
      drag.current = at;
      e.currentTarget.setPointerCapture(e.pointerId);
    } else if (tool === "scale" && photo) {
      setScalePoints((points) => (points.length >= 2 ? [toSample(layout.view, at)] : [...points, toSample(layout.view, at)]));
    } else if (canPlace) {
      const p = toSample(layout.view, at);
      setPending({ x_mm: roundMm(p.x, NUDGE_MM), y_mm: roundMm(p.y, NUDGE_MM) });
    }
  };

  // Dragging moves the photograph under the outline; the outline stays put,
  // because it is the sample and the sample is the coordinate system.
  const pointerMove = (e: PointerEvent<SVGSVGElement>) => {
    if (tool !== "align" || !photo || drag.current === null) return;
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const at = { x: ((e.clientX - rect.left) / rect.width) * FIGURE_WIDTH, y: ((e.clientY - rect.top) / rect.height) * FIGURE_HEIGHT };
    const from = toSample(layout.view, drag.current);
    const to = toSample(layout.view, at);
    drag.current = at;
    setRegistration({ centreXmm: photo.registration.centreXmm + (to.x - from.x), centreYmm: photo.registration.centreYmm + (to.y - from.y) });
  };

  const pointerUp = () => {
    drag.current = null;
  };

  // The wheel scales the photograph about the pointer. React's wheel handler
  // is passive and cannot stop the page from scrolling, hence the listener.
  const wheelState = useRef({ view: layout.view, photo });
  wheelState.current = { view: layout.view, photo };
  useEffect(() => {
    const svg = svgRef.current;
    if (tool !== "align" || !svg) return;
    const onWheel = (e: WheelEvent) => {
      const { view: v, photo: p } = wheelState.current;
      if (!p) return;
      e.preventDefault();
      const rect = svg.getBoundingClientRect();
      const anchor = toSample(v, { x: ((e.clientX - rect.left) / rect.width) * FIGURE_WIDTH, y: ((e.clientY - rect.top) / rect.height) * FIGURE_HEIGHT });
      const k = Math.exp(-e.deltaY * 0.0015);
      const reg = p.registration;
      setRegistration({
        mmPerPx: reg.mmPerPx * k,
        centreXmm: anchor.x + (reg.centreXmm - anchor.x) * k,
        centreYmm: anchor.y + (reg.centreYmm - anchor.y) * k,
      });
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, [tool, open]);

  const halfExtents = outline ? outlineHalfExtents(outline) : null;
  const fitScale = photo && halfExtents ? initialRegistration(halfExtents, photo.naturalWidth, photo.naturalHeight).mmPerPx : null;

  // The figure as drawn, in the print palette, with the photograph inlined
  // so the files stand alone.
  const exportFigure = async () => {
    if (!map || !owner) return;
    setExporting("busy");
    try {
      const href = photo && view.exportPhoto ? await fileToDataUrl(photo.file) : null;
      const printed = layoutFigure({ ...model, palette: PRINT_PALETTE, photo: model.photo && href ? { ...model.photo, href } : null });
      const csv = spotsCsv(map, { sample: owner.sample, operator: owner.user, exportedAt: new Date() });
      downloadAll(await figureFiles(printed.scene, csv, `${map.map_id}_${quantity}`));
      setExporting("done");
    } catch (e) {
      setExporting(e instanceof Error ? e.message : String(e));
    }
  };

  const applyScale = () => {
    if (!photo || scalePoints.length !== 2 || scaleDistance === null) return;
    const mmPerPx = calibratedScale(photo.registration, scalePoints[0]!, scalePoints[1]!, scaleDistance);
    if (mmPerPx === null) return;
    setRegistration({ mmPerPx }, true);
    // Positions are millimetres: a spot placed under the old scale would
    // now be somewhere else on the photograph.
    setPending(null);
    setScalePoints([]);
    setScaleDistance(null);
    setTool("place");
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
          <div className={styles.photoBar}>
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              hidden
              onChange={(e) => {
                const file = e.target.files?.[0];
                e.target.value = "";
                if (file && outline) void addPhoto(file, halfExtents);
              }}
            />
            {photo === null ? (
              <Button size="sm" disabled={outline === null} onClick={() => fileRef.current?.click()} title="Shown under the outline. The file stays where it is; nothing is uploaded.">
                Add photo
              </Button>
            ) : (
              <>
                {bounded ? (
                  <Button size="sm" variant={tool === "align" ? "primary" : "default"} disabled={running} onClick={() => setTool(tool === "align" ? "place" : "align")}>
                    {tool === "align" ? "Done" : "Align photo"}
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    variant={tool === "scale" ? "primary" : "default"}
                    disabled={running}
                    onClick={() => {
                      setScalePoints([]);
                      setTool(tool === "scale" ? "place" : "scale");
                    }}
                  >
                    {tool === "scale" ? "Cancel" : photo.calibrated ? "Reset scale" : "Set scale"}
                  </Button>
                )}
                <Button size="sm" variant="ghost" disabled={running} onClick={removePhoto}>
                  Remove photo
                </Button>
                <span className={styles.photoName} title={photo.sha256 ? `SHA-256 ${photo.sha256}` : "SHA-256 unavailable in this webview"}>
                  {photo.name} · <span className="num">{photo.naturalWidth} × {photo.naturalHeight}</span>
                  {photo.sha256 ? <span className="mono"> · {photo.sha256.slice(0, 8)}</span> : null}
                </span>
              </>
            )}
            {photo === null && storedPhoto ? (
              <span className={styles.photoName}>
                {storedPhoto.name} is not loaded; only its placement is kept. Add it again to show it.
              </span>
            ) : null}
            {photoError ? <span className={styles.danger}>{photoError}</span> : null}
            <span className={styles.exportTools}>
              {photo ? (
                <label className={styles.toggle} title="Include the photograph in the exported figure">
                  <Toggle checked={view.exportPhoto} onChange={(exportPhoto) => setMapView({ exportPhoto })} label="Photo in export" />
                  Photo in export
                </label>
              ) : null}
              <Button
                size="sm"
                disabled={map === null || spots.length === 0 || exporting === "busy"}
                onClick={() => void exportFigure()}
                title="SVG, PNG at 2× and 4×, and a CSV of the spots, to the downloads folder"
              >
                Export figure
              </Button>
            </span>
          </div>
          {exporting !== null && exporting !== "busy" ? (
            <div className={styles.status}>
              <span className={exporting === "done" ? undefined : styles.danger}>
                {exporting === "done" ? `Saved to the downloads folder as ${map?.map_id ?? "map"}_${quantity}.svg, @2x.png, @4x.png and .csv.` : `Export failed: ${exporting}`}
              </span>
            </div>
          ) : null}
          {tool === "align" && photo && fitScale ? (
            <div className={styles.alignBar}>
              <span className={styles.faint}>Drag to move, wheel to scale.</span>
              <label>
                Scale
                <input
                  type="range"
                  min={-3}
                  max={3}
                  step={0.005}
                  value={Math.log2(photo.registration.mmPerPx / fitScale)}
                  onChange={(e) => setRegistration({ mmPerPx: fitScale * 2 ** Number(e.target.value) })}
                />
                <span className="num">{(1 / photo.registration.mmPerPx).toFixed(1)} px/mm</span>
              </label>
              <label>
                Rotation
                <input
                  type="range"
                  min={-180}
                  max={180}
                  step={0.1}
                  value={photo.registration.rotationDeg}
                  onChange={(e) => setRegistration({ rotationDeg: Number(e.target.value) })}
                />
                <span className="num">{photo.registration.rotationDeg.toFixed(1)}°</span>
              </label>
              <Button size="sm" variant="ghost" onClick={() => halfExtents && setRegistration(initialRegistration(halfExtents, photo.naturalWidth, photo.naturalHeight))}>
                Reset
              </Button>
            </div>
          ) : null}
          {tool === "scale" && photo ? (
            <div className={styles.alignBar}>
              <span className={styles.faint}>{scalePoints.length < 2 ? `Click two points a known distance apart (${scalePoints.length}/2).` : "Distance between them:"}</span>
              {scalePoints.length === 2 ? (
                <>
                  <span className={styles.coordinate}>
                    <EngineeringInput value={scaleDistance} unit="mm" nullable min={0} placeholder="distance" onChange={setScaleDistance} />
                  </span>
                  <Button size="sm" variant="primary" disabled={scaleDistance === null || !(scaleDistance > 0)} onClick={applyScale}>
                    Apply
                  </Button>
                </>
              ) : null}
            </div>
          ) : null}
          <div className={styles.canvas}>
            <SceneSvg
              scene={layout.scene}
              svgRef={svgRef}
              onSpot={setHovered}
              role="application"
              aria-label="Map of the sample. Click to place the next spot; arrow keys move it by 0.1 mm, with Shift by 1 mm."
              tabIndex={canPlace ? 0 : -1}
              onPointerDown={pointerDown}
              onPointerMove={pointerMove}
              onPointerUp={pointerUp}
              onPointerCancel={pointerUp}
              onKeyDown={nudge}
              style={{ cursor: tool === "align" ? "move" : canPlace || tool === "scale" ? "crosshair" : "default", touchAction: tool === "align" ? "none" : undefined }}
            >
              {pending && scaled ? (
                <PendingMarker view={layout.view} at={pending} angleDeg={arrayAngleDeg} spacingMm={spacingMm} state={check?.state ?? "none"} />
              ) : null}
              {tool === "scale" ? <ScalePoints view={layout.view} points={scalePoints} /> : null}
            </SceneSvg>
            {hoveredSpot ? <SpotTip spot={hoveredSpot.spot} quantity={quantity} x={hoveredSpot.at.x} y={hoveredSpot.at.y} /> : null}
          </div>
          {scaled ? (
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
                {check && check.state !== "none"
                  ? `${describeClearance(check)}${check.state === "caution" ? "; the backend reports the error at Start" : ""}`
                  : pending
                    ? ""
                    : "No position: click the map, or Start without one."}
              </span>
            </div>
          ) : null}
          <div className={styles.status}>
            {outline === null ? <span className={styles.warn}>Enter the sample's dimensions in Settings ▸ Sample to draw its outline.</span> : null}
            {outline !== null && !bounded && photo === null ? (
              <span>No outline. Choose one in Settings ▸ Sample, or add a photo and set its scale, to place spots.</span>
            ) : null}
            {!bounded && photo !== null && !photo.calibrated && tool !== "scale" ? <span className={styles.warn}>Set the photo's scale to place spots.</span> : null}
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

/** The two points of a scale calibration, and the line between them. */
function ScalePoints({ view, points }: { view: Viewport; points: Point[] }) {
  const at = points.map((p) => toFigure(view, p));
  return (
    <g pointerEvents="none">
      {at.length === 2 ? (
        <>
          <line x1={at[0]!.x} y1={at[0]!.y} x2={at[1]!.x} y2={at[1]!.y} style={{ stroke: "var(--bg-inset)", strokeWidth: 4, opacity: 0.7 }} />
          <line x1={at[0]!.x} y1={at[0]!.y} x2={at[1]!.x} y2={at[1]!.y} style={{ stroke: "var(--accent)", strokeWidth: 1.5 }} />
        </>
      ) : null}
      {at.map((p, k) => (
        <circle key={k} cx={p.x} cy={p.y} r={4} style={{ fill: "var(--accent)", stroke: "var(--bg-inset)", strokeWidth: 1.5 }} />
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


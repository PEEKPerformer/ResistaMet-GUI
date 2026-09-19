import { test } from "node:test";
import assert from "node:assert/strict";
import type { MapSpot, SpotMap } from "../../generated/maps";
import { colorScale, inkOn, viridis } from "./colormap.ts";
import { defaultQuantity, edgeError, layoutFigure, PRINT_PALETTE, type FigureModel } from "./figure.ts";
import { escapeXml, sceneToSvg } from "./scene.ts";
import { SPOTS_CSV_COLUMNS, spotsCsv } from "./spotsCsv.ts";

function quantity(mean: number | null) {
  return { n: mean === null ? 0 : 20, mean, sd: 0.01, rsd_pct: 0.2, u_stat: 0.002, u_inst: 0.003, u_total: 0.0036 };
}

function spot(index: number, x: number | null, y: number | null, rs: number, sigma: number | null = null): MapSpot {
  return {
    index,
    label: `Spot ${index}`,
    x_mm: x,
    y_mm: y,
    angle_deg: 0,
    file: `17${index}_film_4pp.csv`,
    stats: { n: 20, n_excluded: 0, end_reason: "sample_count", rs: quantity(rs), rho: quantity(null), sigma: quantity(sigma) },
  };
}

function model(spots: MapSpot[], extra: Partial<FigureModel> = {}): FigureModel {
  return {
    title: "film A",
    subtitle: "Rs · 2026-09-19",
    outline: { shape: "rectangle", widthMm: 20, lengthMm: 20 },
    spacingMm: 1,
    arrayAngleDeg: 0,
    edgeWarnPct: 1,
    spots,
    quantity: "rs",
    labels: "none",
    cells: false,
    photo: null,
    palette: PRINT_PALETTE,
    ...extra,
  };
}

test("viridis runs from purple to yellow and clamps", () => {
  assert.equal(viridis(0), "#440154");
  assert.equal(viridis(1), "#fde725");
  assert.equal(viridis(0.5), "#21908c");
  assert.equal(viridis(-3), viridis(0));
  assert.equal(viridis(7), viridis(1));
  assert.equal(inkOn(viridis(0)), "#ffffff");
  assert.equal(inkOn(viridis(1)), "#000000");
});

test("a scale over one value is flat, and a missing value has its own colour", () => {
  const flat = colorScale([5, null, undefined]);
  assert.ok(flat.flat);
  assert.equal(flat.color(5), viridis(0.5));
  const scale = colorScale([1, 3]);
  assert.equal(scale.color(1), viridis(0));
  assert.equal(scale.color(3), viridis(1));
  assert.notEqual(scale.color(null), scale.color(2));
});

test("the figure shows conductivity when the map has any, else sheet resistance", () => {
  assert.equal(defaultQuantity([spot(1, 0, 0, 5)]), "rs");
  assert.equal(defaultQuantity([spot(1, 0, 0, 5), spot(2, 1, 1, 5, 1200)]), "sigma");
});

test("the edge error prefers the one about the file's own rows", () => {
  assert.equal(edgeError({ relative_error: 0.02, relative_error_rows: 0.05 }), 0.05);
  assert.equal(edgeError({ relative_error: 0.02, relative_error_rows: null }), 0.02);
  assert.equal(edgeError({}), null);
});

test("a spot up and to the right on the sample is up and to the right in the figure", () => {
  const layout = layoutFigure(model([spot(1, 0, 0, 5), spot(2, 5, 5, 6)]));
  const [centre, upperRight] = layout.placed.map((p) => p.at);
  assert.ok(upperRight!.x > centre!.x);
  assert.ok(upperRight!.y < centre!.y, "figure y grows downward");
});

test("a spot without a position is in the table, not on the map", () => {
  const layout = layoutFigure(model([spot(1, 0, 0, 5), spot(2, null, null, 6)]));
  assert.deepEqual(layout.placed.map((p) => p.spot.index), [1]);
});

test("the nearest-spot fill is drawn only when asked for, and says what it is", () => {
  const spots = [spot(1, -5, 0, 5), spot(2, 5, 0, 6)];
  const plain = sceneToSvg(layoutFigure(model(spots)).scene);
  assert.doesNotMatch(plain, /<polygon/);
  assert.doesNotMatch(plain, /Voronoi/);
  const filled = sceneToSvg(layoutFigure(model(spots, { cells: true })).scene);
  assert.match(filled, /<polygon[^>]*clip-path="url\(#map-sample\)"/);
  assert.match(filled, /nearest spot \(Voronoi cells\), not an interpolation/);
});

test("the exported SVG is a document with a scale bar and the title escaped", () => {
  const svg = sceneToSvg(layoutFigure(model([spot(1, 0, 0, 5)], { title: 'A&B <"film">' })).scene, 2);
  assert.match(svg, /^<\?xml version="1.0"/);
  assert.match(svg, /width="1280" height="920" viewBox="0 0 640 460"/);
  assert.match(svg, /A&amp;B &lt;&quot;film&quot;&gt;/);
  assert.match(svg, />5 mm</);
  assert.doesNotMatch(svg, /var\(--/, "the print palette has no CSS variables");
  assert.equal(escapeXml("a\u0001b"), "ab");
});

test("the CSV has one row per spot, the backend's numbers, and quoted labels", () => {
  const spots = [spot(1, 0, 0, 5.5), { ...spot(2, 7, -2.5, 6), label: 'edge, "left"', relative_error_rows: 0.053, edge_clearance_s: 1.5 }];
  const map: SpotMap = {
    map_id: "20260919-153000_film_ab12",
    spots,
    rs: { n: 2, mean: 5.75, sd: 0.35, rsd_pct: 6.1 },
    rho: { n: 0 },
    sigma: { n: 0 },
  };
  const csv = spotsCsv(map, { sample: "film", operator: "alice", exportedAt: new Date(Date.UTC(2026, 8, 19, 12, 0, 0)) });
  const lines = csv.trimEnd().split("\n");
  const body = lines.filter((l) => !l.startsWith("#"));
  assert.equal(body[0], SPOTS_CSV_COLUMNS.join(","));
  assert.equal(body.length, 3);
  assert.equal(body[1], "1,Spot 1,0,0,0,20,5.5,0.0036,0.2,,0.0036,,0.0036,,,171_film_4pp.csv");
  assert.match(body[2]!, /^2,"edge, ""left""",7,-2.5,0,20,6,/);
  assert.match(body[2]!, /,5.3,1.5,172_film_4pp.csv$/);
  assert.ok(lines.includes("# map_id: 20260919-153000_film_ab12"));
  assert.ok(lines.includes("# between_spots.rs: n=2 mean=5.75 sd=0.35 rsd_pct=6.1"));
  assert.ok(lines.includes("# exported_at: 2026-09-19T12:00:00.000Z"));
});

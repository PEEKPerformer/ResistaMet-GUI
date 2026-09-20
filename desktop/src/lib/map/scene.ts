// A drawing as data: the few SVG primitives the map needs.
//
// The map is laid out once, into a Scene (`figure.ts`), and the Scene is
// rendered twice: by React on the screen (`components/map/SceneSvg.tsx`) and
// as a string for the exported file (`sceneToSvg` below). The exported figure
// is therefore the map as drawn, not a second drawing that could drift.

export interface Paint {
  fill?: string;
  stroke?: string;
  strokeWidth?: number;
  opacity?: number;
  dash?: string;
  /** Id of a clip in `Scene.clips`. */
  clip?: string;
}

export type Prim =
  | ({ kind: "rect"; x: number; y: number; width: number; height: number; rx?: number } & Paint)
  | ({ kind: "circle"; cx: number; cy: number; r: number; spot?: number } & Paint)
  | ({ kind: "line"; x1: number; y1: number; x2: number; y2: number } & Paint)
  | ({ kind: "polygon"; points: [number, number][] } & Paint)
  | {
      kind: "text";
      x: number;
      y: number;
      text: string;
      size: number;
      fill: string;
      anchor?: "start" | "middle" | "end";
      weight?: number;
      /** A halo behind the glyphs, for text over a photograph. */
      halo?: string;
    }
  | {
      kind: "image";
      href: string;
      width: number;
      height: number;
      transform: string;
      clip?: string;
      opacity?: number;
    };

export type Clip =
  | { id: string; kind: "rect"; x: number; y: number; width: number; height: number }
  | { id: string; kind: "circle"; cx: number; cy: number; r: number };

export interface Scene {
  width: number;
  height: number;
  fontFamily: string;
  background: string;
  clips: Clip[];
  prims: Prim[];
}

function n(value: number): string {
  if (!Number.isFinite(value)) return "0";
  const text = value.toFixed(2).replace(/\.?0+$/, "");
  return text === "-0" || text === "" ? "0" : text;
}

export function escapeXml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    // Characters XML 1.0 cannot carry at all.
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "");
}

function paint(p: Paint): string {
  let out = ` fill="${escapeXml(p.fill ?? "none")}"`;
  if (p.stroke) out += ` stroke="${escapeXml(p.stroke)}" stroke-width="${n(p.strokeWidth ?? 1)}"`;
  if (p.dash) out += ` stroke-dasharray="${escapeXml(p.dash)}"`;
  if (p.opacity !== undefined) out += ` opacity="${n(p.opacity)}"`;
  if (p.clip) out += ` clip-path="url(#${escapeXml(p.clip)})"`;
  return out;
}

function primToSvg(p: Prim): string {
  switch (p.kind) {
    case "rect":
      return `<rect x="${n(p.x)}" y="${n(p.y)}" width="${n(p.width)}" height="${n(p.height)}"${p.rx ? ` rx="${n(p.rx)}"` : ""}${paint(p)}/>`;
    case "circle":
      return `<circle cx="${n(p.cx)}" cy="${n(p.cy)}" r="${n(p.r)}"${paint(p)}/>`;
    case "line":
      return `<line x1="${n(p.x1)}" y1="${n(p.y1)}" x2="${n(p.x2)}" y2="${n(p.y2)}"${paint(p)}/>`;
    case "polygon":
      return `<polygon points="${p.points.map(([x, y]) => `${n(x)},${n(y)}`).join(" ")}"${paint(p)}/>`;
    case "text": {
      const halo = p.halo
        ? ` stroke="${escapeXml(p.halo)}" stroke-width="3" stroke-linejoin="round" paint-order="stroke"`
        : "";
      return (
        `<text x="${n(p.x)}" y="${n(p.y)}" font-size="${n(p.size)}" fill="${escapeXml(p.fill)}"` +
        `${p.anchor ? ` text-anchor="${p.anchor}"` : ""}${p.weight ? ` font-weight="${p.weight}"` : ""}${halo}>` +
        `${escapeXml(p.text)}</text>`
      );
    }
    case "image": {
      // A clip on the image itself would be read in the image's own,
      // transformed coordinates; on a group around it, it is the figure's.
      const image =
        `<image href="${escapeXml(p.href)}" width="${n(p.width)}" height="${n(p.height)}" ` +
        `transform="${escapeXml(p.transform)}" preserveAspectRatio="none"` +
        `${p.opacity !== undefined ? ` opacity="${n(p.opacity)}"` : ""}/>`;
      return p.clip ? `<g clip-path="url(#${escapeXml(p.clip)})">${image}</g>` : image;
    }
  }
}

function clipToSvg(c: Clip): string {
  const shape =
    c.kind === "rect"
      ? `<rect x="${n(c.x)}" y="${n(c.y)}" width="${n(c.width)}" height="${n(c.height)}"/>`
      : `<circle cx="${n(c.cx)}" cy="${n(c.cy)}" r="${n(c.r)}"/>`;
  return `<clipPath id="${escapeXml(c.id)}">${shape}</clipPath>`;
}

/** A standalone SVG document. `pixelScale` sets the width and height
 *  attributes (the viewBox stays), which is what a rasteriser sizes by. */
export function sceneToSvg(scene: Scene, pixelScale = 1): string {
  const head =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${n(scene.width * pixelScale)}" height="${n(scene.height * pixelScale)}" ` +
    `viewBox="0 0 ${n(scene.width)} ${n(scene.height)}" font-family="${escapeXml(scene.fontFamily)}">`;
  const defs = scene.clips.length ? `<defs>${scene.clips.map(clipToSvg).join("")}</defs>` : "";
  const background = `<rect width="${n(scene.width)}" height="${n(scene.height)}" fill="${escapeXml(scene.background)}"/>`;
  return `<?xml version="1.0" encoding="UTF-8"?>\n${head}${defs}${background}${scene.prims.map(primToSvg).join("")}</svg>\n`;
}

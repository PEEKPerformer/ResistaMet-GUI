// A Scene (lib/map/scene.ts) on the screen. The same primitives the exported
// file is written from, as React elements; the caller's children draw on top
// in the same coordinates (the pending spot, handles).
//
// Paint goes through `style`, not attributes: the screen palette is CSS
// variables, and only style is sure to resolve them.

import type { CSSProperties, ReactNode, Ref, SVGProps } from "react";
import type { Clip, Paint, Prim, Scene } from "../../lib/map/scene";

interface Props extends Omit<SVGProps<SVGSVGElement>, "ref" | "children"> {
  scene: Scene;
  svgRef?: Ref<SVGSVGElement>;
  /** A spot marker gained or lost the pointer or the focus. */
  onSpot?: (index: number | null) => void;
  children?: ReactNode;
}

function paintStyle(p: Paint): CSSProperties {
  const style: CSSProperties = { fill: p.fill ?? "none" };
  if (p.stroke) {
    style.stroke = p.stroke;
    style.strokeWidth = p.strokeWidth ?? 1;
  }
  if (p.dash) style.strokeDasharray = p.dash;
  if (p.opacity !== undefined) style.opacity = p.opacity;
  return style;
}

function clipUrl(id: string | undefined): string | undefined {
  return id ? `url(#${id})` : undefined;
}

function renderClip(c: Clip): ReactNode {
  return (
    <clipPath key={c.id} id={c.id}>
      {c.kind === "rect" ? <rect x={c.x} y={c.y} width={c.width} height={c.height} /> : <circle cx={c.cx} cy={c.cy} r={c.r} />}
    </clipPath>
  );
}

function renderPrim(p: Prim, key: number, onSpot: Props["onSpot"]): ReactNode {
  switch (p.kind) {
    case "rect":
      return <rect key={key} x={p.x} y={p.y} width={p.width} height={p.height} rx={p.rx} style={paintStyle(p)} clipPath={clipUrl(p.clip)} pointerEvents="none" />;
    case "circle":
      if (p.spot === undefined || !onSpot) {
        return <circle key={key} cx={p.cx} cy={p.cy} r={p.r} style={paintStyle(p)} clipPath={clipUrl(p.clip)} pointerEvents="none" />;
      }
      return (
        <circle
          key={key}
          cx={p.cx}
          cy={p.cy}
          r={p.r}
          style={{ ...paintStyle(p), cursor: "help", outlineOffset: 2 }}
          tabIndex={0}
          role="img"
          aria-label={`Spot ${p.spot}`}
          onPointerEnter={() => onSpot(p.spot!)}
          onPointerLeave={() => onSpot(null)}
          onFocus={() => onSpot(p.spot!)}
          onBlur={() => onSpot(null)}
          // A click on a finished spot is a look at it, not a new position.
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => e.stopPropagation()}
        />
      );
    case "line":
      return <line key={key} x1={p.x1} y1={p.y1} x2={p.x2} y2={p.y2} style={paintStyle(p)} pointerEvents="none" />;
    case "polygon":
      return <polygon key={key} points={p.points.map(([x, y]) => `${x},${y}`).join(" ")} style={paintStyle(p)} clipPath={clipUrl(p.clip)} pointerEvents="none" />;
    case "text":
      return (
        <text
          key={key}
          x={p.x}
          y={p.y}
          textAnchor={p.anchor}
          pointerEvents="none"
          style={{
            fill: p.fill,
            fontSize: p.size,
            fontWeight: p.weight,
            ...(p.halo ? { stroke: p.halo, strokeWidth: 3, strokeLinejoin: "round", paintOrder: "stroke" } : {}),
          }}
        >
          {p.text}
        </text>
      );
    case "image":
      // The clip goes on a group: on the image it would be read in the
      // image's own, transformed coordinates.
      return (
        <g key={key} clipPath={clipUrl(p.clip)} pointerEvents="none">
          <image href={p.href} width={p.width} height={p.height} transform={p.transform} preserveAspectRatio="none" opacity={p.opacity} />
        </g>
      );
  }
}

export function SceneSvg({ scene, svgRef, onSpot, children, style, ...rest }: Props) {
  return (
    <svg
      ref={svgRef}
      viewBox={`0 0 ${scene.width} ${scene.height}`}
      style={{ display: "block", width: "100%", height: "auto", aspectRatio: `${scene.width} / ${scene.height}`, fontFamily: scene.fontFamily, ...style }}
      {...rest}
    >
      {scene.clips.length > 0 ? <defs>{scene.clips.map(renderClip)}</defs> : null}
      {scene.background !== "transparent" ? <rect width={scene.width} height={scene.height} style={{ fill: scene.background }} /> : null}
      {scene.prims.map((p, i) => renderPrim(p, i, onSpot))}
      {children}
    </svg>
  );
}

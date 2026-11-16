// The colour scale of the map: viridis.
//
// Perceptually uniform, monotonic in lightness (so it survives greyscale
// printing) and readable with the common colour-vision deficiencies.
//
// Viridis is by Stéfan van der Walt and Nathaniel Smith, made for matplotlib
// and released under CC0. These are eleven evenly spaced stops of it,
// interpolated linearly in sRGB; against the 256-entry original the
// difference is below what a marker's fill can show.

const STOPS: [number, number, number][] = [
  [0x44, 0x01, 0x54],
  [0x48, 0x25, 0x76],
  [0x41, 0x44, 0x87],
  [0x35, 0x60, 0x8d],
  [0x2a, 0x78, 0x8e],
  [0x21, 0x90, 0x8c],
  [0x22, 0xa8, 0x84],
  [0x43, 0xbf, 0x71],
  [0x7a, 0xd1, 0x51],
  [0xbb, 0xdf, 0x27],
  [0xfd, 0xe7, 0x25],
];

/** Drawn for a spot whose value is not a number. */
export const NO_VALUE_COLOR = "#8a8f98";

function hex(channel: number): string {
  return Math.round(channel).toString(16).padStart(2, "0");
}

/** The colour at `t` in [0, 1]; values outside are clamped. */
export function viridis(t: number): string {
  if (!Number.isFinite(t)) return NO_VALUE_COLOR;
  const clamped = Math.min(1, Math.max(0, t));
  const position = clamped * (STOPS.length - 1);
  const low = Math.min(Math.floor(position), STOPS.length - 2);
  const fraction = position - low;
  const a = STOPS[low]!;
  const b = STOPS[low + 1]!;
  return `#${hex(a[0] + (b[0] - a[0]) * fraction)}${hex(a[1] + (b[1] - a[1]) * fraction)}${hex(a[2] + (b[2] - a[2]) * fraction)}`;
}

/** Whether black or white text reads better on `color` (#rrggbb). */
export function inkOn(color: string): "#000000" | "#ffffff" {
  const channel = (offset: number) => {
    const c = Number.parseInt(color.slice(offset, offset + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  const luminance = 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
  return luminance > 0.35 ? "#000000" : "#ffffff";
}

export interface ColorScale {
  min: number;
  max: number;
  /** One value, or none: the scale has no extent and every spot is mid-scale. */
  flat: boolean;
  color: (value: number | null | undefined) => string;
}

/** A linear scale over the finite values. */
export function colorScale(values: (number | null | undefined)[]): ColorScale {
  const finite = values.filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  const min = finite.length ? Math.min(...finite) : NaN;
  const max = finite.length ? Math.max(...finite) : NaN;
  const flat = !(max > min);
  return {
    min,
    max,
    flat,
    color: (value) => {
      if (typeof value !== "number" || !Number.isFinite(value)) return NO_VALUE_COLOR;
      return viridis(flat ? 0.5 : (value - min) / (max - min));
    },
  };
}

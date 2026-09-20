// Number formatting for an instrument UI: engineering notation with SI
// prefixes, fixed significant figures, and honest handling of NaN.

const PREFIXES: [number, string][] = [
  [1e12, "T"],
  [1e9, "G"],
  [1e6, "M"],
  [1e3, "k"],
  [1, ""],
  [1e-3, "m"],
  [1e-6, "µ"],
  [1e-9, "n"],
  [1e-12, "p"],
  [1e-15, "f"],
];

export interface Engineering {
  /** Scaled mantissa as text, e.g. "1.234". */
  mantissa: string;
  /** SI prefix plus unit, e.g. "mΩ". */
  unit: string;
}

/** Units that take SI prefixes. Anything else — cm, µm, °C, h, %, Ω/sq — is
 *  shown at face value: "50 mcm" is not a thing. */
const PREFIXABLE = new Set(["", "Ω", "V", "A", "W", "s", "Hz", "F", "H", "S", "Ω·cm", "S/cm"]);

/** The SI prefix a magnitude is shown with, and the factor that goes with it. */
export function prefixFor(value: number, unit = ""): { scale: number; prefix: string } {
  if (!PREFIXABLE.has(unit) || !Number.isFinite(value) || value === 0) return { scale: 1, prefix: "" };
  const magnitude = Math.abs(value);
  const [scale, prefix] = PREFIXES.find(([s]) => magnitude >= s * 0.9995) ?? PREFIXES[PREFIXES.length - 1]!;
  return { scale, prefix };
}

/** Split a value into a mantissa and a prefixed unit. NaN yields "—". */
export function engineering(value: number, unit = "", digits = 4): Engineering {
  if (!Number.isFinite(value)) return { mantissa: "—", unit };
  if (value === 0) return { mantissa: "0", unit };
  const { scale, prefix } = prefixFor(value, unit);
  return { mantissa: toSignificant(value / scale, digits), unit: prefix + unit };
}

/** Axis tick labels: one prefix for the whole axis, and as many decimals as
 *  the ticks need so that none reads the same as its neighbour or as a value
 *  it is not. A
 *  resistance trace lives in the last digits (10.075 … 10.080 Ω), which four
 *  significant figures cannot show. */
export function axisLabels(ticks: number[], unit = ""): string[] {
  const largest = ticks.reduce((m, v) => Math.max(m, Math.abs(v)), 0);
  const { scale, prefix } = prefixFor(largest || 1, unit);
  const scaled = ticks.map((v) => v / scale);
  let spacing = Number.POSITIVE_INFINITY;
  for (let i = 1; i < scaled.length; i++) spacing = Math.min(spacing, Math.abs(scaled[i]! - scaled[i - 1]!));
  let decimals = Number.isFinite(spacing) && spacing > 0 ? Math.min(10, Math.max(0, Math.ceil(-Math.log10(spacing) - 1e-9))) : 0;
  // The spacing's own decimals are not always enough: ticks step by 2.5 as
  // well as 1, 2 and 5, and 2.5 at zero decimals reads "3". Add decimals
  // until every label is its tick, to within floating-point noise.
  const noise = Number.isFinite(spacing) ? spacing * 1e-6 : 0;
  while (decimals < 10 && scaled.some((v) => Math.abs(Number(v.toFixed(decimals)) - v) > noise)) decimals += 1;
  const suffix = prefix + unit ? ` ${prefix}${unit}` : "";
  return scaled.map((v) => (Math.abs(v) < 0.5 * 10 ** -decimals ? (0).toFixed(decimals) : v.toFixed(decimals)) + suffix);
}

export function formatEngineering(value: number, unit = "", digits = 4): string {
  const { mantissa, unit: u } = engineering(value, unit, digits);
  return u ? `${mantissa} ${u}` : mantissa;
}

/** Fixed significant figures without exponent notation, trailing zeros kept
 *  so a live readout does not jitter in width. */
export function toSignificant(value: number, digits: number): string {
  if (!Number.isFinite(value)) return "—";
  if (value === 0) return "0";
  const magnitude = Math.floor(Math.log10(Math.abs(value)));
  const decimals = Math.max(0, digits - 1 - magnitude);
  return value.toFixed(Math.min(decimals, 12));
}

/** Elapsed seconds as h:mm:ss or m:ss. */
export function formatElapsed(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const whole = Math.floor(seconds);
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const s = whole % 60;
  const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
  return h > 0 ? `${h}:${mm}:${String(s).padStart(2, "0")}` : `${mm}:${String(s).padStart(2, "0")}`;
}

/** Percent with one decimal, e.g. "0.3 %". */
export function formatPercent(fraction: number): string {
  if (!Number.isFinite(fraction)) return "—";
  return `${(fraction * 100).toFixed(1)} %`;
}

/** Parse what an operator typed into a numeric field: accepts engineering
 *  suffixes ("100m", "1.5k", "10u", "3 µA") and plain numbers. */
export function parseEngineering(text: string): number | null {
  const cleaned = text.trim().replace(/\s+/g, "");
  if (cleaned === "") return null;
  const match = /^([-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)([TGMkmuµnpf]?)/i.exec(cleaned);
  if (!match) return null;
  const base = Number(match[1]);
  if (!Number.isFinite(base)) return null;
  const suffix = match[2] ?? "";
  const factor: Record<string, number> = {
    T: 1e12, G: 1e9, M: 1e6, k: 1e3, m: 1e-3, u: 1e-6, "µ": 1e-6, n: 1e-9, p: 1e-12, f: 1e-15,
  };
  if (suffix === "") return base;
  // "M" and "m" differ; every other suffix is case-insensitive in practice.
  const key = suffix === "M" || suffix === "m" ? suffix : suffix.toLowerCase() === "k" ? "k" : suffix;
  return base * (factor[key] ?? 1);
}

/** A mean with its uncertainty under one prefix, the mean carried to the
 *  uncertainty's second significant figure: "5.6512 ± 0.0036 Ω/sq". Without
 *  a usable uncertainty it is the plain value. */
export function formatWithUncertainty(mean: number | null | undefined, u: number | null | undefined, unit = ""): string {
  if (typeof mean !== "number" || !Number.isFinite(mean)) return "—";
  if (typeof u !== "number" || !Number.isFinite(u) || u <= 0) return formatEngineering(mean, unit);
  const { scale, prefix } = prefixFor(Math.max(Math.abs(mean), u), unit);
  const decimals = Math.min(9, Math.max(0, 1 - Math.floor(Math.log10(u / scale))));
  const suffix = prefix + unit ? ` ${prefix}${unit}` : "";
  return `${(mean / scale).toFixed(decimals)} ± ${(u / scale).toFixed(decimals)}${suffix}`;
}

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

/** Split a value into a mantissa and a prefixed unit. NaN yields "—". */
export function engineering(value: number, unit = "", digits = 4): Engineering {
  if (!Number.isFinite(value)) return { mantissa: "—", unit };
  if (value === 0) return { mantissa: "0", unit };
  const magnitude = Math.abs(value);
  const [scale, prefix] = PREFIXES.find(([s]) => magnitude >= s * 0.9995) ?? PREFIXES[PREFIXES.length - 1]!;
  const scaled = value / scale;
  return { mantissa: toSignificant(scaled, digits), unit: prefix + unit };
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

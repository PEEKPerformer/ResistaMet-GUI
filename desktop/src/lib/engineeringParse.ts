// What an operator typed into a numeric field, read strictly.
//
// The whole string has to be a number, optionally followed by an SI prefix
// and the field's own unit: "100u", "1e-4", "0.1 mA", "3 µA". Anything else
// is refused with null rather than read as far as it makes sense — a current
// field that takes "100U" for 100 A or "1,5" for 1 A is worse than one that
// says no.

/** Power of ten for each SI prefix. Case matters: "m" is milli, "M" is mega. */
const PREFIX_EXPONENT: Record<string, number> = {
  T: 12,
  G: 9,
  M: 6,
  k: 3,
  m: -3,
  u: -6,
  "µ": -6, // µ, micro sign
  "μ": -6, // μ, Greek small mu: what a paste or a Greek keyboard gives
  n: -9,
  p: -12,
  f: -15,
};

const MICRO = ["u", "µ", "μ"];

const NUMBER = /^([-+]?)(\d+\.?\d*|\.\d+)(?:[eE]([-+]?\d+))?\s*(\S*)$/;

export interface EngineeringGrammar {
  /** The field's unit. When given it is the only unit that may be typed;
   *  without one no unit may be typed at all. */
  unit?: string | undefined;
  /** Whether the unit takes SI prefixes. "50 mcm" is not a thing. */
  prefixes?: boolean | undefined;
}

/** The spellings of a unit: a micro sign in it may be typed three ways. */
function unitSpellings(unit: string): string[] {
  const at = unit.search(/[µμ]/);
  if (at < 0) return [unit];
  return MICRO.map((micro) => unit.slice(0, at) + micro + unit.slice(at + 1));
}

/** The power of ten a suffix stands for, or null when it is not a suffix this
 *  field takes. A suffix that reads two ways (a bare "m" in a metre field) is
 *  refused rather than guessed. */
function suffixExponent(suffix: string, grammar: EngineeringGrammar): number | null {
  if (suffix === "") return 0;
  const units = grammar.unit ? unitSpellings(grammar.unit) : [];
  const readings = new Set<number>();
  if (units.includes(suffix)) readings.add(0);
  if (grammar.prefixes !== false) {
    const prefix = suffix.charAt(0);
    const exponent = PREFIX_EXPONENT[prefix];
    const rest = suffix.slice(prefix.length);
    if (exponent !== undefined && (rest === "" || units.includes(rest))) readings.add(exponent);
  }
  if (readings.size !== 1) return null;
  return [...readings][0]!;
}

/** The number typed, in the field's base unit, or null when the text is not
 *  exactly a number with an optional prefix and unit.
 *
 *  The prefix is folded into the decimal exponent and the text converted
 *  once, so "100u" is 1e-4 and not the 9.999999999999999e-05 that
 *  100 * 1e-6 gives. */
export function parseEngineering(text: string, grammar: EngineeringGrammar = {}): number | null {
  const match = NUMBER.exec(text.trim());
  if (!match) return null;
  const [, sign = "", digits = "", exponent = "0", suffix = ""] = match;
  const scale = suffixExponent(suffix, grammar);
  if (scale === null) return null;
  const typedExponent = Number.parseInt(exponent, 10);
  if (!Number.isSafeInteger(typedExponent)) return null;
  const value = Number(`${sign}${digits}e${typedExponent + scale}`);
  return Number.isFinite(value) ? value : null;
}

/** Which bound a value breaks, if any. A refused value is reported to the
 *  operator; it is never moved to the bound. */
export function outOfBounds(
  value: number,
  min: number | undefined,
  max: number | undefined,
): "below" | "above" | null {
  if (min !== undefined && value < min) return "below";
  if (max !== undefined && value > max) return "above";
  return null;
}

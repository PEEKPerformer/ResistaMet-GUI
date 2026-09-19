// A numeric field that speaks engineering notation.
//
// Shows "100.0 µA" when idle; while editing accepts what parseEngineering
// understands ("100u", "1e-4", "0.1 mA") and commits on blur or Enter. The
// value in and out is always the base SI unit the backend uses, so nothing
// upstream has to know how it was displayed.
//
// Text that is not a number, or a number outside the field's bounds, is
// never committed and never moved to the nearest bound: the text stays on
// show, marked invalid, beside the value that is still in force.

import { useEffect, useId, useState, type KeyboardEvent } from "react";
import { outOfBounds, parseEngineering } from "../../lib/engineeringParse";
import { engineering, formatEngineering, prefixFor } from "../../lib/format";
import { Input } from "./index";
import styles from "./ui.module.css";

interface Props {
  value: number | null;
  onChange: (value: number | null) => void;
  unit?: string | undefined;
  min?: number | undefined;
  max?: number | undefined;
  /** Allow an empty field to mean "no value" (nullable settings). */
  nullable?: boolean;
  disabled?: boolean;
  digits?: number;
  invalid?: boolean;
  placeholder?: string | undefined;
}

export function EngineeringInput({
  value,
  onChange,
  unit,
  min,
  max,
  nullable = false,
  disabled = false,
  digits = 4,
  invalid = false,
  placeholder,
}: Props) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  /** Why the text on show was refused; null when the field shows its value. */
  const [refused, setRefused] = useState<string | null>(null);
  const errorId = useId();

  useEffect(() => {
    if (!editing && refused === null) setDraft(value === null ? "" : trimZeros(value));
  }, [value, editing, refused]);

  // A value that arrives from outside replaces whatever was refused.
  useEffect(() => setRefused(null), [value]);

  const commit = () => {
    setEditing(false);
    const text = draft.trim();
    if (text === "") {
      setRefused(null);
      if (nullable) onChange(null);
      return; // otherwise leave the previous value; the effect restores it
    }
    // A unit takes a prefix here exactly when the display gives it one.
    const prefixes = prefixFor(1e3, unit ?? "").prefix !== "";
    const parsed = parseEngineering(text, { unit, prefixes });
    if (parsed === null) {
      setRefused(unit ? `Not a number in ${unit}` : "Not a number");
      return;
    }
    const bound = outOfBounds(parsed, min, max);
    if (bound !== null) {
      setRefused(boundText(bound, min, max, unit ?? ""));
      return;
    }
    setRefused(null);
    onChange(parsed);
  };

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.currentTarget.blur();
    } else if (e.key === "Escape") {
      setEditing(false);
      e.currentTarget.blur();
    }
  };

  // Idle: mantissa and prefixed unit come from one formatting call, so the
  // prefix in the suffix always matches the number beside it. Editing: the
  // operator's text, verbatim.
  const idle = value === null ? null : engineering(value, unit ?? "", digits);
  const verbatim = editing || refused !== null;
  const shown = verbatim ? draft : idle ? idle.mantissa : "";
  const idleUnit = verbatim || idle === null ? unit : idle.unit || unit;
  const showRefusal = refused !== null && !editing;

  return (
    <>
      <Input
        value={shown}
        unit={idleUnit ?? undefined}
        disabled={disabled}
        invalid={invalid || showRefusal}
        aria-invalid={invalid || showRefusal || undefined}
        aria-describedby={showRefusal ? errorId : undefined}
        placeholder={placeholder}
        inputMode="decimal"
        onFocus={() => {
          setEditing(true);
          // Refused text stays so it can be corrected rather than retyped.
          if (refused === null) setDraft(value === null ? "" : trimZeros(value));
        }}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={onKey}
      />
      {showRefusal ? (
        <span className={styles.fieldError} id={errorId}>
          {refused} — still {value === null ? "unset" : formatEngineering(value, unit ?? "", digits)}
        </span>
      ) : null}
    </>
  );
}

function boundText(bound: "below" | "above", min: number | undefined, max: number | undefined, unit: string): string {
  if (bound === "above") return `Above ${formatEngineering(max ?? Number.NaN, unit)}`;
  // A form passes "greater than zero" as the smallest step above it.
  if (min !== undefined && min > 0 && min <= Number.EPSILON) return "Must be above 0";
  return `Below ${formatEngineering(min ?? Number.NaN, unit)}`;
}

function trimZeros(value: number): string {
  // Plain text for editing: the base-unit number, shortest faithful form.
  return String(Number(value.toPrecision(10)));
}

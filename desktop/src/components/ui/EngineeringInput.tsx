// A numeric field that speaks engineering notation.
//
// Shows "100.0 µA" when idle; while editing accepts anything parseEngineering
// understands ("100u", "1e-4", "0.1m") and commits on blur or Enter. The value
// in and out is always the base SI unit the backend uses, so nothing upstream
// has to know how it was displayed.

import { useEffect, useState, type KeyboardEvent } from "react";
import { engineering, parseEngineering } from "../../lib/format";
import { Input } from "./index";

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

  useEffect(() => {
    if (!editing) setDraft(value === null ? "" : trimZeros(value));
  }, [value, editing]);

  const commit = () => {
    setEditing(false);
    const text = draft.trim();
    if (text === "") {
      if (nullable) onChange(null);
      return; // otherwise leave the previous value; the effect restores it
    }
    const parsed = parseEngineering(text);
    if (parsed === null) return;
    let next = parsed;
    if (min !== undefined && next < min) next = min;
    if (max !== undefined && next > max) next = max;
    onChange(next);
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
  const shown = editing ? draft : idle ? idle.mantissa : "";
  const idleUnit = editing || idle === null ? unit : idle.unit || unit;

  return (
    <Input
      value={shown}
      unit={idleUnit ?? undefined}
      disabled={disabled}
      invalid={invalid}
      placeholder={placeholder}
      inputMode="decimal"
      onFocus={() => {
        setEditing(true);
        setDraft(value === null ? "" : trimZeros(value));
      }}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={onKey}
    />
  );
}

function trimZeros(value: number): string {
  // Plain text for editing: the base-unit number, shortest faithful form.
  return String(Number(value.toPrecision(10)));
}

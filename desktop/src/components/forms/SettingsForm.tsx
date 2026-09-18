// Renders a mode's settings from two sources of truth: FIELD_META (bounds,
// enums, defaults — generated from the backend schema) and fields.ts (labels,
// units, grouping — the UI's presentation table). Nothing here knows what a
// field means; it only knows how to edit its type.

import { FIELD_META, MODE_MODEL, type Mode } from "../../generated/settings";
import type { FieldGroup, FieldSpec } from "../../lib/fields";
import type { Issue } from "../../lib/api";
import { EngineeringInput } from "../ui/EngineeringInput";
import { Field, Input, Select, SectionTitle, Toggle } from "../ui";

export type Overrides = Record<string, unknown>;

interface Props {
  mode: Mode;
  groups: FieldGroup[];
  values: Overrides;
  onChange: (key: string, value: unknown) => void;
  issues?: Issue[];
  disabled?: boolean;
}

export function SettingsForm({ mode, groups, values, onChange, issues = [], disabled = false }: Props) {
  const model = MODE_MODEL[mode];
  const meta = FIELD_META[model] ?? {};
  const issueFor = (key: string) => issues.find((i) => i.key === key);

  return (
    <>
      {groups.map((group) => (
        <div key={group.title}>
          <SectionTitle>{group.title}</SectionTitle>
          {group.fields.map((spec) => (
            <FieldRow
              key={spec.key}
              spec={spec}
              meta={meta[spec.key] ?? {}}
              value={values[spec.key]}
              onChange={(v) => onChange(spec.key, v)}
              issue={issueFor(spec.key)}
              disabled={disabled}
            />
          ))}
        </div>
      ))}
    </>
  );
}

interface RowProps {
  spec: FieldSpec;
  meta: (typeof FIELD_META)[string][string];
  value: unknown;
  onChange: (value: unknown) => void;
  issue?: Issue | undefined;
  disabled: boolean;
}

export function FieldRow({ spec, meta, value, onChange, issue, disabled }: RowProps) {
  const error = issue ? issue.message : undefined;

  if (meta.enum) {
    return (
      <Field label={spec.label} hint={spec.hint} error={error}>
        <Select value={String(value ?? meta.default ?? "")} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
          {meta.enum.map((option) => (
            <option key={String(option)} value={String(option)}>
              {spec.options?.[String(option)] ?? String(option)}
            </option>
          ))}
        </Select>
      </Field>
    );
  }

  if (meta.type === "boolean") {
    return (
      <Field label={spec.label} hint={spec.hint} error={error}>
        <Toggle checked={Boolean(value ?? meta.default)} disabled={disabled} onChange={onChange} label={spec.label} />
      </Field>
    );
  }

  if (meta.type === "string") {
    // Free text: an address, a driver name, a directory. Never the numeric
    // input, whose formatter would throw on a string mid-render.
    return (
      <Field label={spec.label} hint={spec.hint} error={error} stacked>
        <Input
          className="mono"
          value={typeof value === "string" ? value : String(meta.default ?? "")}
          disabled={disabled}
          invalid={issue !== undefined}
          onChange={(e) => onChange(e.target.value)}
        />
      </Field>
    );
  }

  if (meta.type === "integer") {
    // Counts are counts: no prefixes, no decimals.
    const current = typeof value === "number" ? value : ((meta.default as number | undefined) ?? 0);
    return (
      <Field label={spec.label} hint={spec.hint} error={error}>
        <Input
          type="number"
          inputMode="numeric"
          step={1}
          min={meta.min}
          max={meta.max}
          value={String(current)}
          unit={spec.unit}
          disabled={disabled}
          invalid={issue !== undefined}
          onChange={(e) => {
            const parsed = Number.parseInt(e.target.value, 10);
            if (Number.isFinite(parsed)) onChange(parsed);
          }}
        />
      </Field>
    );
  }

  // number (possibly nullable)
  const numeric = typeof value === "number" && Number.isFinite(value) ? value : null;
  const lower = meta.exclusiveMin !== undefined ? meta.exclusiveMin + Number.EPSILON : meta.min;
  return (
    <Field label={spec.label} hint={spec.hint} error={error}>
      <EngineeringInput
        value={numeric ?? (meta.nullable ? null : ((meta.default as number | undefined) ?? 0))}
        min={lower}
        max={meta.max}
        unit={spec.unit}
        nullable={Boolean(meta.nullable)}
        disabled={disabled}
        invalid={issue !== undefined}
        placeholder={meta.nullable ? "—" : undefined}
        onChange={onChange}
      />
    </Field>
  );
}

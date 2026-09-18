// Profile settings: the knobs that live with the operator rather than on a
// tab. Edits go straight to PATCH /profiles/{user} on Save, one section at a
// time, so the backend's validation is the only validation.
//
// The instrument address is machine-local and shown apart from the profile:
// the same operator's profile on another PC has a different bus.

import { useEffect, useMemo, useState } from "react";
import { useApi } from "../../app/AppContext";
import { FIELD_META } from "../../generated/settings";
import type { FieldSpec } from "../../lib/fields";
import type { InstrumentInfo, Profile } from "../../lib/api";
import { ApiError } from "../../lib/api";
import { useSession } from "../../state/session";
import { setTheme, useUi } from "../../state/ui";
import { FieldRow } from "../forms/SettingsForm";
import { Button, Dialog, Field, Input, Notice, Select } from "../ui";
import styles from "./dialogs.module.css";

type Section = "timing" | "instrument" | "aux" | "safety" | "files" | "display";

const SECTIONS: { id: Section; label: string }[] = [
  { id: "timing", label: "Timing" },
  { id: "instrument", label: "Instrument" },
  { id: "aux", label: "Aux sensor" },
  { id: "safety", label: "Safety" },
  { id: "files", label: "Files & output" },
  { id: "display", label: "Display" },
];

const TIMING: FieldSpec[] = [
  { key: "nplc", label: "NPLC", hint: "Power-line cycles per reading. Tabs may override." },
  { key: "sampling_rate", label: "Sampling rate", unit: "Hz" },
  { key: "settling_time", label: "Settle before first reading", unit: "s" },
  { key: "auto_zero", label: "Auto zero", options: { on: "On", once: "Once", off: "Off" } },
  { key: "filter_enabled", label: "Hardware filter" },
  { key: "filter_type", label: "Filter type", options: { repeat: "Repeat", moving: "Moving" } },
  { key: "filter_count", label: "Filter count" },
  { key: "stop_on_compliance", label: "Stop on compliance" },
];

const AUX: FieldSpec[] = [
  { key: "aux_log_enabled", label: "Co-log an auxiliary sensor" },
  { key: "aux_driver", label: "Driver" },
  { key: "aux_address", label: "Address", hint: "VISA/serial resource, e.g. ASRL6::INSTR." },
];

const SAFETY: FieldSpec[] = [
  { key: "safety_voltage_warn_v", label: "Warn at or above", unit: "V", hint: "0 disables the warning." },
  { key: "safety_voltage_warn_silenced", label: "Warning silenced for this profile" },
];

const FILES: FieldSpec[] = [
  { key: "data_directory", label: "Data directory" },
  { key: "auto_save_interval", label: "Flush interval", unit: "s" },
];

const OUTPUT: FieldSpec[] = [
  { key: "format", label: "Format", options: { csv: "CSV", hdf5: "HDF5", "csv+legacy_json": "CSV + legacy JSON" } },
  { key: "compression", label: "Compression", options: { never: "Never", always: "Always", auto: "Above a size" } },
  { key: "compression_threshold_mb", label: "Compress above", unit: "MB" },
];

interface Props {
  onClose: () => void;
}

export function SettingsDialog({ onClose }: Props) {
  const api = useApi();
  const ui = useUi();
  const session = useSession();
  const [section, setSection] = useState<Section>("timing");
  const [profile, setProfile] = useState<Profile | null>(null);
  const [draft, setDraft] = useState<Profile>({});
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const running = session.status !== null && session.status.state !== "idle";

  useEffect(() => {
    if (!ui.username) return;
    api
      .profile(ui.username)
      .then((p) => {
        setProfile(p);
        setDraft(structuredClone(p));
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [api, ui.username]);

  const dirtySections = useMemo(() => {
    if (!profile) return [];
    return (Object.keys(draft) as (keyof Profile)[]).filter(
      (s) => JSON.stringify(draft[s]) !== JSON.stringify(profile[s]),
    );
  }, [draft, profile]);

  const set = (sectionName: string, key: string, value: unknown) =>
    setDraft((d) => ({ ...d, [sectionName]: { ...(d[sectionName] ?? {}), [key]: value } }));

  const save = async () => {
    if (!ui.username) return;
    setSaving(true);
    setError(null);
    try {
      const patch: Partial<Profile> = {};
      for (const s of dirtySections) patch[s] = draft[s];
      const updated = await api.patchProfile(ui.username, patch);
      setProfile(updated);
      setDraft(structuredClone(updated));
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSaving(false);
    }
  };

  const measurement = draft.measurement ?? {};
  const meta = (model: string) => FIELD_META[model] ?? {};

  return (
    <Dialog
      title={`Settings — ${ui.username ?? ""}`}
      size="lg"
      onClose={onClose}
      footer={
        <>
          {saved ? <span className={styles.savedNote}>Saved</span> : null}
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
          <Button variant="primary" disabled={saving || dirtySections.length === 0} onClick={() => void save()}>
            Save {dirtySections.length > 0 ? `(${dirtySections.length})` : ""}
          </Button>
        </>
      }
    >
      <div className={styles.settingsLayout}>
        <nav className={styles.settingsNav}>
          {SECTIONS.map((s) => (
            <button
              key={s.id}
              type="button"
              className={styles.settingsNavItem}
              aria-current={section === s.id ? "page" : undefined}
              onClick={() => setSection(s.id)}
            >
              {s.label}
            </button>
          ))}
        </nav>
        <div className={styles.settingsBody}>
          {error ? <Notice tone="danger">{error}</Notice> : null}
          {profile === null && !error ? <div className={styles.muted}>Loading…</div> : null}

          {profile && section === "timing"
            ? TIMING.map((spec) => (
                <FieldRow
                  key={spec.key}
                  spec={spec}
                  meta={meta("InstrumentSettings")[spec.key] ?? {}}
                  value={measurement[spec.key]}
                  onChange={(v) => set("measurement", spec.key, v)}
                  disabled={running}
                />
              ))
            : null}

          {profile && section === "instrument" ? <InstrumentSection running={running} /> : null}

          {profile && section === "aux"
            ? AUX.map((spec) => (
                <FieldRow
                  key={spec.key}
                  spec={spec}
                  meta={meta("AuxSensorSettings")[spec.key] ?? {}}
                  value={measurement[spec.key]}
                  onChange={(v) => set("measurement", spec.key, v)}
                  disabled={running}
                />
              ))
            : null}

          {profile && section === "safety"
            ? SAFETY.map((spec) => (
                <FieldRow
                  key={spec.key}
                  spec={spec}
                  meta={meta("SafetySettings")[spec.key] ?? {}}
                  value={measurement[spec.key]}
                  onChange={(v) => set("measurement", spec.key, v)}
                  disabled={running}
                />
              ))
            : null}

          {profile && section === "files" ? (
            <>
              {FILES.map((spec) => (
                <FieldRow
                  key={spec.key}
                  spec={spec}
                  meta={meta("FileSettings")[spec.key] ?? {}}
                  value={(draft.file ?? {})[spec.key]}
                  onChange={(v) => set("file", spec.key, v)}
                  disabled={running}
                />
              ))}
              <div className={styles.subhead}>Output</div>
              {OUTPUT.map((spec) => (
                <FieldRow
                  key={spec.key}
                  spec={spec}
                  meta={meta("OutputSettings")[spec.key] ?? {}}
                  value={(draft.output ?? {})[spec.key]}
                  onChange={(v) => set("output", spec.key, v)}
                  disabled={running}
                />
              ))}
            </>
          ) : null}

          {section === "display" ? (
            <Field label="Theme" hint="Stored on this computer, not in the profile.">
              <Select value={ui.theme} onChange={(e) => setTheme(e.target.value as typeof ui.theme)}>
                <option value="dark">Dark</option>
                <option value="light">Light</option>
                <option value="system">Follow system</option>
              </Select>
            </Field>
          ) : null}
        </div>
      </div>
    </Dialog>
  );
}

/** Address is machine-local; identifying touches the bus, so it is refused
 *  while a run holds it and the backend says so. */
function InstrumentSection({ running }: { running: boolean }) {
  const api = useApi();
  const ui = useUi();
  const [address, setAddress] = useState("");
  const [resources, setResources] = useState<string[] | null>(null);
  const [info, setInfo] = useState<InstrumentInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!ui.username) return;
    api
      .profile(ui.username)
      .then((p) => setAddress(String((p.measurement ?? {}).gpib_address ?? "")))
      .catch(() => undefined);
  }, [api, ui.username]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Field label="Address" hint="Machine-local: saved for this PC, not carried with the profile." stacked>
        <div className={styles.addressRow}>
          <Input className="mono" value={address} onChange={(e) => setAddress(e.target.value)} disabled={running} list="visa-resources" />
          <datalist id="visa-resources">{(resources ?? []).map((r) => <option key={r} value={r} />)}</datalist>
          <Button
            disabled={busy || running}
            onClick={() => void run(async () => setResources((await api.resources()).resources))}
          >
            Scan
          </Button>
          <Button
            disabled={busy || running || address.trim() === ""}
            onClick={() =>
              void run(async () => {
                setInfo(await api.identify(address.trim()));
                if (ui.username) await api.patchProfile(ui.username, { measurement: { gpib_address: address.trim() } });
              })
            }
          >
            Identify & save
          </Button>
        </div>
      </Field>
      {resources !== null && resources.length === 0 ? <div className={styles.muted}>VISA sees no resources.</div> : null}
      {info ? (
        <div className={styles.instrumentInfo}>
          <div>
            <strong>Keithley {info.model ?? "?"}</strong>
          </div>
          <div className="mono">{info.idn}</div>
          {info.max_source_v !== null ? (
            <div className={styles.muted}>
              max {info.max_source_v} V · {info.max_source_i} A · {info.max_power_w} W
            </div>
          ) : null}
        </div>
      ) : null}
      {error ? <Notice tone="danger">{error}</Notice> : null}
    </>
  );
}

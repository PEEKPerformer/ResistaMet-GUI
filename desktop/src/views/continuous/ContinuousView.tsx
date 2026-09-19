// The view for the four continuous modes: resistance, voltage source, current
// source and four-point probe. One component, parameterised by mode, because
// they share everything but which quantities are sourced and shown.
//
// Layout: controls across the top, live readout, the plot filling the rest,
// and the settings panel on the right. Settings are the operator's tab values
// on top of the profile; the backend resolves them (with issues and the
// hazard check) as they change, so Start is only offered for a run the backend
// will accept.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApi } from "../../app/AppContext";
import { FIELD_META, MODE_MODEL, type Mode } from "../../generated/settings";
import { MODE_FIELDS, MODE_LABEL, MODE_TIMING, TIMING_FIELDS } from "../../lib/fields";
import type { Resolved } from "../../lib/api";
import { ApiError } from "../../lib/api";
import { NAME_THE_SAMPLE } from "../../lib/copy";
import { formatElapsed, formatEngineering } from "../../lib/format";
import { useSession } from "../../state/session";
import { useLatestSample } from "../../state/samples";
import { useUi } from "../../state/ui";
import { seedOverrides, setOverride, useOverrides } from "../../state/overrides";
import { Badge, Button, Notice, Panel } from "../../components/ui";
import { BackendNotice } from "../../components/BackendNotice";
import { Icons } from "../../components/icons";
import { LivePlot, type TraceSpec } from "../../components/plot/LivePlot";
import { FieldRow, SettingsForm } from "../../components/forms/SettingsForm";
import { FourPointPanel } from "./FourPointPanel";
import styles from "./ContinuousView.module.css";

type ContinuousMode = Exclude<Mode, "sweep" | "vdp">;

interface ReadoutSpec {
  key: string;
  label: string;
  unit: string;
  primary?: boolean;
  color: string;
}

const READOUTS: Record<ContinuousMode, ReadoutSpec[]> = {
  resistance: [
    { key: "resistance", label: "R", unit: "Ω", primary: true, color: "var(--data-r)" },
    { key: "voltage", label: "V", unit: "V", color: "var(--data-v)" },
    { key: "current", label: "I", unit: "A", color: "var(--data-i)" },
  ],
  source_v: [
    { key: "current", label: "I", unit: "A", primary: true, color: "var(--data-i)" },
    { key: "voltage", label: "V", unit: "V", color: "var(--data-v)" },
  ],
  source_i: [
    { key: "voltage", label: "V", unit: "V", primary: true, color: "var(--data-v)" },
    { key: "current", label: "I", unit: "A", color: "var(--data-i)" },
  ],
  four_point: [
    { key: "voltage", label: "V", unit: "V", primary: true, color: "var(--data-v)" },
    { key: "current", label: "I", unit: "A", color: "var(--data-i)" },
  ],
};

const TRACES: Record<ContinuousMode, TraceSpec[]> = {
  resistance: [{ key: "resistance", label: "R", unit: "Ω", color: "var(--data-r)" }],
  source_v: [
    { key: "current", label: "I", unit: "A", color: "var(--data-i)" },
    { key: "voltage", label: "V", unit: "V", color: "var(--data-v)", rightAxis: true },
  ],
  source_i: [
    { key: "voltage", label: "V", unit: "V", color: "var(--data-v)" },
    { key: "current", label: "I", unit: "A", color: "var(--data-i)", rightAxis: true },
  ],
  four_point: [
    { key: "voltage", label: "V", unit: "V", color: "var(--data-v)" },
    { key: "current", label: "I", unit: "A", color: "var(--data-i)", rightAxis: true },
  ],
};

export const STATE_LABEL: Record<string, string> = {
  running: "Measuring",
  paused: "Paused",
  awaiting_prompt: "Waiting for you",
  stopping: "Stopping",
  identifying: "Identifying",
};

const WINDOWS: { label: string; seconds: number }[] = [
  { label: "30 s", seconds: 30 },
  { label: "5 min", seconds: 300 },
  { label: "1 h", seconds: 3600 },
  { label: "All", seconds: 0 },
];

export function ContinuousView({ mode }: { mode: ContinuousMode }) {
  const api = useApi();
  const session = useSession();
  const ui = useUi();
  const overrides = useOverrides(mode);
  const [resolved, setResolved] = useState<Resolved | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const [windowS, setWindowS] = useState(300);
  const [busy, setBusy] = useState(false);

  const status = session.status;
  const thisModeRunning = status !== null && status.state !== "idle" && status.mode === mode;
  const otherModeRunning = status !== null && status.state !== "idle" && status.mode !== mode;
  const locked = thisModeRunning || otherModeRunning;

  // Seed the tab from the profile once per (user, mode).
  const model = MODE_MODEL[mode];
  const fieldKeys = useMemo(
    () => [...Object.keys(FIELD_META[model] ?? {}), ...MODE_TIMING[mode]],
    [model, mode],
  );
  useEffect(() => {
    if (!ui.username) return;
    api
      .profile(ui.username)
      .then((profile) => seedOverrides(mode, fieldKeys, profile.measurement ?? {}))
      .catch(() => undefined);
  }, [api, ui.username, mode, fieldKeys]);

  // Resolve on change, debounced: the backend tells us what the run would use
  // and what is wrong with it.
  const resolveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (!ui.username) return;
    if (resolveTimer.current) clearTimeout(resolveTimer.current);
    resolveTimer.current = setTimeout(() => {
      api
        .resolve(mode, ui.username!, overrides, true)
        .then(setResolved)
        .catch(() => setResolved(null));
    }, 150);
    return () => {
      if (resolveTimer.current) clearTimeout(resolveTimer.current);
    };
  }, [api, mode, ui.username, overrides]);

  const onChange = useCallback((key: string, value: unknown) => setOverride(mode, key, value), [mode]);

  // M marks the moment, as in the PySide6 app — unless the operator is typing.
  useEffect(() => {
    if (!thisModeRunning) return;
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
      if (e.key === "m" || e.key === "M") void api.mark();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [api, thisModeRunning]);

  const canStart =
    session.backendReachable === true && !locked && ui.username !== null && ui.sampleName.trim() !== "" && resolved !== null && resolved.ok && !busy;

  const start = async () => {
    if (!ui.username) return;
    setBusy(true);
    setStartError(null);
    try {
      await api.start({ mode, sample_name: ui.sampleName.trim(), username: ui.username, overrides });
    } catch (e) {
      setStartError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  const command = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      setStartError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  const errors = resolved?.issues.filter((i) => i.severity === "error") ?? [];
  const hazard = resolved?.hazard?.hazardous ? resolved.hazard : null;
  const maxRate = typeof resolved?.derived.max_rate_hz === "number" ? resolved.derived.max_rate_hz : null;
  const requestedRate = typeof overrides.sampling_rate === "number" ? overrides.sampling_rate : null;
  const rateTooHigh = maxRate !== null && requestedRate !== null && requestedRate > maxRate;
  // The achievable rate is stated once: in the banner when the request
  // exceeds it, as a quiet line under the timing fields otherwise.
  const rateBanner = rateTooHigh && !locked;

  return (
    <div className={styles.view}>
      <div className={styles.workspace}>
        <header className={styles.controls}>
          <div className={styles.title}>
            <h1 title={MODE_LABEL[mode]}>{MODE_LABEL[mode]}</h1>
            <RunState mode={mode} />
          </div>
          <div className={styles.buttons}>
            {!thisModeRunning ? (
              <Button variant="primary" size="lg" disabled={!canStart} onClick={() => void start()}>
                <Icons.play /> Start
              </Button>
            ) : (
              <>
                {status?.state === "paused" ? (
                  <Button size="lg" disabled={busy} onClick={() => void command(() => api.resume())}>
                    <Icons.play /> Resume
                  </Button>
                ) : (
                  <Button size="lg" disabled={busy} onClick={() => void command(() => api.pause())}>
                    <Icons.pause /> Pause
                  </Button>
                )}
                <Button size="lg" disabled={busy} onClick={() => void command(() => api.mark())} title="Mark this moment in the data (M)">
                  <Icons.flag /> Mark
                </Button>
                <Button variant="danger" size="lg" disabled={busy} onClick={() => void command(() => api.stop())}>
                  <Icons.stop /> Stop
                </Button>
              </>
            )}
          </div>
        </header>

        <BackendNotice />
        {startError ? <Notice tone="danger">{startError}</Notice> : null}
        {otherModeRunning ? (
          <Notice tone="info">A {MODE_LABEL[status!.mode!]} run is in progress. Stop it before starting another.</Notice>
        ) : null}
        {ui.sampleName.trim() === "" && !locked ? <Notice tone="info">{NAME_THE_SAMPLE}</Notice> : null}
        {hazard && !locked ? (
          <Notice tone="warn">
            {hazard.reason} = {hazard.voltage_v} V is at or above the {hazard.threshold_v} V touch-safety threshold. You will be asked to
            acknowledge before the output turns on.
          </Notice>
        ) : null}
        {rateBanner ? (
          <Notice tone="warn">
            {requestedRate} Hz exceeds what these timing settings can deliver (~{maxRate!.toFixed(1)} Hz). The run will sample as fast as it can.
          </Notice>
        ) : null}

        <Readout mode={mode} final={!thisModeRunning} />

        <Panel
          className={styles.plotPanel}
          bodyClassName={styles.plotBody}
          title="Live"
          actions={
            <div className={styles.windowPicker} role="group" aria-label="Time window">
              {WINDOWS.map((w) => (
                <button
                  key={w.seconds}
                  type="button"
                  data-active={w.seconds === windowS}
                  onClick={() => setWindowS(w.seconds)}
                >
                  {w.label}
                </button>
              ))}
            </div>
          }
        >
          <LivePlot traces={TRACES[mode]} mode={mode} windowS={windowS} />
        </Panel>

        {mode === "four_point" ? <FourPointPanel running={thisModeRunning} /> : null}
      </div>

      <aside className={styles.settings}>
        <Panel title="Settings" bodyClassName={styles.settingsBody}>
          <SettingsForm
            mode={mode}
            groups={MODE_FIELDS[mode]}
            values={overrides}
            onChange={onChange}
            issues={resolved?.issues ?? []}
            disabled={locked}
          />
          <div className={styles.sectionTitle}>Timing</div>
          {TIMING_FIELDS.filter((f) => MODE_TIMING[mode].includes(f.key)).map((spec) => (
            <FieldRow
              key={spec.key}
              spec={spec}
              meta={FIELD_META.InstrumentSettings?.[spec.key] ?? {}}
              value={overrides[spec.key]}
              onChange={(v) => onChange(spec.key, v)}
              issue={resolved?.issues.find((i) => i.key === spec.key)}
              disabled={locked}
            />
          ))}
          {maxRate !== null && !rateBanner ? (
            <div className={styles.derived}>
              Max rate with these settings: <span className="num">{maxRate.toFixed(1)} Hz</span>
            </div>
          ) : null}
          {errors.length > 0 && !locked ? (
            <div className={styles.issues}>
              {errors.map((i) => (
                <div key={i.key}>
                  <span className="mono">{i.key}</span>: {i.message}
                </div>
              ))}
            </div>
          ) : null}
        </Panel>
      </aside>
    </div>
  );
}

function RunState({ mode }: { mode: Mode }) {
  const { status, lastRunEnded } = useSession();
  const sample = useLatestSample();
  const latest = sample && sample.mode === mode ? sample : null;
  if (status && status.state !== "idle" && status.mode === mode) {
    const tone = status.state === "running" ? "ok" : "warn";
    return (
      <span className={styles.runState}>
        <Badge tone={tone}>{STATE_LABEL[status.state] ?? status.state}</Badge>
        {latest ? (
          <span className={`${styles.runMeta} num`}>
            {formatElapsed(latest.elapsedS)} · {latest.count} samples
          </span>
        ) : null}
      </span>
    );
  }
  if (lastRunEnded && status?.mode === mode) {
    // The run's totals stay on screen until the next Start: the backend's
    // own figures when it sent them, the last sample's otherwise.
    const durationS = lastRunEnded.durationS ?? latest?.elapsedS ?? null;
    const samples = lastRunEnded.samples ?? latest?.count ?? null;
    return (
      <span className={styles.runState}>
        <Badge tone={lastRunEnded.ok ? undefined : "danger"}>ended: {lastRunEnded.reason.replace(/_/g, " ")}</Badge>
        {durationS !== null && samples !== null ? (
          <span className={`${styles.runMeta} num`}>
            {formatElapsed(durationS)} · {samples} samples
          </span>
        ) : null}
      </span>
    );
  }
  return <Badge>Idle</Badge>;
}

/** `final`: no run of this mode is going, so whatever is shown is the last
 *  run's closing reading, not a live one — dimmed and tagged to say so. */
function Readout({ mode, final }: { mode: ContinuousMode; final: boolean }) {
  const sample = useLatestSample();
  // Another mode's samples are not this view's numbers.
  const latest = sample && sample.mode === mode ? sample : null;
  const stale = final && latest !== null;
  const specs = READOUTS[mode];
  const derived = mode === "four_point" ? latest?.derived : null;
  return (
    <div className={styles.readout} data-final={stale || undefined}>
      {specs.map((spec) => {
        const raw = latest?.values[spec.key];
        const value = typeof raw === "number" ? raw : NaN;
        return (
          <div key={spec.key} className={styles.readoutCell} data-primary={spec.primary}>
            <span className={styles.readoutLabel} style={{ color: spec.color }}>
              {spec.label}
            </span>
            <span className={`${styles.readoutValue} num`}>{formatEngineering(value, spec.unit, spec.primary ? 5 : 4)}</span>
          </div>
        );
      })}
      {derived ? (
        <>
          <DerivedCell label="Rs" value={derived.rs} unit="Ω/sq" />
          <DerivedCell label="ρ" value={derived.rho} unit="Ω·cm" />
          <DerivedCell label="σ" value={derived.sigma} unit="S/cm" />
        </>
      ) : null}
      {latest && latest.compliance !== "OK" ? (
        <div className={styles.readoutCell}>
          <Badge tone="danger">{latest.compliance === "V_COMP" ? "Voltage compliance" : "Current compliance"}</Badge>
        </div>
      ) : null}
      {stale ? (
        <div className={styles.readoutFinal}>
          <Badge>final</Badge>
        </div>
      ) : null}
    </div>
  );
}

function DerivedCell({ label, value, unit }: { label: string; value: unknown; unit: string }) {
  const n = typeof value === "number" ? value : NaN;
  return (
    <div className={styles.readoutCell}>
      <span className={styles.readoutLabel} style={{ color: "var(--data-derived)" }}>
        {label}
      </span>
      <span className={`${styles.readoutValue} num`}>{formatEngineering(n, unit)}</span>
    </div>
  );
}

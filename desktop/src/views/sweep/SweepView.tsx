// The I-V sweep: configure, run once, look at the curve.
//
// Unlike the continuous modes there is no stream of samples; the instrument
// runs the sweep itself and returns every point at once, one segment per
// direction. The view shows the curve, the point count, and a least-squares
// resistance across all points as a quick sanity check on an ohmic sample.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApi } from "../../app/AppContext";
import { FIELD_META, type Mode } from "../../generated/settings";
import { MODE_FIELDS, MODE_LABEL, MODE_TIMING, TIMING_FIELDS } from "../../lib/fields";
import { ApiError, type Resolved } from "../../lib/api";
import { NAME_THE_SAMPLE } from "../../lib/copy";
import { formatEngineering } from "../../lib/format";
import { useSession } from "../../state/session";
import { fitResistance, useSweep } from "../../state/sweep";
import { useUi } from "../../state/ui";
import { seedOverrides, setOverride, useOverrides } from "../../state/overrides";
import { Badge, Button, Notice, Panel } from "../../components/ui";
import { BackendNotice } from "../../components/BackendNotice";
import { Icons } from "../../components/icons";
import { XYPlot, type XYSeries } from "../../components/plot/XYPlot";
import { FieldRow, SettingsForm } from "../../components/forms/SettingsForm";
import styles from "../continuous/ContinuousView.module.css";
import own from "./SweepView.module.css";

const MODE: Mode = "sweep";

/** Conservative sweeps per source, applied when the source is switched. The
 *  voltage set is the profile default; the current set is its mirror at the
 *  scale a 2400 sources into an unknown DUT without drama. */
const SWEEP_FOR_VOLTAGE = { sweep_start: 0.0, sweep_stop: 1.0, sweep_step: 0.05, sweep_compliance: 0.1 };
const SWEEP_FOR_CURRENT = { sweep_start: 0.0, sweep_stop: 1e-3, sweep_step: 50e-6, sweep_compliance: 2.0 };

export function SweepView() {
  const api = useApi();
  const session = useSession();
  const ui = useUi();
  const overrides = useOverrides(MODE);
  const sweep = useSweep();
  const [resolved, setResolved] = useState<Resolved | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const status = session.status;
  const running = status !== null && status.state !== "idle";
  const thisRunning = running && status.mode === MODE;

  const fieldKeys = useMemo(() => [...Object.keys(FIELD_META.SweepSettings ?? {}), ...MODE_TIMING.sweep], []);
  useEffect(() => {
    if (!ui.username) return;
    // The reply may arrive after the operator has changed: seed the tab of
    // the operator the profile was fetched for.
    const username = ui.username;
    api
      .profile(username)
      .then((profile) => seedOverrides(MODE, fieldKeys, profile.measurement ?? {}, username))
      .catch(() => undefined);
  }, [api, ui.username, fieldKeys]);

  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (!ui.username) return;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      api
        .resolve(MODE, ui.username!, overrides, true)
        .then(setResolved)
        .catch(() => setResolved(null));
    }, 150);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [api, ui.username, overrides]);

  // The start, stop, step and compliance numbers are in volts or amps
  // depending on the source. Switching the source must not keep the numbers
  // and change their meaning: a 2 V compliance would silently become 2 A.
  // They reset to a conservative sweep for the new source instead, and the
  // view says so until the next edit.
  const [reset, setReset] = useState<string | null>(null);
  const onChange = useCallback(
    (key: string, value: unknown) => {
      if (key === "sweep_source" && value !== (overrides.sweep_source ?? "voltage")) {
        const range = value === "current" ? SWEEP_FOR_CURRENT : SWEEP_FOR_VOLTAGE;
        for (const [k, v] of Object.entries(range)) setOverride(MODE, k, v);
        setReset(value === "current" ? "Start, stop, step and compliance reset for a current source: 0 to 1 mA in 50 µA steps, 2 V limit." : "Start, stop, step and compliance reset for a voltage source: 0 to 1 V in 50 mV steps, 100 mA limit.");
      } else if (key !== "sweep_source") {
        setReset(null);
      }
      setOverride(MODE, key, value);
    },
    [overrides.sweep_source],
  );

  const sourceIsVoltage = (overrides.sweep_source ?? "voltage") === "voltage";
  const sourceUnit = sourceIsVoltage ? "V" : "A";
  const complianceUnit = sourceIsVoltage ? "A" : "V";
  const points = typeof resolved?.derived.sweep_points === "number" ? resolved.derived.sweep_points : null;

  const canStart = session.backendReachable === true && !running && ui.username !== null && ui.sampleName.trim() !== "" && resolved?.ok === true && !busy;

  const start = async () => {
    if (!ui.username) return;
    setBusy(true);
    setError(null);
    try {
      await api.start({ mode: MODE, sample_name: ui.sampleName.trim(), username: ui.username, overrides });
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  const curve: XYSeries[] = useMemo(
    () =>
      sweep.segments.map((s, i) => ({
        label: s.direction === "reverse" ? "Reverse" : "Forward",
        color: i === 0 ? "var(--data-v)" : "var(--data-i)",
        x: s.voltages,
        y: s.currents,
        points: true,
      })),
    [sweep.segments],
  );
  // A segment does not say which quantity was sourced; the run's settings do.
  const sourced = overrides.sweep_source === "current" ? "current" : "voltage";
  const fit = useMemo(() => fitResistance(sweep.segments, sourced), [sweep.segments, sourced]);
  const totalPoints = sweep.segments.reduce((n, s) => n + s.voltages.length, 0);
  const inCompliance = sweep.segments.reduce((n, s) => n + s.compliance.filter((c) => c !== "OK").length, 0);

  return (
    <div className={styles.view}>
      <div className={styles.workspace}>
        <header className={styles.controls}>
          <div className={styles.title}>
            <h1>{MODE_LABEL.sweep}</h1>
            {thisRunning ? <Badge tone="ok">Sweeping</Badge> : sweep.segments.length > 0 ? <Badge>Done</Badge> : <Badge>Idle</Badge>}
          </div>
          <div className={styles.buttons}>
            {thisRunning ? (
              <Button variant="danger" size="lg" disabled={busy} onClick={() => void api.stop()}>
                <Icons.stop /> Stop
              </Button>
            ) : (
              <Button variant="primary" size="lg" disabled={!canStart} onClick={() => void start()}>
                <Icons.play /> Run sweep
              </Button>
            )}
          </div>
        </header>

        <BackendNotice />
        {error ? <Notice tone="danger">{error}</Notice> : null}
        {reset ? <Notice tone="info">{reset}</Notice> : null}
        {running && !thisRunning ? <Notice tone="info">Another run is in progress.</Notice> : null}
        {ui.sampleName.trim() === "" && !running ? <Notice tone="info">{NAME_THE_SAMPLE}</Notice> : null}
        {resolved?.hazard?.hazardous && !running ? (
          <Notice tone="warn">
            {resolved.hazard.reason} = {resolved.hazard.voltage_v} V is at or above the {resolved.hazard.threshold_v} V touch-safety
            threshold. You will be asked to acknowledge before the output turns on.
          </Notice>
        ) : null}

        <div className={own.summary}>
          <div className={own.stat}>
            <span className={own.statLabel}>Points</span>
            <span className={`${own.statValue} num`}>{totalPoints || (points ?? "—")}</span>
          </div>
          <div className={own.stat}>
            <span className={own.statLabel}>Fit R</span>
            <span className={`${own.statValue} num`}>{Number.isFinite(fit.r) ? formatEngineering(fit.r, "Ω") : "—"}</span>
          </div>
          {fit.fit.pooled === null && fit.fit.legs.length > 1
            ? fit.fit.legs.map((leg) => (
                <div className={own.stat} key={leg.direction}>
                  <span className={own.statLabel}>R {leg.direction} (n={leg.n})</span>
                  <span className={`${own.statValue} num`}>
                    {Number.isFinite(leg.r) ? formatEngineering(leg.r, "Ω") : "—"}
                    {Number.isFinite(leg.u) ? ` ± ${formatEngineering(leg.u, "Ω")}` : ""}
                  </span>
                </div>
              ))
            : null}
          <div className={own.stat}>
            <span className={own.statLabel}>R²</span>
            <span className={`${own.statValue} num`}>{Number.isFinite(fit.r2) ? fit.r2.toFixed(5) : "—"}</span>
          </div>
          {inCompliance > 0 ? (
            <div className={own.stat}>
              <Badge tone="danger">{inCompliance} in compliance</Badge>
            </div>
          ) : null}
        </div>

        <Panel
          className={styles.plotPanel}
          bodyClassName={styles.plotBody}
          title="I–V"
          actions={
            curve.length > 0 ? (
              // The key to the two legs: hysteresis is what this view is for.
              <div className={own.legend} aria-label="Legend">
                {curve.map((s, i) => (
                  <span key={i} className={own.legendItem}>
                    <span className={own.swatch} style={{ background: s.color }} />
                    {s.label}
                  </span>
                ))}
              </div>
            ) : undefined
          }
        >
          {curve.length > 0 ? (
            <XYPlot series={curve} xUnit="V" yUnit="A" />
          ) : (
            <div className={own.empty}>Run a sweep to see the curve.</div>
          )}
        </Panel>
      </div>

      <aside className={styles.settings}>
        <Panel title="Settings" bodyClassName={styles.settingsBody}>
          <SettingsForm
            mode={MODE}
            groups={MODE_FIELDS.sweep.map((g) => ({
              ...g,
              fields: g.fields.map((f) =>
                f.key === "sweep_start" || f.key === "sweep_stop" || f.key === "sweep_step"
                  ? { ...f, unit: sourceUnit }
                  : f.key === "sweep_compliance"
                    ? { ...f, unit: complianceUnit }
                    : f,
              ),
            }))}
            values={overrides}
            onChange={onChange}
            issues={resolved?.issues ?? []}
            disabled={running}
          />
          <div className={styles.sectionTitle}>Timing</div>
          {TIMING_FIELDS.filter((f) => MODE_TIMING.sweep.includes(f.key)).map((spec) => (
            <FieldRow
              key={spec.key}
              spec={spec}
              meta={FIELD_META.InstrumentSettings?.[spec.key] ?? {}}
              value={overrides[spec.key]}
              onChange={(v) => onChange(spec.key, v)}
              issue={resolved?.issues.find((i) => i.key === spec.key)}
              disabled={running}
            />
          ))}
          {points !== null ? (
            <div className={styles.derived}>
              This sweep: <span className="num">{points}</span> points
            </div>
          ) : null}
        </Panel>
      </aside>
    </div>
  );
}

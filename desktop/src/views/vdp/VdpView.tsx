// van der Pauw, ASTM F76 Method A. Four wirings, rewired by hand between each,
// current reversal automated at each: eight voltages, one sheet resistance.
//
// The backend drives the sequence and raises a prompt at each geometry; this
// view shows which contacts to move, waits for the operator to press Measure,
// and fills in the readings as they land. The result panel appears with the
// f(Q) homogeneity check, because a van der Pauw number without it is not one.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApi } from "../../app/AppContext";
import { FIELD_META, type Mode } from "../../generated/settings";
import { MODE_FIELDS, MODE_LABEL, MODE_TIMING, TIMING_FIELDS } from "../../lib/fields";
import { ApiError, type PendingPrompt, type Resolved } from "../../lib/api";
import { NAME_THE_SAMPLE } from "../../lib/copy";
import { formatEngineering } from "../../lib/format";
import { useSession } from "../../state/session";
import { useVdp } from "../../state/vdp";
import { useUi } from "../../state/ui";
import { seedOverrides, setOverride, useOverrides } from "../../state/overrides";
import { Badge, Button, Notice, Panel } from "../../components/ui";
import { Icons } from "../../components/icons";
import { FieldRow, SettingsForm } from "../../components/forms/SettingsForm";
import { STATE_LABEL } from "../continuous/ContinuousView";
import styles from "../continuous/ContinuousView.module.css";
import own from "./VdpView.module.css";

const MODE: Mode = "vdp";

interface GeometryDetail {
  index: number;
  name: string;
  group: string;
  source_high: number;
  source_low: number;
  sense_high: number;
  sense_low: number;
  label_pos: string;
  label_neg: string;
}

export function VdpView() {
  const api = useApi();
  const session = useSession();
  const ui = useUi();
  const overrides = useOverrides(MODE);
  const vdp = useVdp();
  const [resolved, setResolved] = useState<Resolved | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const status = session.status;
  const running = status !== null && status.state !== "idle";
  const thisRunning = running && status.mode === MODE;
  const prompt = status?.pending_prompt ?? null;
  const geometryPrompt = prompt && prompt.kind === "vdp_geometry" ? prompt : null;
  const geometry = geometryPrompt ? (geometryPrompt.detail as unknown as GeometryDetail) : null;

  const fieldKeys = useMemo(() => [...Object.keys(FIELD_META.VdpSettings ?? {}), ...MODE_TIMING.vdp], []);
  useEffect(() => {
    if (!ui.username) return;
    api
      .profile(ui.username)
      .then((profile) => seedOverrides(MODE, fieldKeys, profile.measurement ?? {}))
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

  const onChange = useCallback((key: string, value: unknown) => setOverride(MODE, key, value), []);

  const run = async (fn: () => Promise<unknown>) => {
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

  const canStart = !running && ui.username !== null && ui.sampleName.trim() !== "" && resolved?.ok === true && !busy;
  const start = () =>
    run(() => api.start({ mode: MODE, sample_name: ui.sampleName.trim(), username: ui.username!, overrides }));
  const measure = (p: PendingPrompt) => run(() => api.answerPrompt(p.prompt_id, "proceed"));

  // What the store holds is only this run's if the run ids agree: between
  // pressing Start and the run_started event, and after a replay, the store
  // can still describe the previous run.
  const current = status === null || status.run_id === null || vdp.runId === null || vdp.runId === status.run_id;
  const geometries = current ? vdp.geometries : [];
  const done = geometries.length;
  const result = current ? vdp.result : null;

  return (
    <div className={styles.view}>
      <div className={styles.workspace}>
        <header className={styles.controls}>
          <div className={styles.title}>
            <h1>{MODE_LABEL.vdp}</h1>
            {thisRunning ? (
              <Badge tone={status.state === "awaiting_prompt" ? "accent" : "ok"}>{STATE_LABEL[status.state] ?? status.state}</Badge>
            ) : result ? (
              <Badge>Done</Badge>
            ) : (
              <Badge>Idle</Badge>
            )}
          </div>
          <div className={styles.buttons}>
            {thisRunning ? (
              <Button variant="danger" size="lg" disabled={busy} onClick={() => void run(() => api.stop())}>
                <Icons.stop /> Abort
              </Button>
            ) : (
              <Button variant="primary" size="lg" disabled={!canStart} onClick={() => void start()}>
                <Icons.play /> Start
              </Button>
            )}
          </div>
        </header>

        {error ? <Notice tone="danger">{error}</Notice> : null}
        {running && !thisRunning ? <Notice tone="info">Another run is in progress.</Notice> : null}
        {ui.sampleName.trim() === "" && !running ? <Notice tone="info">{NAME_THE_SAMPLE}</Notice> : null}

        <div className={own.body}>
          <Panel className={own.wizard} bodyClassName={own.wizardBody} title={<Stepper done={done} active={geometry?.index ?? null} running={thisRunning} />}>
            {geometry ? (
              <div className={own.step}>
                <ContactDiagram geometry={geometry} />
                <div className={own.instructions}>
                  <div className={own.stepTitle}>{geometry.name}</div>
                  <table className={own.wiring}>
                    <tbody>
                      <tr>
                        <th>Force HI</th>
                        <td className="num">C{geometry.source_high}</td>
                      </tr>
                      <tr>
                        <th>Force LO</th>
                        <td className="num">C{geometry.source_low}</td>
                      </tr>
                      <tr>
                        <th>Sense HI</th>
                        <td className="num">C{geometry.sense_high}</td>
                      </tr>
                      <tr>
                        <th>Sense LO</th>
                        <td className="num">C{geometry.sense_low}</td>
                      </tr>
                    </tbody>
                  </table>
                  <Button variant="primary" size="lg" disabled={busy} onClick={() => void measure(geometryPrompt!)}>
                    <Icons.check /> Measure
                  </Button>
                  {geometry.index === 0 ? <div className={own.muted}>Output is off while you rewire.</div> : null}
                </div>
              </div>
            ) : thisRunning ? (
              <div className={own.centered}>
                <div className={own.muted}>{status.state === "running" ? "Measuring…" : STATE_LABEL[status.state] ?? status.state}</div>
              </div>
            ) : result ? (
              <ResultPanel result={result} />
            ) : (
              <div className={own.centered}>
                <ContactDiagram geometry={null} />
                <div className={own.muted}>Four wirings, rewired by hand. Press Start.</div>
              </div>
            )}
          </Panel>

          <Panel title="Readings" bodyClassName={own.readingsBody}>
            {geometries.length === 0 ? (
              <div className={own.muted}>None yet.</div>
            ) : (
              <table className={own.readings}>
                <thead>
                  <tr>
                    <th>Geometry</th>
                    <th>V+</th>
                    <th>V−</th>
                    <th>I</th>
                  </tr>
                </thead>
                <tbody>
                  {geometries.map((g) => (
                    <tr key={g.index}>
                      <td>
                        {g.name} <span className={own.group}>{g.group}</span>
                      </td>
                      <td className="num">{formatEngineering(g.v_pos, "V")}</td>
                      <td className="num">{formatEngineering(g.v_neg, "V")}</td>
                      <td className="num">{formatEngineering(g.current_a, "A")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>
        </div>
      </div>

      <aside className={styles.settings}>
        <Panel title="Settings" bodyClassName={styles.settingsBody}>
          <SettingsForm mode={MODE} groups={MODE_FIELDS.vdp} values={overrides} onChange={onChange} issues={resolved?.issues ?? []} disabled={running} />
          <div className={styles.sectionTitle}>Timing</div>
          {TIMING_FIELDS.filter((f) => MODE_TIMING.vdp.includes(f.key)).map((spec) => (
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
        </Panel>
      </aside>
    </div>
  );
}

function Stepper({ done, active, running }: { done: number; active: number | null; running: boolean }) {
  return (
    <div className={own.stepper}>
      <span>Configurations</span>
      {[0, 1, 2, 3].map((i) => (
        <span
          key={i}
          className={own.stepDot}
          data-state={i < done ? "done" : active === i && running ? "active" : "todo"}
          aria-label={`configuration ${i + 1}`}
        >
          {i < done ? <Icons.check size={11} /> : i + 1}
        </span>
      ))}
    </div>
  );
}

/** The sample as a square with contacts 1–4 at the corners. Force leads are
 *  drawn solid, sense leads dashed; the current arrow shows +I direction. */
function ContactDiagram({ geometry }: { geometry: GeometryDetail | null }) {
  // Contact numbering runs clockwise from top-left, as in the F76 figure.
  const corners: Record<number, [number, number]> = { 1: [40, 40], 2: [160, 40], 3: [160, 160], 4: [40, 160] };
  const dot = (n: number, role: "fh" | "fl" | "sh" | "sl" | null) => {
    const [x, y] = corners[n]!;
    return (
      <g key={n}>
        <circle cx={x} cy={y} r={11} className={own.contact} data-role={role ?? undefined} />
        <text x={x} y={y + 4} textAnchor="middle" className={own.contactLabel}>
          {n}
        </text>
      </g>
    );
  };
  const role = (n: number): "fh" | "fl" | "sh" | "sl" | null => {
    if (!geometry) return null;
    if (n === geometry.source_high) return "fh";
    if (n === geometry.source_low) return "fl";
    if (n === geometry.sense_high) return "sh";
    if (n === geometry.sense_low) return "sl";
    return null;
  };
  const from = geometry ? corners[geometry.source_high] : undefined;
  const to = geometry ? corners[geometry.source_low] : undefined;
  return (
    <svg viewBox="0 0 200 200" className={own.diagram} aria-hidden="true">
      <rect x="40" y="40" width="120" height="120" rx="6" className={own.sample} />
      {from && to ? (
        <line x1={from[0]} y1={from[1]} x2={to[0]} y2={to[1]} className={own.currentArrow} markerEnd="url(#vdp-arrow)" />
      ) : null}
      <defs>
        <marker id="vdp-arrow" markerWidth="8" markerHeight="8" refX="14" refY="4" orient="auto">
          <path d="M0,0 L8,4 L0,8 z" className={own.currentArrowHead} />
        </marker>
      </defs>
      {[1, 2, 3, 4].map((n) => dot(n, role(n)))}
      {geometry ? (
        <>
          <text x="100" y="192" textAnchor="middle" className={own.legend}>
            <tspan className={own.legendForce}>● force</tspan> <tspan className={own.legendSense}>● sense</tspan>
          </text>
        </>
      ) : null}
    </svg>
  );
}

function ResultPanel({ result }: { result: NonNullable<ReturnType<typeof useVdp>["result"]> }) {
  const uRs = result.sheet_resistance_uncertainty;
  const uRho = result.rho_avg_uncertainty;
  return (
    <div className={own.result}>
      <div className={own.headline}>
        <div>
          <div className={own.resultLabel}>Sheet resistance</div>
          <div className={`${own.resultValue} num`}>
            {formatEngineering(result.sheet_resistance, "Ω/sq")}
            {typeof uRs === "number" && Number.isFinite(uRs) ? <span className={own.unc}> ± {formatEngineering(uRs, "Ω/sq")}</span> : null}
          </div>
        </div>
        <div>
          <div className={own.resultLabel}>Resistivity</div>
          <div className={`${own.resultValue} num`}>
            {formatEngineering(result.rho_avg, "Ω·cm")}
            {typeof uRho === "number" && Number.isFinite(uRho) ? <span className={own.unc}> ± {formatEngineering(uRho, "Ω·cm")}</span> : null}
          </div>
        </div>
      </div>
      <div className={own.checks}>
        <Badge tone={result.homogeneous ? "ok" : "danger"}>{result.homogeneous ? "f(Q) homogeneity passes" : "f(Q) homogeneity fails"}</Badge>
        <span className="num">asymmetry {result.asymmetry_pct.toFixed(2)} %</span>
      </div>
      <table className={own.detailTable}>
        <tbody>
          <tr>
            <th>ρ_A</th>
            <td className="num">{formatEngineering(result.rho_a, "Ω·cm")}</td>
            <th>Q_A</th>
            <td className="num">{result.q_a.toFixed(4)}</td>
            <th>f_A</th>
            <td className="num">{result.f_a.toFixed(4)}</td>
          </tr>
          <tr>
            <th>ρ_B</th>
            <td className="num">{formatEngineering(result.rho_b, "Ω·cm")}</td>
            <th>Q_B</th>
            <td className="num">{result.q_b.toFixed(4)}</td>
            <th>f_B</th>
            <td className="num">{result.f_b.toFixed(4)}</td>
          </tr>
          <tr>
            <th>I</th>
            <td className="num">{formatEngineering(result.current_a, "A")}</td>
            <th>t</th>
            <td className="num">{formatEngineering(result.thickness_cm, "cm")}</td>
            <th />
            <td />
          </tr>
        </tbody>
      </table>
    </div>
  );
}

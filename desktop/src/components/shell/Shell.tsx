// The frame every view sits in: top bar, mode rail, main area, status log.

import { useEffect, useRef, type ReactNode } from "react";
import logo from "../../assets/logo.svg";
import { Icons, type IconName } from "../icons";
import { useSession, clearLog } from "../../state/session";
import { useUi, setView, setLogOpen, setSampleName, type View } from "../../state/ui";
import styles from "./Shell.module.css";

const NAV: { view: View; label: string; icon: IconName }[] = [
  { view: "resistance", label: "Resistance", icon: "resistance" },
  { view: "source_v", label: "Voltage source", icon: "voltage" },
  { view: "source_i", label: "Current source", icon: "current" },
  { view: "four_point", label: "Four-point probe", icon: "fourPoint" },
  { view: "sweep", label: "I-V sweep", icon: "sweep" },
  { view: "vdp", label: "van der Pauw", icon: "vdp" },
];

interface ShellProps {
  children: ReactNode;
  onOpenSettings: () => void;
  onOpenUser: () => void;
}

export function Shell({ children, onOpenSettings, onOpenUser }: ShellProps) {
  const ui = useUi();
  return (
    <div className={styles.shell} data-log-closed={!ui.logOpen}>
      <TopBar onOpenSettings={onOpenSettings} onOpenUser={onOpenUser} />
      <NavRail />
      <main className={styles.main}>{children}</main>
      <StatusLog />
    </div>
  );
}

function TopBar({ onOpenSettings, onOpenUser }: Omit<ShellProps, "children">) {
  const session = useSession();
  const ui = useUi();
  const running = session.status?.state !== undefined && session.status.state !== "idle";

  const instrument = session.instrument;
  const backendState = session.backendReachable === false ? "off" : session.connected ? "live" : "warn";
  const backendLabel =
    session.backendReachable === false
      ? "Backend unreachable"
      : session.connected
        ? "Connected"
        : session.backendReachable === null
          ? "Connecting"
          : "Reconnecting";

  return (
    <header className={styles.topbar}>
      <div className={styles.brand}>
        <img src={logo} alt="" />
        <span>ResistaMet</span>
        <span className={styles.brandVersion}>2.0</span>
      </div>

      <div className={styles.sample}>
        <span className={styles.sampleLabel}>Sample</span>
        <input
          className={`${styles.sampleInput} mono`}
          value={ui.sampleName}
          placeholder="name this sample"
          disabled={running}
          onChange={(e) => setSampleName(e.target.value)}
          spellCheck={false}
        />
      </div>

      <div className={styles.chips}>
        <span className={styles.chip} title={instrument ? instrument.idn : "No instrument identified yet"}>
          <Icons.plug />
          {instrument ? (
            <>
              <strong>Keithley {instrument.model}</strong>
              <span className="mono">{instrument.address}</span>
            </>
          ) : (
            "No instrument"
          )}
        </span>
        <span className={styles.chip} title="Connection to the measurement backend">
          <span className={styles.dot} data-state={backendState} />
          {backendLabel}
        </span>
        <button className={`${styles.chip} ${styles.chipButton}`} onClick={onOpenUser} type="button">
          <Icons.user />
          <strong>{ui.username ?? "Select user"}</strong>
        </button>
        <button
          className={`${styles.chip} ${styles.chipButton}`}
          onClick={onOpenSettings}
          type="button"
          title="Settings"
        >
          <Icons.settings />
        </button>
      </div>
    </header>
  );
}

function NavRail() {
  const ui = useUi();
  const session = useSession();
  const activeMode = session.status?.mode ?? null;
  const running = session.status !== null && session.status.state !== "idle";

  return (
    <nav className={styles.rail} aria-label="Measurement modes">
      <div className={styles.railSection}>Measure</div>
      {NAV.map(({ view, label, icon }) => {
        const Icon = Icons[icon];
        return (
          <button
            key={view}
            type="button"
            className={styles.railItem}
            aria-current={ui.view === view ? "page" : undefined}
            onClick={() => setView(view)}
          >
            <Icon />
            <span>{label}</span>
            {running && activeMode === view ? <span className={styles.railRunning} /> : null}
          </button>
        );
      })}
      <div className={styles.railSection}>Review</div>
      <button
        type="button"
        className={styles.railItem}
        aria-current={ui.view === "results" ? "page" : undefined}
        onClick={() => setView("results")}
      >
        <Icons.results />
        <span>Results</span>
      </button>
      <div className={styles.railSpacer} />
    </nav>
  );
}

function StatusLog() {
  const { log } = useSession();
  const ui = useUi();
  const bodyRef = useRef<HTMLDivElement>(null);
  const pinnedToBottom = useRef(true);

  useEffect(() => {
    const body = bodyRef.current;
    if (body && pinnedToBottom.current) body.scrollTop = body.scrollHeight;
  }, [log]);

  return (
    <section className={styles.log} aria-label="Status log">
      <div className={styles.logHeader}>
        Log
        <button type="button" onClick={clearLog} title="Clear the log">
          <Icons.trash size={12} /> Clear
        </button>
        <button
          type="button"
          className={styles.logToggle}
          onClick={() => setLogOpen(!ui.logOpen)}
          aria-expanded={ui.logOpen}
        >
          {ui.logOpen ? <Icons.chevronDown size={12} /> : <Icons.chevronUp size={12} />}
          {ui.logOpen ? "Hide" : "Show"}
        </button>
      </div>
      {ui.logOpen ? (
        <div
          className={styles.logBody}
          ref={bodyRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            pinnedToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
          }}
        >
          {log.length === 0 ? <div className={styles.logEmpty}>Nothing yet.</div> : null}
          {log.map((line) => (
            <div key={line.seq + ":" + line.t} className={styles.logLine} data-level={line.level}>
              <span className={styles.logTime}>{formatClock(line.t)}</span>
              <span>{line.message}</span>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function formatClock(unixSeconds: number): string {
  const d = new Date(unixSeconds * 1000);
  return d.toLocaleTimeString(undefined, { hour12: false });
}

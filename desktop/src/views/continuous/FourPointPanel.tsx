// The four-point probe's extra panels: the spot being measured and the map's
// spots.
//
// A spot is a run. Every number here is the backend's: the statistics of the
// last run come from its spot_complete event, the table and the spread
// between spots from GET /maps/{id}. The panel adds the next spot's name,
// and Redo, which makes the next run measure an earlier spot again (the
// newer run of an index stands for the spot in the map).

import type { ReactNode } from "react";
import type { MapSpot, QuantityStats, SpotMap } from "../../generated/maps";
import type { MapOwner } from "../../lib/map/mapId";
import { edgeError } from "../../lib/map/figure";
import { activeMap, activeMapId, nextIndex, setRedo, setSpotLabel, startNewMap, useSpots } from "../../state/spots";
import { useLatestSample } from "../../state/samples";
import { engineering, formatEngineering, formatWithUncertainty } from "../../lib/format";
import { Badge, Button, IconButton, Input, Panel } from "../../components/ui";
import { Icons } from "../../components/icons";
import styles from "./FourPointPanel.module.css";

/** Ends that mean the spot ran its course. */
const COMPLETE_ENDS = new Set(["target_samples", "completed", "duration"]);

interface Props {
  running: boolean;
  owner: MapOwner | null;
  /** fpp_edge_warn_pct of the settings in force. */
  edgeWarnPct: number;
}

export function FourPointPanel({ running, owner, edgeWarnPct }: Props) {
  const spots = useSpots();
  const map = activeMap(spots, owner);
  const mapId = activeMapId(spots, owner);
  const index = nextIndex(spots, map);

  return (
    <div className={styles.row}>
      <Panel title="Spot" className={styles.stats}>
        <div className={styles.nextRow}>
          <Input
            value={spots.label}
            placeholder={`Spot ${index}`}
            aria-label="Spot name"
            disabled={running}
            maxLength={80}
            onChange={(e) => setSpotLabel(e.target.value)}
          />
          {spots.redo ? (
            <span className={styles.redo}>
              <Badge tone="accent">redo #{spots.redo.index}</Badge>
              <IconButton aria-label="Cancel redo" disabled={running} onClick={() => setRedo(null)}>
                <Icons.close size={12} />
              </IconButton>
            </span>
          ) : (
            <span className={styles.nextIndex}>#{index}</span>
          )}
        </div>
        <SpotStatistics running={running} />
      </Panel>

      <Panel
        title={
          <span className={styles.spotsTitle}>
            Spots
            {mapId ? (
              <span className={`${styles.mapId} mono`} title={mapId}>
                {mapId}
              </span>
            ) : null}
          </span>
        }
        actions={
          mapId ? (
            <Button size="sm" variant="ghost" disabled={running} onClick={startNewMap} title="The next run starts a new map. The files of this one stay.">
              New map
            </Button>
          ) : undefined
        }
        className={styles.spots}
        bodyClassName={styles.spotsBody}
      >
        {spots.mapError ? <div className={styles.error}>{spots.mapError}</div> : null}
        {map === null || (map.spots ?? []).length === 0 ? (
          <div className={styles.empty}>{mapId ? "No spot measured yet." : "A new map starts with the next run."}</div>
        ) : (
          <SpotTable map={map} edgeWarnPct={edgeWarnPct} running={running} redoIndex={spots.redo?.index ?? null} />
        )}
      </Panel>
    </div>
  );
}

function SpotStatistics({ running }: { running: boolean }) {
  const { lastSpot, running: runningSpot, warning } = useSpots();
  const sample = useLatestSample();
  const nearEdge = warning !== null && !warning.refused ? warning : null;

  if (running) {
    return (
      <div className={styles.pendingStats}>
        <span>
          Measuring {runningSpot ? <strong>{runningSpot.label}</strong> : "…"}
          {sample && sample.mode === "four_point" ? <span className="num"> · {sample.count} samples</span> : null}
        </span>
        <span className={styles.faint}>Statistics when the run ends.</span>
        {nearEdge ? <EdgeNote message={nearEdge.message} /> : null}
      </div>
    );
  }
  if (lastSpot === null) {
    return (
      <div className={styles.pendingStats}>
        <span className={styles.faint}>{warning?.refused ? "Refused; nothing measured." : "No run yet."}</span>
      </div>
    );
  }

  const { stats, spot } = lastSpot;
  const excluded = stats.n_excluded ?? 0;
  const cutShort = stats.end_reason && !COMPLETE_ENDS.has(stats.end_reason) ? stats.end_reason.replace(/_/g, " ") : null;
  return (
    <>
      <div className={styles.statsHead}>
        <span className={styles.statsLabel}>{spot ? `#${spot.index} ${spot.label}` : "Last run"}</span>
        {cutShort ? <Badge tone="warn">ended: {cutShort}</Badge> : null}
      </div>
      <div className={styles.statGrid}>
        <Stat label={<><Sym>Rs</Sym> mean ± <Sym>u</Sym></>} value={formatWithUncertainty(stats.rs.mean, stats.rs.u_total, "Ω/sq")} />
        <Stat label={<><Sym>Rs</Sym> SD</>} {...quantity(stats.rs.sd, "Ω/sq")} />
        <Stat label="RSD" value={percent(stats.rs.rsd_pct)} />
        <Stat label={<><Sym>ρ</Sym> ± <Sym>u</Sym></>} value={formatWithUncertainty(stats.rho.mean, stats.rho.u_total, "Ω·cm")} />
        <Stat label={<><Sym>σ</Sym> ± <Sym>u</Sym></>} value={formatWithUncertainty(stats.sigma.mean, stats.sigma.u_total, "S/cm")} />
        <Stat label={<Sym>n</Sym>} value={excluded > 0 ? `${stats.n} (+${excluded} in compliance)` : String(stats.n)} />
      </div>
      {nearEdge ? <EdgeNote message={nearEdge.message} /> : null}
    </>
  );
}

function EdgeNote({ message }: { message: string }) {
  return (
    <div className={styles.edgeNote}>
      <Icons.warning size={13} style={{ flex: "none", marginTop: 2 }} />
      <span>{message}</span>
    </div>
  );
}

interface TableProps {
  map: SpotMap;
  edgeWarnPct: number;
  running: boolean;
  redoIndex: number | null;
}

function SpotTable({ map, edgeWarnPct, running, redoIndex }: TableProps) {
  const spots = map.spots ?? [];
  const skipped = map.skipped ?? [];
  return (
    <>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>#</th>
            <th>Spot</th>
            <th><Sym>n</Sym></th>
            <th><Sym>Rs</Sym> ± <Sym>u</Sym></th>
            <th>RSD</th>
            <th>Edge</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {spots.map((s) => (
            <SpotRow key={s.index} spot={s} edgeWarnPct={edgeWarnPct} running={running} redo={redoIndex === s.index} />
          ))}
        </tbody>
      </table>
      <div className={styles.between}>
        <Between symbol="Rs" unit="Ω/sq" stats={map.rs} />
        <Between symbol="ρ" unit="Ω·cm" stats={map.rho} />
        <Between symbol="σ" unit="S/cm" stats={map.sigma} />
        {skipped.length > 0 ? (
          <span className={styles.skipped} title={skipped.map((r) => `${r.file}: ${r.reason}`).join("\n")}>
            {skipped.length} {skipped.length === 1 ? "run" : "runs"} skipped
          </span>
        ) : null}
      </div>
    </>
  );
}

function SpotRow({ spot, edgeWarnPct, running, redo }: { spot: MapSpot; edgeWarnPct: number; running: boolean; redo: boolean }) {
  const error = edgeError(spot);
  const overEdge = error !== null && Math.abs(error) * 100 > edgeWarnPct;
  const superseded = spot.superseded?.length ?? 0;
  const cutShort = spot.stats.end_reason && !COMPLETE_ENDS.has(spot.stats.end_reason) ? spot.stats.end_reason.replace(/_/g, " ") : null;
  return (
    <tr data-redo={redo || undefined}>
      <td className="num">{spot.index}</td>
      <td>
        {spot.label}
        {superseded > 0 ? (
          <span className={styles.hint} title={`Earlier runs of this spot, kept on disk:\n${(spot.superseded ?? []).join("\n")}`}>
            {" "}superseded ×{superseded}
          </span>
        ) : null}
        {cutShort ? <span className={styles.hint} title="How the run that stands for this spot ended"> {cutShort}</span> : null}
      </td>
      <td className="num">{spot.stats.n}</td>
      <td className="num">{formatWithUncertainty(spot.stats.rs.mean, spot.stats.rs.u_total, "Ω/sq")}</td>
      <td className="num">{percent(spot.stats.rs.rsd_pct)}</td>
      <td
        className="num"
        data-tone={overEdge ? "warn" : undefined}
        title={
          error === null
            ? undefined
            : `Rs is ${signedPercent(error)} off for assuming a centred probe` +
              (typeof spot.edge_clearance_s === "number" ? `; nearest tip ${spot.edge_clearance_s.toFixed(1)} s from the edge` : "") +
              ". Not applied."
        }
      >
        {error === null ? "" : signedPercent(error)}
      </td>
      <td>
        <Button
          size="sm"
          variant="ghost"
          disabled={running}
          onClick={() => setRedo(redo ? null : { index: spot.index, label: spot.label })}
          title="Measure this spot again with the next run. The newer run stands for the spot; the older file stays."
        >
          {redo ? "Cancel" : "Redo"}
        </Button>
      </td>
    </tr>
  );
}

/** The spread between the spots' means, as the backend worked it out. */
function Between({ symbol, unit, stats }: { symbol: string; unit: string; stats: SpotMap["rs"] }) {
  if (stats.n < 2 || typeof stats.mean !== "number") return null;
  return (
    <span>
      <Sym>{symbol}</Sym> across {stats.n}: <span className="num">{formatEngineering(stats.mean, unit)}</span>
      {typeof stats.sd === "number" ? <> · SD <span className="num">{formatEngineering(stats.sd, unit)}</span></> : null}
      {typeof stats.rsd_pct === "number" ? <> · RSD <span className="num">{percent(stats.rsd_pct)}</span></> : null}
    </span>
  );
}

/** The backend's RSD is already a percentage. */
function percent(value: QuantityStats["rsd_pct"]): string {
  return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(value < 10 ? 2 : 1)} %` : "—";
}

/** A fraction, signed, as a percentage. */
function signedPercent(fraction: number): string {
  const pct = fraction * 100;
  return `${pct > 0 ? "+" : pct < 0 ? "−" : ""}${Math.abs(pct).toFixed(Math.abs(pct) < 10 ? 1 : 0)} %`;
}

/** A quantity symbol in a label that is otherwise uppercased. */
function Sym({ children }: { children: ReactNode }) {
  return <span className="sym">{children}</span>;
}

/** Number and unit apart, so a narrow cell can put the unit on its own line
 *  instead of cutting the value short. */
function quantity(value: number | null | undefined, unit: string): { value: string; unit: string } {
  const e = engineering(typeof value === "number" ? value : NaN, unit);
  return { value: e.mantissa, unit: e.unit };
}

function Stat({ label, value, unit }: { label: ReactNode; value: string; unit?: string }) {
  return (
    <div className={styles.stat}>
      <span className={styles.statLabel}>{label}</span>
      <span className={`${styles.statValue} num`}>
        {value}
        {unit ? <> <span className={styles.statUnit}>{unit}</span></> : null}
      </span>
    </div>
  );
}

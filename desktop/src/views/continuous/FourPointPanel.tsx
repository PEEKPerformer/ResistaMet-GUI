// The four-point probe's extra panel: running statistics on the derived
// sheet resistance, and the spot table.
//
// Statistics are recomputed on a timer, not per sample — a 4PP spot is a few
// dozen readings, but the arithmetic is over the whole run and there is no
// need to do it fifty times a second.

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { getSeries } from "../../state/samples";
import { addSpot, clearSpots, meanSd, removeSpot, useSpots } from "../../state/spots";
import { useUi } from "../../state/ui";
import { formatEngineering, formatPercent } from "../../lib/format";
import { Button, IconButton, Input, Panel } from "../../components/ui";
import { Icons } from "../../components/icons";
import styles from "./FourPointPanel.module.css";

interface Stats {
  n: number;
  rs: { mean: number; sd: number };
  rho: { mean: number; sd: number };
  sigma: { mean: number };
}

function computeStats(): Stats {
  const series = getSeries();
  if (series.mode !== "four_point") return { n: 0, rs: { mean: NaN, sd: NaN }, rho: { mean: NaN, sd: NaN }, sigma: { mean: NaN } };
  const rs = meanSd(series.values["derived_rs"] ?? []);
  const rho = meanSd(series.values["derived_rho"] ?? []);
  const sigma = meanSd(series.values["derived_sigma"] ?? []);
  return { n: rs.n, rs, rho, sigma };
}

export function FourPointPanel({ running }: { running: boolean }) {
  const [stats, setStats] = useState<Stats>(computeStats);
  const [spotName, setSpotName] = useState("");
  const spots = useSpots();
  const ui = useUi();

  useEffect(() => {
    const timer = setInterval(() => setStats(computeStats()), 500);
    return () => clearInterval(timer);
  }, []);

  const rsd = useMemo(
    () => (Number.isFinite(stats.rs.sd) && stats.rs.mean !== 0 ? stats.rs.sd / Math.abs(stats.rs.mean) : NaN),
    [stats],
  );

  const save = () => {
    if (stats.n === 0) return;
    addSpot({
      name: spotName.trim() || `Spot ${spots.length + 1}`,
      sample: ui.sampleName,
      n: stats.n,
      rsMean: stats.rs.mean,
      rsSd: stats.rs.sd,
      rhoMean: stats.rho.mean,
      sigmaMean: stats.sigma.mean,
    });
    setSpotName("");
  };

  const across = useMemo(() => meanSd(spots.map((s) => s.rsMean)), [spots]);

  return (
    <div className={styles.row}>
      <Panel title="This spot" className={styles.stats}>
        <div className={styles.statGrid}>
          <Stat label={<Sym>n</Sym>} value={String(stats.n)} />
          <Stat label={<><Sym>Rs</Sym> mean</>} value={formatEngineering(stats.rs.mean, "Ω/sq")} />
          <Stat label={<><Sym>Rs</Sym> SD</>} value={formatEngineering(stats.rs.sd, "Ω/sq")} />
          <Stat label="RSD" value={formatPercent(rsd)} />
          <Stat label={<><Sym>ρ</Sym> mean</>} value={formatEngineering(stats.rho.mean, "Ω·cm")} />
          <Stat label={<><Sym>σ</Sym> mean</>} value={formatEngineering(stats.sigma.mean, "S/cm")} />
        </div>
        <div className={styles.saveRow}>
          <Input
            value={spotName}
            placeholder={`Spot ${spots.length + 1}`}
            onChange={(e) => setSpotName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") save();
            }}
          />
          <Button disabled={stats.n === 0 || running} onClick={save} title={running ? "Stop the run first" : "Save this spot's statistics"}>
            Save spot
          </Button>
        </div>
      </Panel>

      <Panel
        title={
          <span className={styles.spotsTitle}>
            Spots
            {spots.length > 1 ? (
              <span className={styles.across}>
                across {spots.length}: <span className="num">{formatEngineering(across.mean, "Ω/sq")}</span> ±{" "}
                <span className="num">{formatEngineering(across.sd, "Ω/sq")}</span>
              </span>
            ) : null}
          </span>
        }
        actions={
          spots.length > 0 ? (
            <Button size="sm" variant="ghost" onClick={clearSpots}>
              Clear
            </Button>
          ) : undefined
        }
        className={styles.spots}
        bodyClassName={styles.spotsBody}
      >
        {spots.length === 0 ? (
          <div className={styles.empty}>None yet.</div>
        ) : (
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Spot</th>
                <th><Sym>n</Sym></th>
                <th><Sym>Rs</Sym></th>
                <th>SD</th>
                <th><Sym>ρ</Sym></th>
                <th />
              </tr>
            </thead>
            <tbody>
              {spots.map((s) => (
                <tr key={s.id}>
                  <td>{s.name}</td>
                  <td className="num">{s.n}</td>
                  <td className="num">{formatEngineering(s.rsMean, "Ω/sq")}</td>
                  <td className="num">{formatEngineering(s.rsSd, "Ω/sq")}</td>
                  <td className="num">{formatEngineering(s.rhoMean, "Ω·cm")}</td>
                  <td>
                    <IconButton aria-label="Remove spot" onClick={() => removeSpot(s.id)}>
                      <Icons.close size={12} />
                    </IconButton>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}

/** A quantity symbol in a label that is otherwise uppercased. */
function Sym({ children }: { children: ReactNode }) {
  return <span className="sym">{children}</span>;
}

function Stat({ label, value }: { label: ReactNode; value: string }) {
  return (
    <div className={styles.stat}>
      <span className={styles.statLabel}>{label}</span>
      <span className={`${styles.statValue} num`}>{value}</span>
    </div>
  );
}

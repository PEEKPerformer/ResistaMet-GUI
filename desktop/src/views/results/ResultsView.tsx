// What runs have written: a list of files and a preview of one.
//
// The preview parses the CSV in the browser and plots one column against
// elapsed time; the metadata header is shown as-is, because it is the record
// of what the run was.

import { useEffect, useMemo, useState } from "react";
import { useApi } from "../../app/AppContext";
import { ApiError, type ResultFile } from "../../lib/api";
import { parseResistametCsv, type ParsedCsv } from "../../lib/csv";
import { useUi } from "../../state/ui";
import { Badge, Button, Notice, Panel, Select } from "../../components/ui";
import { Icons } from "../../components/icons";
import { XYPlot } from "../../components/plot/XYPlot";
import styles from "./ResultsView.module.css";

const UNIT_BY_COLUMN: Record<string, string> = {
  R_ohm: "Ω",
  R_unc_ohm: "Ω",
  V_meas: "V",
  I_meas: "A",
  V: "V",
  I: "A",
  V_over_I: "Ω",
  Rs_ohm_sq: "Ω/sq",
  rho_ohm_cm: "Ω·cm",
  sigma_S_cm: "S/cm",
  V_unc_V: "V",
  I_unc_A: "A",
  elapsed_s: "s",
};

export function ResultsView() {
  const api = useApi();
  const ui = useUi();
  const [files, setFiles] = useState<ResultFile[] | null>(null);
  const [root, setRoot] = useState("");
  const [onlyMine, setOnlyMine] = useState(true);
  const [selected, setSelected] = useState<ResultFile | null>(null);
  const [parsed, setParsed] = useState<ParsedCsv | null>(null);
  const [column, setColumn] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = () => {
    setLoading(true);
    api
      .results(onlyMine ? ui.username : null)
      .then(({ root, files }) => {
        setRoot(root);
        setFiles(files);
      })
      .catch((e: unknown) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  };

  useEffect(refresh, [api, onlyMine, ui.username]); // eslint-disable-line react-hooks/exhaustive-deps

  const open = (file: ResultFile) => {
    setSelected(file);
    setParsed(null);
    setError(null);
    if (!file.name.endsWith(".csv")) return;
    api
      .resultFile(file.path)
      .then((text) => {
        const result = parseResistametCsv(text);
        setParsed(result);
        // The quantity the run was about, when the file has it.
        const preferred = ["R_ohm", "Rs_ohm_sq", "I_meas", "V_meas", "I", "V"];
        const first =
          preferred.find((c) => c in result.data) ??
          result.columns.find((c) => c !== "elapsed_s" && c in result.data && !c.includes("unc"));
        setColumn(first ?? null);
      })
      .catch((e: unknown) => setError(e instanceof ApiError ? e.detail : String(e)));
  };

  const series = useMemo(() => {
    if (!parsed || !column) return [];
    const x = parsed.data["elapsed_s"] ?? parsed.data["Point"] ?? parsed.data[parsed.columns[0] ?? ""] ?? [];
    const y = parsed.data[column] ?? [];
    return [{ label: column, color: "var(--data-r)", x, y, points: y.length < 400 }];
  }, [parsed, column]);

  const numericColumns = parsed ? parsed.columns.filter((c) => c in parsed.data && c !== "elapsed_s") : [];

  return (
    <div className={styles.view}>
      <Panel
        className={styles.list}
        bodyClassName={styles.listBody}
        title="Files"
        actions={
          <div className={styles.listActions}>
            <label className={styles.mine}>
              <input type="checkbox" checked={onlyMine} onChange={(e) => setOnlyMine(e.target.checked)} />
              only {ui.username ?? "me"}
            </label>
            <Button size="sm" variant="ghost" onClick={refresh} disabled={loading}>
              Refresh
            </Button>
          </div>
        }
      >
        {files === null ? <div className={styles.muted}>Loading…</div> : null}
        {files && files.length === 0 ? <div className={styles.muted}>No runs yet.</div> : null}
        {files?.map((file) => (
          <button
            key={file.path}
            type="button"
            className={styles.file}
            data-selected={selected?.path === file.path}
            onClick={() => open(file)}
          >
            <Icons.folder size={14} />
            <span className={styles.fileName}>{file.name}</span>
            <span className={styles.fileMeta}>
              {file.user ?? "—"} · {formatSize(file.size)} · {new Date(file.modified * 1000).toLocaleString()}
            </span>
          </button>
        ))}
        <div className={styles.root} title={root}>
          <span className="mono">{root}</span>
        </div>
      </Panel>

      <div className={styles.preview}>
        {selected === null ? (
          <div className={styles.empty}>Pick a file to preview it.</div>
        ) : (
          <>
            <header className={styles.previewHeader}>
              <h2 className={styles.previewTitle}>{selected.name}</h2>
              {parsed ? <Badge>{parsed.rows} rows</Badge> : null}
              {numericColumns.length > 0 ? (
                <Select value={column ?? ""} onChange={(e) => setColumn(e.target.value)} className={styles.columnPicker}>
                  {numericColumns.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </Select>
              ) : null}
            </header>
            {error ? <Notice tone="danger">{error}</Notice> : null}
            {!selected.name.endsWith(".csv") ? (
              <Notice tone="info">Only CSV files can be previewed here. Open this one from the data directory.</Notice>
            ) : null}
            {parsed && series.length > 0 ? (
              <Panel className={styles.plotPanel} bodyClassName={styles.plotBody}>
                <XYPlot series={series} xUnit="s" yUnit={UNIT_BY_COLUMN[column ?? ""] ?? ""} />
              </Panel>
            ) : null}
            {parsed ? (
              <Panel title="Metadata" className={styles.meta} bodyClassName={styles.metaBody}>
                <table className={styles.metaTable}>
                  <tbody>
                    {Object.entries(parsed.metadata).map(([key, value]) => (
                      <tr key={key}>
                        <th>{key}</th>
                        <td className="mono">{value}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Panel>
            ) : null}
          </>
        )}
      </div>
    </div>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} kB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

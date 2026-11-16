// Where the files go on this computer: the folder the backend runs in, which
// a profile's relative data directory resolves against. The shell owns it
// (lib/dataFolder.ts); this picks one. Changing it restarts the backend, so
// it is refused while the session is not idle, and the page reloads after.

import { useState } from "react";
import { createPortal } from "react-dom";
import { useApi } from "../../app/AppContext";
import { changeRefusal, isAbsolutePath, needsReload, type DataFolderState } from "../../lib/dataFolder";
import { invokeShell } from "../../shell/tauri";
import { setDataFolder } from "../../state/dataFolder";
import { useSession } from "../../state/session";
import { Button, Dialog, Field, Input, Notice } from "../ui";
import styles from "./dialogs.module.css";

interface Props {
  state: DataFolderState;
  onClose: () => void;
}

export function DataFolderDialog({ state, onClose }: Props) {
  const api = useApi();
  const session = useSession();
  const [path, setPath] = useState(state.folder);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refusal = session.status === null ? null : changeRefusal(session.status);
  const target = path.trim();

  const browse = async () => {
    setError(null);
    try {
      const picked = await invokeShell<string | null>("pick_data_folder", { start: target });
      if (picked !== null) setPath(picked);
    } catch (reason) {
      setError(String(reason));
    }
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      // Asked now, not read off the screen: the restart would stop a run.
      const refused = changeRefusal(await api.status().catch(() => null));
      if (refused !== null) {
        setError(refused);
        return;
      }
      const next = await invokeShell<DataFolderState>("set_data_folder", { path: target });
      if (needsReload(state, next)) {
        // Another backend: a reload is how the app learns its URL and token.
        window.location.reload();
        return;
      }
      setDataFolder(next);
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  // In a portal: Settings opens this over itself.
  return createPortal(
    <Dialog
      title="Data folder"
      onClose={busy ? undefined : onClose}
      dismissable={!busy}
      footer={
        <>
          <Button variant="ghost" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={busy || refusal !== null || !isAbsolutePath(target)}
            onClick={() => void save()}
          >
            {busy ? "Saving…" : "Save"}
          </Button>
        </>
      }
    >
      {state.unavailable !== null ? <Notice tone="warn">Unavailable: <span className="mono">{state.unavailable}</span></Notice> : null}
      {refusal !== null ? <Notice tone="info">{refusal}</Notice> : null}
      <Field label="Folder" stacked>
        <div className={styles.addressRow}>
          <Input className="mono" value={path} disabled={busy} onChange={(e) => setPath(e.target.value)} />
          <Button disabled={busy} onClick={() => void browse()}>
            Browse…
          </Button>
          <Button disabled={busy || target === state.default_folder} onClick={() => setPath(state.default_folder)}>
            Default
          </Button>
        </div>
      </Field>
      {state.default_is_fallback ? <div className={styles.pathLine}>Documents folder unavailable.</div> : null}
      {state.legacy !== null ? (
        <div className={styles.pathLine}>
          Earlier files: <span className="mono">{state.legacy}</span>
        </div>
      ) : null}
      {error ? <div className={styles.error}>{error}</div> : null}
    </Dialog>,
    document.body,
  );
}

// The backend process ended and the shell noticed. A backend that did not go
// through its shutdown did not turn the output off in order, so the operator
// is told to look at the instrument, and nothing else can be done in the app
// until a new backend is up: the old URL and token lead nowhere.

import { useEffect, useState } from "react";
import { ShellAlert, ShellButton } from "./ShellAlert";
import { BACKEND_EXITED, invokeShell, onShellEvent, type BackendExited } from "./tauri";
import styles from "./shell.module.css";

export function BackendExitAlert() {
  const [exited, setExited] = useState<BackendExited | null>(null);
  const [restarting, setRestarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => onShellEvent<BackendExited>(BACKEND_EXITED, setExited), []);

  if (exited === null) return null;

  const restart = async () => {
    setRestarting(true);
    setError(null);
    try {
      await invokeShell("restart_backend");
      // The app wires itself to one backend at load; a reload is how it
      // learns the new URL and token, and it drops every stale store with it.
      window.location.reload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setRestarting(false);
    }
  };

  return (
    <ShellAlert
      title="The measurement backend has exited"
      tone="danger"
      actions={
        <ShellButton variant="primary" isDefault disabled={restarting} onClick={() => void restart()}>
          {restarting ? "Restarting…" : "Restart backend"}
        </ShellButton>
      }
    >
      <p>The instrument's output state is unknown. Check the front panel.</p>
      <p className={styles.detail}>
        {exited.code === null ? "The process was killed." : `Exit code ${exited.code}.`} Any run in progress has
        stopped.
      </p>
      {error !== null && <p className={styles.error}>{error}</p>}
    </ShellAlert>
  );
}

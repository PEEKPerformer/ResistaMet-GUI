// The simulator answers like the bench instrument, down to the IDN in the
// header. When the shell started the backend against it, this says so for as
// long as the window is open, on top of everything and out of the pointer's
// way, so a simulated reading is never taken for a measured one.

import { useEffect, useState } from "react";
import { invokeShell } from "./tauri";
import styles from "./shell.module.css";

interface ShellBackendInfo {
  simulated?: boolean;
}

export function SimulatedBadge() {
  const [simulated, setSimulated] = useState(false);

  useEffect(() => {
    let cancelled = false;
    invokeShell<ShellBackendInfo>("backend_info")
      .then((info) => {
        if (!cancelled) setSimulated(info.simulated === true);
      })
      // No backend, no readings to mislabel; the startup screen says why.
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  if (!simulated) return null;
  return (
    <div className={styles.simulated} role="status" aria-label="Simulated instrument">
      SIMULATED
    </div>
  );
}

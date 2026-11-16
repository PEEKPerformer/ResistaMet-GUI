// The shell's data folder, read once at start and after each change. Null in
// a plain browser, where there is no shell, and until the shell has answered.

import { useSyncExternalStore } from "react";
import type { DataFolderState } from "../lib/dataFolder";
import { inTauri, invokeShell } from "../shell/tauri";

let state: DataFolderState | null = null;
let requested = false;
const listeners = new Set<() => void>();

export function setDataFolder(next: DataFolderState): void {
  state = next;
  for (const listener of listeners) listener();
}

function load(): void {
  if (requested || !inTauri()) return;
  requested = true;
  invokeShell<DataFolderState>("data_folder_state")
    .then(setDataFolder)
    .catch((reason: unknown) => {
      // Without an answer nothing is offered; the Results view still shows
      // where the files are.
      console.error("data folder:", reason);
    });
}

export function useDataFolder(): DataFolderState | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      load();
      return () => listeners.delete(listener);
    },
    () => state,
    () => state,
  );
}

// "Not now" lasts for this launch: session storage survives a reload of the
// page and not a restart of the app.
const DISMISSED_KEY = "resistamet.dataFolderDismissed";

export function dataFolderDismissed(): boolean {
  try {
    return sessionStorage.getItem(DISMISSED_KEY) === "1";
  } catch {
    return false;
  }
}

export function dismissDataFolder(): void {
  try {
    sessionStorage.setItem(DISMISSED_KEY, "1");
  } catch {
    // storage unavailable: the dialog is asked again after a reload
  }
}

// The data folder as the shell reports it (the `data_folder_state` command),
// and the decisions the UI makes from it. No DOM here, so it can be tested.
//
// The data folder is the directory an installed backend runs in. A profile's
// relative `data_directory` (the default, `measurement_data`) resolves
// against it; an absolute one ignores it.

import type { SessionStatus } from "../generated/session";

export interface DataFolderState {
  /** False in a source checkout: the backend runs at the repository root. */
  managed: boolean;
  /** The directory the backend runs in. */
  folder: string;
  /** Whether `folder` is the operator's stored choice. */
  chosen: boolean;
  default_folder: string;
  /** The default is the app data directory, Documents being unusable. */
  default_is_fallback: boolean;
  /** A stored choice that could not be used when the backend started. */
  unavailable: string | null;
  /** Where versions before the data folder left files, if any are there. */
  legacy: string | null;
}

/** Whether the dialog opens by itself: nothing chosen yet, or the choice
 *  could not be used. `dismissed` is this launch's "not now". */
export function shouldAskForDataFolder(state: DataFolderState | null, dismissed: boolean): boolean {
  if (state === null || !state.managed || dismissed) return false;
  return !state.chosen || state.unavailable !== null;
}

/** Why the folder cannot be changed now, or null when it can. Changing it
 *  restarts the backend, which would stop a run; the shell cannot see the
 *  session, so this is the check. `status` is null when the backend did not
 *  answer, and then a run cannot be ruled out. */
export function changeRefusal(status: Pick<SessionStatus, "state"> | null): string | null {
  if (status === null) return "The backend is not answering.";
  if (status.state !== "idle") return "A run is in progress.";
  return null;
}

/** Whether the backend was restarted by a change, so the page must reload to
 *  learn the new URL and token. Saving the folder already in use does not. */
export function needsReload(before: DataFolderState, after: DataFolderState): boolean {
  return before.folder !== after.folder;
}

export function isAbsolutePath(path: string): boolean {
  return path.startsWith("/") || /^[A-Za-z]:[\\/]/.test(path) || path.startsWith("\\\\");
}

/** Where a profile's `data_directory` lands: itself when absolute, inside
 *  `folder` otherwise. Null when it is relative and the folder is unknown
 *  (a plain browser has no shell to ask). */
export function resolveDataDirectory(value: string, folder: string | null): string | null {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  if (isAbsolutePath(trimmed)) return trimmed;
  if (folder === null || folder === "") return null;
  const separator = folder.includes("\\") && !folder.startsWith("/") ? "\\" : "/";
  return folder.replace(/[\\/]+$/, "") + separator + trimmed.replace(/^\.[\\/]/, "");
}

/** The line under the profile's "Data directory" field: the backend's own
 *  answer while the field shows what is stored, else what the edit would
 *  resolve to. */
export function dataDirectoryHint(
  draft: string,
  stored: string,
  backendRoot: string | null,
  folder: string | null,
): string | null {
  const resolved = draft === stored && backendRoot !== null ? backendRoot : resolveDataDirectory(draft, folder);
  return resolved === null ? null : `Resolves to ${resolved}`;
}

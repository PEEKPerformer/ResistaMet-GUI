// The shell's side of the app: the commands and events of the Tauri process
// that launched the backend. In a plain browser there is no shell, and every
// component in this directory renders nothing.

export function inTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/** Sent when the backend ends without the shell having asked it to. */
export const BACKEND_EXITED = "backend-exited";

/** Sent when the window was asked to close and the shell held it open. */
export const CLOSE_REQUESTED = "close-requested";

export interface BackendExited {
  /** The process's exit code; null when a signal ended it. */
  code: number | null;
  message: string;
}

export async function invokeShell<T>(command: string): Promise<T> {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(command);
}

/** Subscribe to a shell event; the returned function unsubscribes, also when
 *  called before the subscription has finished being set up. */
export function onShellEvent<T>(event: string, handler: (payload: T) => void): () => void {
  let cancelled = false;
  let unlisten: (() => void) | null = null;
  void import("@tauri-apps/api/event").then(async ({ listen }) => {
    const stop = await listen<T>(event, (e) => handler(e.payload));
    if (cancelled) stop();
    else unlisten = stop;
  });
  return () => {
    cancelled = true;
    unlisten?.();
  };
}

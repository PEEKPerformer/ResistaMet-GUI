// Where the backend is. Inside Tauri the shell launched it and knows; in a
// plain browser (UI development against `python -m resistamet_gui.api`) the
// page is told through the query string and remembers it.

export interface BackendInfo {
  url: string;
  token: string;
  pid?: number;
}

const STORAGE_KEY = "resistamet.backend";

function inTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

async function fromTauri(): Promise<BackendInfo> {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<BackendInfo>("backend_info");
}

function fromBrowser(): BackendInfo | null {
  const params = new URLSearchParams(window.location.search);
  const url = params.get("backend");
  const token = params.get("token");
  if (url && token) {
    const info = { url: url.replace(/\/$/, ""), token };
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(info));
    return info;
  }
  const stored = sessionStorage.getItem(STORAGE_KEY);
  return stored ? (JSON.parse(stored) as BackendInfo) : null;
}

/** Resolve the backend once; callers cache the promise. */
export async function discoverBackend(): Promise<BackendInfo> {
  if (inTauri()) return fromTauri();
  const info = fromBrowser();
  if (!info) {
    throw new Error(
      "No backend. Start `python -m resistamet_gui.api --port 8765 --token dev` and open " +
        "this page with ?backend=http://127.0.0.1:8765&token=dev",
    );
  }
  return info;
}

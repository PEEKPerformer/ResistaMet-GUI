// How long a request may go unanswered before the client gives up on it.
//
// Without a limit, a backend that accepts connections but does not answer
// leaves every fetch pending: the status chip never turns to unreachable, and
// a Stop waits in the webview's per-host queue behind the polls stuck ahead
// of it.

/** GET /session answers from memory; slower than this is not answering. */
export const STATUS_TIMEOUT_MS = 5_000;
/** Settings, profiles, results: file and schema work. */
export const DEFAULT_TIMEOUT_MS = 20_000;
/** Routes that wait on the instrument or the bus: a run thread winding down
 *  with the output going off, a VISA scan, an identify on a slow adapter. */
export const INSTRUMENT_TIMEOUT_MS = 60_000;

const INSTRUMENT_PATHS = ["/session/start", "/session/stop", "/session/abort", "/instruments/"];

export function timeoutFor(method: string, path: string): number {
  if (method === "GET" && path === "/session") return STATUS_TIMEOUT_MS;
  if (INSTRUMENT_PATHS.some((prefix) => path.startsWith(prefix))) return INSTRUMENT_TIMEOUT_MS;
  return DEFAULT_TIMEOUT_MS;
}

/** A signal that aborts after `ms`. AbortSignal.timeout where the webview
 *  has it; older WebKit (macOS 12 and before) does not, and without this
 *  every request there would throw before it was sent. */
export function timeoutSignal(ms: number): AbortSignal {
  if (typeof AbortSignal.timeout === "function") return AbortSignal.timeout(ms);
  const controller = new AbortController();
  setTimeout(() => controller.abort(), ms);
  return controller.signal;
}

/** Whether a rejected fetch was the time limit. Nothing else aborts these
 *  requests, so the fallback signal's plain AbortError counts too. */
export function isTimeout(error: unknown): boolean {
  if (typeof error !== "object" || error === null) return false;
  const name = (error as { name?: unknown }).name;
  return name === "TimeoutError" || name === "AbortError";
}

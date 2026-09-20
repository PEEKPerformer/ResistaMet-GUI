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

/** Whether a rejected fetch was the timeout firing. */
export function isTimeout(error: unknown): boolean {
  return typeof error === "object" && error !== null && (error as { name?: unknown }).name === "TimeoutError";
}

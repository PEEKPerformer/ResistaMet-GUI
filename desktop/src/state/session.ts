// The session as the UI sees it: status from the backend, plus what the event
// stream has told us since. One store, read through useSyncExternalStore, so
// every panel agrees on the run state without prop threading.
//
// High-rate data (samples) does not live here. React re-rendering on every
// sample at 50 Hz is exactly the thing to avoid; the plot keeps its own
// buffers (see state/samples.ts) and re-renders on a timer. This store only
// changes when something a panel shows changes.

import { useSyncExternalStore } from "react";
import type { AnyEvent, LogPayload } from "../generated/events";
import type { InstrumentInfo, SessionStatus } from "../lib/api";

export interface LogLine {
  seq: number;
  t: number;
  level: LogPayload["level"] | "error";
  code: string;
  message: string;
}

export interface InstrumentState {
  address: string;
  idn: string;
  model: string;
  maxSourceV: number | null;
  maxSourceI: number | null;
  maxPowerW: number | null;
}

export interface SessionSnapshot {
  status: SessionStatus | null;
  connected: boolean;
  /** Whether the last status request was answered; null before the first. */
  backendReachable: boolean | null;
  instrument: InstrumentState | null;
  /** How the last run ended, kept until the next one starts. Duration and
   *  sample count are null when the backend did not report them. */
  lastRunEnded: {
    reason: string;
    ok: boolean;
    path: string | null;
    durationS: number | null;
    samples: number | null;
  } | null;
  log: LogLine[];
  gap: boolean;
}

const MAX_LOG_LINES = 500;

let snapshot: SessionSnapshot = {
  status: null,
  connected: false,
  backendReachable: null,
  instrument: null,
  lastRunEnded: null,
  log: [],
  gap: false,
};

const listeners = new Set<() => void>();

function publish(next: SessionSnapshot): void {
  snapshot = next;
  for (const listener of listeners) listener();
}

export function getSessionSnapshot(): SessionSnapshot {
  return snapshot;
}

export function useSession(): SessionSnapshot {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getSessionSnapshot,
    getSessionSnapshot,
  );
}

/** Prompts that have had their answer, by id. A status that still names one
 *  was read before the answer landed; showing it again would offer the
 *  operator a button that either does nothing or, a moment later, belongs to
 *  the next prompt. Ids carry the run id, so they do not repeat. */
const answeredPrompts = new Set<string>();

function withoutAnsweredPrompt(status: SessionStatus): SessionStatus {
  const pending = status.pending_prompt;
  if (pending === null || !answeredPrompts.has(pending.prompt_id)) return status;
  return { ...status, pending_prompt: null };
}

/** The prompt has its answer, from here or from another client. */
export function markPromptAnswered(promptId: string): void {
  answeredPrompts.add(promptId);
  if (snapshot.status?.pending_prompt?.prompt_id === promptId) {
    publish({ ...snapshot, status: withoutAnsweredPrompt(snapshot.status) });
  }
}

/** The backend is a new process. Its run and prompt ids start again, so
 *  what was remembered about the old one's would be held against them. */
export function backendRestarted(): void {
  answeredPrompts.clear();
  publish({ ...snapshot, lastRunEnded: null, gap: false });
}

/** The run the pending prompt belongs to, for an answer to name. */
export function promptRunId(promptId: string): string | null {
  const status = snapshot.status;
  return status?.pending_prompt?.prompt_id === promptId ? status.run_id : null;
}

export function setStatus(incoming: SessionStatus): void {
  const status = withoutAnsweredPrompt(incoming);
  // After a reload the events that named the instrument are gone; the backend
  // remembers the last one it saw. Only fills a gap: a live event or an
  // Identify is newer than any status that was in flight.
  const instrument =
    snapshot.instrument === null && status.instrument !== null
      ? instrumentState(status.instrument)
      : snapshot.instrument;
  publish({ ...snapshot, status, instrument, backendReachable: true });
}

export function setBackendReachable(reachable: boolean): void {
  if (snapshot.backendReachable !== reachable) publish({ ...snapshot, backendReachable: reachable });
}

export function setConnected(connected: boolean): void {
  if (snapshot.connected !== connected) publish({ ...snapshot, connected });
}

export function setGap(gap: boolean): void {
  if (snapshot.gap !== gap) publish({ ...snapshot, gap });
}

/** An Identify from the settings dialog is as good a sighting of the
 *  instrument as a run connecting to it, and the header badge shows either. */
export function setIdentifiedInstrument(info: InstrumentInfo): void {
  publish({ ...snapshot, instrument: instrumentState(info) });
}

function instrumentState(info: InstrumentInfo): InstrumentState {
  return {
    address: info.address,
    idn: info.idn,
    model: info.model ?? "?",
    maxSourceV: info.max_source_v,
    maxSourceI: info.max_source_i,
    maxPowerW: info.max_power_w,
  };
}

function appendLog(line: LogLine): LogLine[] {
  const log = snapshot.log.length >= MAX_LOG_LINES ? snapshot.log.slice(-MAX_LOG_LINES + 1) : snapshot.log.slice();
  log.push(line);
  return log;
}

/** Fold one event into the snapshot. Samples are handled elsewhere. */
export function applyEvent(event: AnyEvent): void {
  switch (event.type) {
    case "log":
      // The per-sample "Running …" line is the readout's job; in the log it
      // is noise that scrolls everything else away.
      if (event.payload.code === "progress") return;
      publish({
        ...snapshot,
        log: appendLog({
          seq: event.seq,
          t: event.t,
          level: event.payload.level,
          code: event.payload.code,
          message: event.payload.message,
        }),
      });
      return;
    case "error":
      publish({
        ...snapshot,
        log: appendLog({
          seq: event.seq,
          t: event.t,
          level: "error",
          code: event.payload.code,
          message: event.payload.message,
        }),
      });
      return;
    case "instrument_connected":
      publish({
        ...snapshot,
        instrument: {
          address: event.payload.address,
          idn: event.payload.idn,
          model: event.payload.model,
          maxSourceV: event.payload.max_source_v ?? null,
          maxSourceI: event.payload.max_source_i ?? null,
          maxPowerW: event.payload.max_power_w ?? null,
        },
      });
      return;
    case "run_started":
      publish({ ...snapshot, lastRunEnded: null, gap: false });
      return;
    case "run_ended":
      publish({
        ...snapshot,
        lastRunEnded: {
          reason: event.payload.reason,
          ok: event.payload.ok ?? true,
          path: event.payload.path ?? null,
          durationS: event.payload.duration_s ?? null,
          samples: event.payload.samples ?? null,
        },
      });
      return;
    case "prompt_resolved":
      markPromptAnswered(event.payload.prompt_id);
      return;
    default:
      return;
  }
}

export function clearLog(): void {
  publish({ ...snapshot, log: [] });
}

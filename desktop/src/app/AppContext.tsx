// Wires the backend into the UI once, at the root: discover it, build the API
// client, open the event stream, keep the session store current.
//
// The run state on screen is the backend's status. It is read when a command
// answers (the client reports every reply), when the stream says the run
// changed state, and at a slow cadence as a backstop: a stream can be
// silently dead for the few seconds before the socket notices, and the run
// state is what the operator is looking at.

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { discoverBackend, type BackendInfo } from "../lib/backend";
import { ApiClient } from "../lib/api";
import { EventStream } from "../lib/events";
import type { AnyEvent } from "../generated/events";
import {
  applyEvent,
  backendRestarted,
  getSessionSnapshot,
  markPromptAnswered,
  promptRunId,
  setBackendReachable,
  setConnected,
  setGap,
  setStatus,
} from "../state/session";
import { applySample, getSeries, resetSamples } from "../state/samples";
import { applySweepSegment, resetSweep } from "../state/sweep";
import { applyVdpGeometry, applyVdpResult, resetVdp } from "../state/vdp";
import { applyGeometryWarning, applySpotComplete, applySpotRunEnded, applySpotRunStarted } from "../state/spots";

interface AppServices {
  api: ApiClient;
  stream: EventStream;
  backend: BackendInfo;
  /** Ask the backend for its status and reconnect the stream, now. */
  retryConnection: () => Promise<void>;
}

const ServicesContext = createContext<AppServices | null>(null);

const STATUS_POLL_MS = 2000;

/** Events after which the status the screen holds is out of date. */
const RUN_STATE_EVENTS: ReadonlySet<AnyEvent["type"]> = new Set([
  "run_started",
  "run_ended",
  "paused",
  "resumed",
  "prompt",
  "prompt_resolved",
  "stopping",
] as const);

/** Point the per-run stores at the run an event belongs to.
 *
 *  run_started is the announcement, and carries the mode. But not every run
 *  is announced: van der Pauw runs emit no run_started, and after a reload
 *  late in a long run the history no longer holds it. Events arrive in order,
 *  so a run id the stores are not on is a newer run; without this its
 *  readings were dropped as strays from another run, or joined the previous
 *  run's series. The mode then comes from the backend's status if that is the
 *  run it is on. */
function beginRunIfNew(event: AnyEvent): void {
  const runId = event.run_id ?? null;
  let mode: string | null;
  if (event.type === "run_started") {
    mode = event.payload.mode;
  } else if (runId !== null && runId !== getSeries().runId) {
    const status = getSessionSnapshot().status;
    mode = status !== null && status.run_id === runId ? status.mode : null;
  } else {
    return;
  }
  resetSamples(mode, runId);
  resetSweep(runId);
  resetVdp(runId);
  // A gap is about one run's trace. The stream reports a new run's own gap
  // after the first of its events.
  setGap(false);
}

export function useServices(): AppServices {
  const services = useContext(ServicesContext);
  if (!services) throw new Error("useServices outside <AppProvider>");
  return services;
}

export function useApi(): ApiClient {
  return useServices().api;
}

interface ProviderProps {
  children: ReactNode;
  /** Shown until the backend has been found; receives the error if it was not. */
  fallback: (state: { error: string | null }) => ReactNode;
}

export function AppProvider({ children, fallback }: ProviderProps) {
  const [backend, setBackend] = useState<BackendInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    discoverBackend()
      .then((info) => {
        if (!cancelled) setBackend(info);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const services = useMemo<AppServices | null>(() => {
    if (!backend) return null;
    const api = new ApiClient(backend, { onStatus: setStatus, onPromptAnswered: markPromptAnswered, promptRunId });
    const stream = new EventStream(api);
    const retryConnection = async () => {
      try {
        await api.status();
      } catch {
        setBackendReachable(false);
      }
      stream.retry();
    };
    return { api, stream, backend, retryConnection };
  }, [backend]);

  useEffect(() => {
    if (!services) return;
    const { api, stream } = services;

    const unsubscribe = stream.subscribe((message) => {
      switch (message.kind) {
        case "connection":
          setConnected(message.connected);
          return;
        case "gap":
          setGap(true);
          return;
        case "restarted":
          // Same run ids, another process: nothing on screen is this one's.
          resetSamples();
          resetSweep(null);
          resetVdp(null);
          backendRestarted();
          return;
        case "event":
          beginRunIfNew(message.event);
          if (message.event.type === "sample") applySample(message.event);
          if (message.event.type === "sweep_segment") applySweepSegment(message.event);
          if (message.event.type === "vdp_geometry_complete") applyVdpGeometry(message.event);
          if (message.event.type === "vdp_result") applyVdpResult(message.event);
          if (message.event.type === "run_started") applySpotRunStarted(message.event);
          if (message.event.type === "geometry_warning") applyGeometryWarning(message.event);
          if (message.event.type === "spot_complete") applySpotComplete(message.event);
          if (message.event.type === "run_ended") applySpotRunEnded();
          applyEvent(message.event);
          if (RUN_STATE_EVENTS.has(message.event.type)) void poll(true);
          return;
      }
    });
    let alive = true;
    let polling = false;
    let pollAgain = false;
    // One status request at a time. While one is out the timer's tick is
    // skipped; an event's is remembered and sent when the first returns, so a
    // burst of events (the history after a reload) costs two requests and the
    // last word is still current.
    const poll = async (afterEvent = false): Promise<void> => {
      if (polling) {
        if (afterEvent) pollAgain = true;
        return;
      }
      polling = true;
      try {
        await api.status();
      } catch {
        if (alive) setBackendReachable(false);
      } finally {
        polling = false;
      }
      if (pollAgain && alive) {
        pollAgain = false;
        void poll(true);
      }
    };
    // Status first, then the stream: the history the stream opens with is
    // folded against the run the backend says it is on.
    void poll().then(() => {
      if (alive) stream.open();
    });
    const timer = setInterval(() => void poll(), STATUS_POLL_MS);

    return () => {
      alive = false;
      clearInterval(timer);
      unsubscribe();
      stream.close();
    };
  }, [services]);

  if (!services) return <>{fallback({ error })}</>;
  return <ServicesContext.Provider value={services}>{children}</ServicesContext.Provider>;
}

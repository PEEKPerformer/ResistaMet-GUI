// Wires the backend into the UI once, at the root: discover it, build the API
// client, open the event stream, keep the session store current.
//
// Status is polled at a slow cadence as a backstop; the stream is what keeps
// the UI live. Polling exists because a stream can be silently dead for the
// few seconds before the socket notices, and the run state is what the
// operator is looking at.

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { discoverBackend, type BackendInfo } from "../lib/backend";
import { ApiClient } from "../lib/api";
import { EventStream } from "../lib/events";
import type { AnyEvent } from "../generated/events";
import { applyEvent, getSessionSnapshot, setBackendReachable, setConnected, setGap, setStatus } from "../state/session";
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
    const api = new ApiClient(backend);
    const stream = new EventStream(api);
    const retryConnection = async () => {
      try {
        setStatus(await api.status());
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
          return;
      }
    });
    let alive = true;
    const poll = async () => {
      try {
        const status = await api.status();
        if (alive) setStatus(status);
      } catch {
        if (alive) setBackendReachable(false);
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

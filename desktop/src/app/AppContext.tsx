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
import { applyEvent, getSessionSnapshot, setBackendReachable, setConnected, setGap, setStatus } from "../state/session";
import { applySample, getSeries, resetSamples } from "../state/samples";
import { applySweepSegment, resetSweep } from "../state/sweep";
import { applyVdpGeometry, applyVdpResult, resetVdp } from "../state/vdp";

interface AppServices {
  api: ApiClient;
  stream: EventStream;
  backend: BackendInfo;
  /** Ask the backend for its status and reconnect the stream, now. */
  retryConnection: () => Promise<void>;
}

const ServicesContext = createContext<AppServices | null>(null);

const STATUS_POLL_MS = 2000;

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
          if (message.event.type === "run_started") {
            resetSamples(message.event.payload.mode, message.event.run_id ?? null);
            resetSweep(message.event.run_id ?? null);
            resetVdp(message.event.run_id ?? null);
          }
          if (message.event.type === "sample") {
            // Samples of a run whose run_started the history no longer holds
            // (a reload late in a long run) must not join the previous run's
            // series. They start their own, under the mode the backend
            // reports if this is the run it is on, and under none otherwise.
            const runId = message.event.run_id ?? null;
            if (runId !== getSeries().runId) {
              const status = getSessionSnapshot().status;
              resetSamples(status !== null && status.run_id === runId ? status.mode : null, runId);
            }
            applySample(message.event);
          }
          if (message.event.type === "sweep_segment") applySweepSegment(message.event);
          if (message.event.type === "vdp_geometry_complete") applyVdpGeometry(message.event);
          if (message.event.type === "vdp_result") applyVdpResult(message.event);
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

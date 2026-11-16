// The live event stream, with the reconnect behaviour a lab UI needs.
//
// A WebSocket to /session/events/ws. When it drops — the backend restarted,
// the machine slept — it reconnects with backoff and resumes from the last
// sequence number it saw, so a UI that was watching a run does not lose the
// samples in between. If the backend says it no longer has them, listeners
// get a `gap` and the UI can refetch the file rather than draw a chart with a
// hole it cannot see.

import type { AnyEvent, EventEnvelope } from "../generated/events";
import type { ApiClient } from "./api";

export type StreamMessage =
  | { kind: "event"; event: AnyEvent }
  | { kind: "gap"; sinceSeq: number }
  | { kind: "connection"; connected: boolean };

export type StreamListener = (message: StreamMessage) => void;

const BACKOFF_MS = [250, 500, 1000, 2000, 4000];

export class EventStream {
  private socket: WebSocket | null = null;
  private listeners = new Set<StreamListener>();
  private lastSeq = 0;
  private runId: string | null = null;
  private attempt = 0;
  private closed = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly api: ApiClient) {}

  /** Start streaming. Idempotent. */
  open(): void {
    this.closed = false;
    this.connect();
  }

  close(): void {
    this.closed = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.socket?.close();
    this.socket = null;
  }

  subscribe(listener: StreamListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Forget the cursor: the next run starts its own sequence at 1. */
  resetCursor(runId: string | null = null): void {
    this.lastSeq = 0;
    this.runId = runId;
  }

  private connect(): void {
    if (this.closed) return;
    const socket = new WebSocket(this.api.eventsSocketUrl(this.runId, this.lastSeq));
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      this.emit({ kind: "connection", connected: true });
    };
    socket.onmessage = (message: MessageEvent<string>) => {
      const parsed = JSON.parse(message.data) as EventEnvelope | { type: "gap"; since_seq: number };
      if (parsed.type === "gap") {
        this.emit({ kind: "gap", sinceSeq: (parsed as { since_seq: number }).since_seq });
        return;
      }
      const event = parsed as AnyEvent;
      const runId = event.run_id ?? null;
      // A replay after reconnect can overlap what we already saw.
      if (runId === this.runId && event.seq <= this.lastSeq) return;
      if (runId !== this.runId) {
        this.runId = runId;
        this.lastSeq = 0;
      }
      this.lastSeq = event.seq;
      this.emit({ kind: "event", event });
    };
    socket.onclose = () => {
      if (this.socket === socket) this.socket = null;
      this.emit({ kind: "connection", connected: false });
      this.scheduleReconnect();
    };
    socket.onerror = () => {
      // onclose follows; nothing to do here that it does not.
    };
  }

  private scheduleReconnect(): void {
    if (this.closed) return;
    const delay = BACKOFF_MS[Math.min(this.attempt, BACKOFF_MS.length - 1)] ?? 4000;
    this.attempt += 1;
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }

  private emit(message: StreamMessage): void {
    for (const listener of this.listeners) {
      try {
        listener(message);
      } catch (error) {
        console.error("event listener failed", error);
      }
    }
  }
}

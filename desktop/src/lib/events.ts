// The live event stream, with the reconnect behaviour a lab UI needs.
//
// A WebSocket to /session/events/ws. When it drops — the backend restarted,
// the machine slept — it reconnects with backoff and resumes from the last
// sequence number it saw, so a UI that was watching a run does not lose the
// samples in between. If the backend says it no longer has them, or a run is
// first seen part-way through and its beginning cannot be fetched, listeners
// get a `gap` and the UI says the chart is partial rather than draw one with
// a hole nobody can see. If the backend that comes back is a new process,
// counting its runs from 1 again, listeners get `restarted` and everything is
// read afresh (see streamCursor.ts for how that is told).
//
// A page that has seen nothing yet — first load, or a reload — starts by
// fetching the backend's event history over HTTP and delivering it as if it
// had been streamed, so the log, the instrument badge and a run in progress
// survive a reload. The socket then resumes from where the history ended.

import type { AnyEvent, EventEnvelope } from "../generated/events";
import type { ApiClient } from "./api";
import { judge, type Cursor, type ResumePoint } from "./streamCursor";

export type StreamMessage =
  | { kind: "event"; event: AnyEvent }
  /** Events before this point in the current run were not delivered. */
  | { kind: "gap"; sinceSeq: number }
  /** The backend is a new process: what was shown belongs to the old one. */
  | { kind: "restarted" }
  | { kind: "connection"; connected: boolean };

export type StreamListener = (message: StreamMessage) => void;

const BACKOFF_MS = [250, 500, 1000, 2000, 4000];

/** The backend's history ring holds this many events; ask for all of it. */
const HISTORY_LIMIT = 10000;

export class EventStream {
  private socket: WebSocket | null = null;
  private listeners = new Set<StreamListener>();
  private cursor: Cursor = { runId: null, lastSeq: 0 };
  /** Where the open socket resumed from; null while reading the history. */
  private resume: ResumePoint = null;
  /** Live events waiting for the head of their run to be fetched. */
  private held: AnyEvent[] | null = null;
  private attempt = 0;
  private closed = false;
  private backfilled = false;
  /** Bumped by every open and close, so a fetch that outlives its stream
   *  (a remount, React's strict-mode double effect) is discarded. */
  private generation = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly api: ApiClient) {}

  /** Start streaming. Idempotent. */
  open(): void {
    this.closed = false;
    void this.connect();
  }

  close(): void {
    this.closed = true;
    this.generation += 1;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.dropSocket();
  }

  /** Try now instead of waiting out the backoff. No-op while connected. */
  retry(): void {
    if (this.closed || this.socket) return;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.attempt = 0;
    void this.connect();
  }

  subscribe(listener: StreamListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Let go of the socket without hearing from it again: its onclose would
   *  otherwise report a disconnect and start a second connection. */
  private dropSocket(): void {
    const socket = this.socket;
    this.socket = null;
    if (!socket) return;
    socket.onopen = null;
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;
    socket.close();
  }

  private async connect(): Promise<void> {
    if (this.closed) return;
    const generation = ++this.generation;
    this.held = null;
    if (!this.backfilled) {
      try {
        const page = await this.api.events(0, undefined, HISTORY_LIMIT);
        if (this.closed || generation !== this.generation) return;
        this.resume = null;
        // The history cannot be asked for more than it holds, so a run it
        // picks up part-way through is reported as a gap, not refetched.
        for (const event of page.events) this.accept(event as AnyEvent, false);
        this.backfilled = true;
      } catch {
        // No history without the backend, and no socket either: try both
        // again together, so the history is never skipped and then replayed
        // on top of live events.
        if (this.closed || generation !== this.generation) return;
        this.scheduleReconnect();
        return;
      }
    }
    this.resume = { ...this.cursor };
    const socket = new WebSocket(this.api.eventsSocketUrl(this.cursor.runId, this.cursor.lastSeq));
    this.socket = socket;

    socket.onopen = () => {
      if (this.socket !== socket) return;
      this.attempt = 0;
      this.emit({ kind: "connection", connected: true });
    };
    socket.onmessage = (message: MessageEvent<string>) => {
      if (this.socket !== socket) return;
      let parsed: EventEnvelope | { type: "gap"; since_seq: number };
      try {
        parsed = JSON.parse(message.data) as EventEnvelope | { type: "gap"; since_seq: number };
      } catch (error) {
        console.error("unreadable event stream message", error);
        return;
      }
      if (parsed.type === "gap") {
        this.emit({ kind: "gap", sinceSeq: (parsed as { since_seq: number }).since_seq });
        return;
      }
      this.accept(parsed as AnyEvent, true);
    };
    socket.onclose = () => {
      // A socket this stream has already let go of has nothing to report.
      if (this.socket !== socket) return;
      this.socket = null;
      this.emit({ kind: "connection", connected: false });
      this.scheduleReconnect();
    };
    socket.onerror = () => {
      // onclose follows; nothing to do here that it does not.
    };
  }

  /** Take one event, from the history or the socket. `canRefetch`: the head
   *  of a run first seen part-way through can be asked for. */
  private accept(event: AnyEvent, canRefetch: boolean): void {
    if (this.held) {
      this.held.push(event);
      return;
    }
    switch (judge(this.cursor, event, this.resume)) {
      case "replay":
        return;
      case "restarted":
        this.startOver();
        return;
      case "missed-head":
        if (canRefetch) {
          this.held = [event];
          void this.fetchHead(event.run_id as string);
          return;
        }
        this.deliver(event);
        this.emit({ kind: "gap", sinceSeq: 0 });
        return;
      case "deliver":
        this.deliver(event);
    }
  }

  private deliver(event: AnyEvent): void {
    this.cursor = { runId: event.run_id ?? null, lastSeq: event.seq };
    this.emit({ kind: "event", event });
  }

  /** A run began while the socket was down. Fetch what it said before the
   *  first event seen here; if the backend no longer has all of it, say so. */
  private async fetchHead(runId: string): Promise<void> {
    const generation = this.generation;
    let head: AnyEvent[] = [];
    try {
      const page = await this.api.events(0, runId, HISTORY_LIMIT);
      head = (page.events as AnyEvent[]).filter((event) => event.run_id === runId);
    } catch {
      // Nothing fetched: the gap below covers it.
    }
    if (this.closed || generation !== this.generation || this.held === null) return;
    const held = this.held;
    this.held = null;
    const firstLive = held[0]?.seq ?? 0;
    const firstSeen = head[0]?.seq ?? firstLive;
    for (const event of head) if (event.seq < firstLive) this.deliver(event);
    for (const event of held) this.accept(event, true);
    if (firstSeen > 1) this.emit({ kind: "gap", sinceSeq: 0 });
  }

  /** The backend is a new process. Forget the old one's position, tell the
   *  listeners, and read the new one from its history on. */
  private startOver(): void {
    this.cursor = { runId: null, lastSeq: 0 };
    this.backfilled = false;
    this.emit({ kind: "restarted" });
    this.dropSocket();
    this.emit({ kind: "connection", connected: false });
    void this.connect();
  }

  private scheduleReconnect(): void {
    if (this.closed) return;
    const delay = BACKOFF_MS[Math.min(this.attempt, BACKOFF_MS.length - 1)] ?? 4000;
    this.attempt += 1;
    this.reconnectTimer = setTimeout(() => void this.connect(), delay);
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

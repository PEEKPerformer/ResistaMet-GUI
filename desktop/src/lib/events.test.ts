import { test } from "node:test";
import assert from "node:assert/strict";
import { EventStream, type StreamMessage } from "./events.ts";
import type { ApiClient, EventPage } from "./api.ts";

class FakeSocket {
  static opened: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((message: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  readonly url: string;
  constructor(url: string) {
    this.url = url;
    FakeSocket.opened.push(this);
  }
  close(): void {
    this.closed = true;
    this.onclose?.();
  }
  send(event: object): void {
    this.onmessage?.({ data: JSON.stringify(event) });
  }
}
(globalThis as { WebSocket?: unknown }).WebSocket = FakeSocket;

const ev = (type: string, run_id: string, seq: number) => ({ type, run_id, seq, t: 0, v: 1, payload: {} });
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

/** An api whose history answers come from `pages`, keyed by the run asked for ("" = all). */
function harness(pages: Record<string, object[][]>) {
  FakeSocket.opened = [];
  const asked: string[] = [];
  const api = {
    events(_since: number, runId?: string): Promise<EventPage> {
      const key = runId ?? "";
      asked.push(key);
      const events = pages[key]?.shift() ?? [];
      return Promise.resolve({ events, gap: false, last_seq: 0 } as unknown as EventPage);
    },
    eventsSocketUrl: (runId?: string | null, sinceSeq = 0) => `ws://test/?run_id=${runId ?? ""}&since_seq=${sinceSeq}`,
  } as unknown as ApiClient;
  const stream = new EventStream(api);
  const seen: string[] = [];
  stream.subscribe((message: StreamMessage) => {
    if (message.kind === "event") seen.push(`${message.event.run_id}:${message.event.seq}`);
    else if (message.kind !== "connection") seen.push(message.kind);
  });
  return { stream, seen, asked, socket: () => FakeSocket.opened[FakeSocket.opened.length - 1] as FakeSocket };
}

test("a history that starts part-way through a run is delivered and flagged as a gap", async () => {
  const h = harness({ "": [[ev("sample", "run-3", 40001), ev("sample", "run-3", 40002)]] });
  h.stream.open();
  await flush();
  assert.deepEqual(h.seen, ["run-3:40001", "gap", "run-3:40002"]);
  assert.equal(h.socket().url, "ws://test/?run_id=run-3&since_seq=40002");
  h.stream.close();
});

test("a run that began while the socket was down has its head fetched before the live events", async () => {
  const h = harness({
    "": [[ev("run_started", "run-1", 1), ev("sample", "run-1", 2)]],
    "run-2": [[ev("run_started", "run-2", 1), ev("sample", "run-2", 2), ev("sample", "run-2", 3)]],
  });
  h.stream.open();
  await flush();
  h.socket().send(ev("sample", "run-2", 3));
  h.socket().send(ev("sample", "run-2", 4));
  await flush();
  assert.deepEqual(h.asked, ["", "run-2"]);
  assert.deepEqual(h.seen, ["run-1:1", "run-1:2", "run-2:1", "run-2:2", "run-2:3", "run-2:4"]);
  h.stream.close();
});

test("a head the backend no longer has is a gap", async () => {
  const h = harness({ "": [[ev("run_started", "run-1", 1)]], "run-2": [[ev("sample", "run-2", 7)]] });
  h.stream.open();
  await flush();
  h.socket().send(ev("sample", "run-2", 9));
  await flush();
  assert.deepEqual(h.seen, ["run-1:1", "run-2:7", "run-2:9", "gap"]);
  h.stream.close();
});

test("a restarted backend's run-1 starts everything over instead of being dropped", async () => {
  const h = harness({
    "": [
      [ev("run_started", "run-1", 1), ev("sample", "run-1", 2), ev("sample", "run-1", 3)],
      [ev("run_started", "run-1", 1)],
    ],
  });
  h.stream.open();
  await flush();
  const first = h.socket();
  first.send(ev("run_started", "run-1", 1));
  await flush();
  assert.deepEqual(h.seen, ["run-1:1", "run-1:2", "run-1:3", "restarted", "run-1:1"]);
  assert.equal(first.closed, true);
  assert.equal(FakeSocket.opened.length, 2);
  assert.equal(h.socket().url, "ws://test/?run_id=run-1&since_seq=1");
  h.stream.close();
});

test("a socket that was let go of reports nothing and starts no second connection", async () => {
  const h = harness({ "": [[]] });
  h.stream.open();
  await flush();
  const old = h.socket();
  const onclose = old.onclose;
  h.stream.close();
  assert.equal(old.onclose, null);
  h.stream.open();
  await flush();
  assert.equal(FakeSocket.opened.length, 2);
  // Even a handler that was captured before close() must do nothing now.
  onclose?.();
  await flush();
  assert.equal(FakeSocket.opened.length, 2);
  h.stream.close();
});

test("a message that is not JSON is ignored", async () => {
  const h = harness({ "": [[]] });
  h.stream.open();
  await flush();
  const original = console.error;
  console.error = () => undefined;
  try {
    h.socket().onmessage?.({ data: "not json" });
  } finally {
    console.error = original;
  }
  h.socket().send(ev("sample", "run-1", 1));
  assert.deepEqual(h.seen, ["run-1:1"]);
  h.stream.close();
});

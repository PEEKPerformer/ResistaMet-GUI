// The HTTP side of the backend, one method per route.
//
// Thin on purpose: no caching, no retries, and no state beyond the order the
// session requests went out in. Each method builds a
// request, sends the bearer token, and turns a non-2xx response into an
// ApiError carrying the backend's `detail` so the UI can show the reason the
// backend gave rather than one it made up.

import type { BackendInfo } from "./backend";
import type { ClientInfo, Mode, RunRequest } from "../generated/settings";
import type { EventEnvelope } from "../generated/events";
import type { InstrumentInfo, SessionStatus } from "../generated/session";
import type { SpotMap } from "../generated/maps";
import { version as packageVersion } from "../../package.json";
import { describeDetail } from "./apiDetail";
import { ReplyOrder } from "./replyOrder";
import { isTimeout, timeoutFor } from "./requestTimeout";

/** Written into the header of every file a run started from here produces,
 *  so desktop output can be told from the PySide6 app's. */
const CLIENT: ClientInfo = { name: "resistamet-desktop", version: packageVersion };

// What GET /session and the session commands answer with: generated from the
// backend's SessionStatus model, re-exported here so callers keep one import.
export type { InstrumentInfo, PendingPrompt, SessionStatus } from "../generated/session";
export type SessionState = SessionStatus["state"];

/** What a view hands to start(): the backend's RunRequest, which is the model
 *  POST /session/start validates and which refuses a field it does not know.
 *  `client` is filled in here, not by the views. `spot` is four-point only. */
export type StartRequest = Omit<RunRequest, "client" | "overrides"> & {
  overrides?: Record<string, unknown>;
};

export interface Issue {
  key: string;
  message: string;
  severity: "error" | "warning";
}

export interface Hazard {
  hazardous: boolean;
  voltage_v: number | null;
  threshold_v: number;
  reason: string;
}

export interface Resolved {
  settings: Record<string, Record<string, unknown>>;
  derived: Record<string, unknown>;
  ok: boolean;
  issues: Issue[];
  hazard: Hazard | null;
}

export interface ModeSchema {
  model: string;
  fields: string[];
  override_keys: string[];
}

/** Which VISA implementation answered: a vendor library ("ivi"), pyvisa-py
 *  ("py"), or something that does not say ("unknown"). */
export interface VisaBackend {
  requested: string;
  kind: "ivi" | "py" | "unknown";
  library: string | null;
  version: string | null;
}

export interface ResourceList {
  resources: string[];
  backend: VisaBackend;
  /** The Prologix interface resource that is open, or null. */
  gpib_interface: string | null;
}

export interface EventPage {
  events: EventEnvelope[];
  gap: boolean;
  last_seq: number;
}

export type Profile = Record<string, Record<string, unknown>>;

export interface ResultFile {
  path: string;
  name: string;
  user: string | null;
  size: number;
  modified: number;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
  ) {
    super(detail);
    this.name = "ApiError";
  }
}

/** Where the client reports what the session routes tell it, so the run
 *  state on screen follows a command's reply rather than the next poll. */
export interface ApiHooks {
  /** The status GET /session and every session command answer with. */
  onStatus?: (status: SessionStatus) => void;
  /** The backend accepted an answer to this prompt. */
  onPromptAnswered?: (promptId: string) => void;
  /** The run the UI believes this prompt belongs to, sent with the answer so
   *  the backend can refuse one meant for another run. */
  promptRunId?: (promptId: string) => string | null;
}

export class ApiClient {
  private readonly statusOrder = new ReplyOrder();

  constructor(
    readonly backend: BackendInfo,
    private readonly hooks: ApiHooks = {},
  ) {}

  // --- session -----------------------------------------------------------

  status(): Promise<SessionStatus> {
    return this.sessionRequest("GET", "/session");
  }

  async start(request: StartRequest): Promise<{ run_id: string }> {
    const reply = await this.request<{ run_id: string }>("POST", "/session/start", { ...request, client: CLIENT });
    // Start answers with the run id only. Read the status before returning,
    // so the caller's "busy" does not end on a screen that still says idle.
    await this.status().catch(() => undefined);
    return reply;
  }

  stop(): Promise<SessionStatus> {
    return this.sessionRequest("POST", "/session/stop");
  }

  abort(): Promise<SessionStatus> {
    return this.sessionRequest("POST", "/session/abort");
  }

  pause(): Promise<SessionStatus> {
    return this.sessionRequest("POST", "/session/pause");
  }

  resume(): Promise<SessionStatus> {
    return this.sessionRequest("POST", "/session/resume");
  }

  mark(label = "MARK"): Promise<SessionStatus> {
    return this.sessionRequest("POST", "/session/mark", { label });
  }

  async answerPrompt(
    promptId: string,
    choice: string,
    fields: Record<string, unknown> = {},
  ): Promise<SessionStatus> {
    const body: { prompt_id: string; choice: string; fields: Record<string, unknown>; run_id?: string } = {
      prompt_id: promptId,
      choice,
      fields,
    };
    const runId = this.hooks.promptRunId?.(promptId) ?? null;
    if (runId !== null) body.run_id = runId;
    const ticket = this.statusOrder.sent();
    const status = await this.request<SessionStatus>("POST", "/session/prompt", body);
    // Before the status: the reply may still name the prompt it answered.
    this.hooks.onPromptAnswered?.(promptId);
    if (this.statusOrder.accepts(ticket)) this.hooks.onStatus?.(status);
    return status;
  }

  events(sinceSeq = 0, runId?: string, limit = 500): Promise<EventPage> {
    const params = new URLSearchParams({ since_seq: String(sinceSeq), limit: String(limit) });
    if (runId) params.set("run_id", runId);
    return this.request("GET", `/session/events?${params}`);
  }

  /** The WebSocket URL for the live stream, resuming from a cursor if given. */
  eventsSocketUrl(runId?: string | null, sinceSeq = 0): string {
    const url = new URL(this.backend.url.replace(/^http/, "ws"));
    url.pathname = "/session/events/ws";
    url.searchParams.set("token", this.backend.token);
    if (runId) url.searchParams.set("run_id", runId);
    if (sinceSeq) url.searchParams.set("since_seq", String(sinceSeq));
    return url.toString();
  }

  // --- settings ----------------------------------------------------------

  users(): Promise<{ users: string[]; last_user: string | null }> {
    return this.request("GET", "/users");
  }

  /** Create a profile, or select an existing one as last_user. */
  addUser(username: string): Promise<{ users: string[]; last_user: string }> {
    return this.request("POST", "/users", { username });
  }

  profile(username: string): Promise<Profile> {
    return this.request("GET", `/profiles/${encodeURIComponent(username)}`);
  }

  patchProfile(username: string, sections: Partial<Profile>): Promise<Profile> {
    return this.request("PATCH", `/profiles/${encodeURIComponent(username)}`, sections);
  }

  schema(): Promise<{ modes: Record<Mode, ModeSchema> }> {
    return this.request("GET", "/schema/settings");
  }

  resolve(
    mode: Mode,
    username: string,
    overrides: Record<string, unknown>,
    strict = true,
  ): Promise<Resolved> {
    return this.request("POST", "/settings/resolve", { mode, username, overrides, strict });
  }

  // --- results -----------------------------------------------------------

  results(user?: string | null): Promise<{ root: string; files: ResultFile[] }> {
    const params = new URLSearchParams();
    if (user) params.set("user", user);
    const query = params.toString();
    return this.request("GET", `/results${query ? `?${query}` : ""}`);
  }

  async resultFile(path: string): Promise<string> {
    const response = await fetch(`${this.backend.url}/results/file?path=${encodeURIComponent(path)}`, {
      headers: { Authorization: `Bearer ${this.backend.token}` },
    });
    if (!response.ok) {
      let detail = response.statusText;
      try {
        detail = describeDetail(((await response.json()) as { detail?: unknown }).detail, detail);
      } catch {
        // keep status text
      }
      throw new ApiError(response.status, detail);
    }
    return response.text();
  }

  resultsDirectory(): Promise<{ root: string; exists: boolean; separator: string }> {
    return this.request("GET", "/results/directory");
  }

  // --- four-point maps ---------------------------------------------------

  /** The map ids the operator's four-point runs name. */
  maps(user: string): Promise<{ user: string; maps: string[] }> {
    return this.request("GET", `/maps?user=${encodeURIComponent(user)}`);
  }

  /** The map as the run files describe it now. 404 until a run names it. */
  map(mapId: string, user: string): Promise<SpotMap> {
    return this.request("GET", `/maps/${encodeURIComponent(mapId)}?user=${encodeURIComponent(user)}`);
  }

  // --- instruments -------------------------------------------------------

  /** `visaLibrary` / `gpibInterface` undefined = this machine's saved value. */
  resources(visaLibrary?: string, gpibInterface?: string): Promise<ResourceList> {
    const params = new URLSearchParams();
    if (visaLibrary !== undefined) params.set("visa_library", visaLibrary);
    if (gpibInterface !== undefined) params.set("gpib_interface", gpibInterface);
    const encoded = params.toString();
    const query = encoded ? `?${encoded}` : "";
    return this.request("GET", `/instruments/resources${query}`);
  }

  identify(address: string, visaLibrary?: string, gpibInterface?: string): Promise<InstrumentInfo> {
    const body: { address: string; visa_library?: string; gpib_interface?: string } = { address };
    if (visaLibrary !== undefined) body.visa_library = visaLibrary;
    if (gpibInterface !== undefined) body.gpib_interface = gpibInterface;
    return this.request("POST", "/instruments/identify", body);
  }

  // --- transport ---------------------------------------------------------

  /** A route that answers with the session's status: report it, unless a
   *  request sent after this one has already been answered. */
  private async sessionRequest(method: string, path: string, body?: unknown): Promise<SessionStatus> {
    const ticket = this.statusOrder.sent();
    const status = await this.request<SessionStatus>(method, path, body);
    if (this.statusOrder.accepts(ticket)) this.hooks.onStatus?.(status);
    return status;
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = { Authorization: `Bearer ${this.backend.token}` };
    const init: RequestInit = { method, headers };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    init.signal = AbortSignal.timeout(timeoutFor(method, path));
    let response: Response;
    try {
      response = await fetch(`${this.backend.url}${path}`, init);
    } catch (error) {
      if (isTimeout(error)) throw new ApiError(0, "The backend did not answer in time.");
      throw error;
    }
    if (!response.ok) {
      let detail = response.statusText;
      try {
        detail = describeDetail(((await response.json()) as { detail?: unknown }).detail, detail);
      } catch {
        // no JSON body; keep the status text
      }
      throw new ApiError(response.status, detail);
    }
    return (await response.json()) as T;
  }
}

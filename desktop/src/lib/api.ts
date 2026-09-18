// The HTTP side of the backend, one method per route.
//
// Thin on purpose: no caching, no retries, no state. Each method builds a
// request, sends the bearer token, and turns a non-2xx response into an
// ApiError carrying the backend's `detail` so the UI can show the reason the
// backend gave rather than one it made up.

import type { BackendInfo } from "./backend";
import type { Mode } from "../generated/settings";
import type { EventEnvelope } from "../generated/events";

export type SessionState =
  | "idle"
  | "identifying"
  | "running"
  | "paused"
  | "awaiting_prompt"
  | "stopping";

export interface PendingPrompt {
  prompt_id: string;
  kind: "vdp_geometry" | "safety_voltage_ack" | "cable_null_shorted";
  options: string[];
  requires_human: boolean;
  detail: Record<string, unknown>;
}

// Mirrors MeasurementSession.status(). Not part of the exported contract yet;
// when it is, this moves to src/generated.
export interface SessionStatus {
  state: SessionState;
  run_id: string | null;
  mode: Mode | null;
  path: string | null;
  last_seq: number;
  pending_prompt: PendingPrompt | null;
}

export interface StartRequest {
  mode: Mode;
  sample_name: string;
  username: string;
  overrides?: Record<string, unknown>;
  prompt_timeout_s?: number;
}

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

export interface InstrumentInfo {
  address: string;
  idn: string;
  model: string | null;
  max_source_v: number | null;
  max_source_i: number | null;
  max_power_w: number | null;
}

export interface EventPage {
  events: EventEnvelope[];
  gap: boolean;
  last_seq: number;
}

export type Profile = Record<string, Record<string, unknown>>;

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
  ) {
    super(detail);
    this.name = "ApiError";
  }
}

export class ApiClient {
  constructor(readonly backend: BackendInfo) {}

  // --- session -----------------------------------------------------------

  status(): Promise<SessionStatus> {
    return this.request("GET", "/session");
  }

  start(request: StartRequest): Promise<{ run_id: string }> {
    return this.request("POST", "/session/start", request);
  }

  stop(): Promise<SessionStatus> {
    return this.request("POST", "/session/stop");
  }

  abort(): Promise<SessionStatus> {
    return this.request("POST", "/session/abort");
  }

  pause(): Promise<SessionStatus> {
    return this.request("POST", "/session/pause");
  }

  resume(): Promise<SessionStatus> {
    return this.request("POST", "/session/resume");
  }

  mark(label = "MARK"): Promise<SessionStatus> {
    return this.request("POST", "/session/mark", { label });
  }

  answerPrompt(
    promptId: string,
    choice: string,
    fields: Record<string, unknown> = {},
  ): Promise<SessionStatus> {
    return this.request("POST", "/session/prompt", { prompt_id: promptId, choice, fields });
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

  // --- instruments -------------------------------------------------------

  resources(): Promise<{ resources: string[] }> {
    return this.request("GET", "/instruments/resources");
  }

  identify(address: string): Promise<InstrumentInfo> {
    return this.request("POST", "/instruments/identify", { address });
  }

  // --- transport ---------------------------------------------------------

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = { Authorization: `Bearer ${this.backend.token}` };
    const init: RequestInit = { method, headers };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const response = await fetch(`${this.backend.url}${path}`, init);
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const parsed = (await response.json()) as { detail?: unknown };
        if (parsed.detail !== undefined) {
          detail =
            typeof parsed.detail === "string" ? parsed.detail : JSON.stringify(parsed.detail);
        }
      } catch {
        // no JSON body; keep the status text
      }
      throw new ApiError(response.status, detail);
    }
    return (await response.json()) as T;
  }
}

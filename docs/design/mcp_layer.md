# MCP layer: an AI agent as a client of the measurement API

**Status:** design, 2026-10-07. A1–A4 and M1–M3 (§10) are built, with
`docs/mcp.md`; the desktop's Settings toggle and limits section were built
with A1 and A3. Tested against the simulator only.
**Depends on:** `tauri_backend_split.md` (session, events, the API and its
decision D4), `four_point_probe_spots.md` (spots and maps).

## 1. Goal and non-goals

**Goal.** An AI agent (Claude Code, Claude Desktop, any MCP client) can carry a
measurement from start to finish through the same API the desktop app uses:

- find the instrument;
- choose and check the settings;
- start the run, watch it, stop it;
- read the results.

A person at the bench stays in charge of what only a person can know, and the
instrument can do no more for an agent than the operator has allowed.

**Non-goals.**
- **No raw SCPI, ever.** The agent gets the session's commands, not the bus.
  Every SCPI quirk the run layer works around (`instrument.py`,
  `session/configure.py`) stays behind it.
- **No second measurement path.** The MCP server is a client of the API
  (§7.4 of the split design). It holds no instrument, no run lock and no
  event ring of its own.
- **Not the PySide6 window.** That window does not use the API. An agent works
  with the desktop app or with a bare `python -m resistamet_gui.api` backend.
- **Not the second instrument yet.** The thermoelectric rig and the
  serial-bus work come later. Nothing here may assume one instrument forever
  (§9), but nothing here builds the second one.

## 2. What already exists

- **Commands.** `MeasurementSession` covers start, stop, abort, pause, resume,
  mark, `answer_prompt`, identify and status. The API exposes each one as a
  one-line route (`api/routes_session.py`, `routes_settings.py`).
- **Events.** One typed stream (`session/events.py`) feeds two consumers: a
  pull route `GET /session/events?run_id=&since_seq=` with gap reporting, made
  for request/response clients, and the WebSocket.
- **Prompts.** Every blocking operator decision is data. A prompt marked
  `requires_human` can be answered only by the `ui` role (D4); any other role
  gets 403. Today every prompt is `requires_human`: the touch-safety
  acknowledgement and the vdP rewire.
- **Role checks on profile writes.** The touch-safety keys and a VISA library
  path can be changed only by the `ui` role (`routes_settings.py`).
- **Provenance.** `RunRequest.client` (name, version) goes into the file
  header. It is self-reported.
- **One process per instrument.** An OS lock per address
  (`session/instrument_lock.py`) means an agent can never interleave SCPI with
  a PySide6 window or a script.

What is missing:

- a second credential;
- a way for the MCP server to find the backend;
- limits an agent cannot raise;
- the MCP server itself;
- an audit log.

## 3. Topology

```
 MCP client (Claude Code / Desktop)
        │ stdio
 python -m resistamet_gui.mcp          ← this design; a thin HTTP client
        │ HTTP, Bearer <agent token>
 backend (desktop sidecar, or python -m resistamet_gui.api)
        │ HTTP + WS, Bearer <ui token>
 desktop window                         ← the person at the bench
```

**M1. The MCP server is a separate, stdio-launched process that talks HTTP to
a backend that is already running.** It does not start a backend of its own.

- **Why separate.** An MCP client launches its servers itself, with its own
  lifetime and its own stdout. The desktop launches the backend with a
  different lifetime and reads the handshake from that stdout. Neither can
  host the other cleanly.
- **Why not self-starting.** A backend the agent started has no window, so no
  one could answer a `requires_human` prompt. The run would sit until
  `prompt_timeout_s` and abort. Worse, it would look as if the agent could run
  the instrument alone. For a hardware-free trial,
  `python -m resistamet_gui.api --simulate --allow-agents` is one command.

## 4. Credential and discovery

**M2. The backend mints a second token, role `agent`, only when agents are
allowed. It writes the token to a connection file that only the user can
read.**

- **The switch.** `allow_agents` is a machine-local setting, like
  `visa_library`, off by default. Only the `ui` role can change it. The desktop
  Settings shows it as "Allow AI agents to connect". The `--allow-agents` flag
  does the same for a bare backend.
- **When it is on.** At start the backend writes
  `~/.resistamet/api/connection.json` with mode 0600 and these fields:
  `{url, agent_token, pid, started}`. It removes the file on shutdown. A stale
  file whose `pid` is gone, or whose URL refuses the token, means no backend:
  the MCP server says so and does nothing else.
- **`ApiState` changes** from one token and role to a `{token: role}` map.
  `require_token` returns the matched role. Every existing role check already
  compares against `UI_ROLE`, so `agent` is refused wherever the `ui` role is
  required, with no further change.
- **The agent token never crosses stdout.** The desktop reads the handshake
  line, and the agent token has no business there.

This is not authentication, as the split design already says of the `ui`
token. It keeps other local programs, and agents the user did not configure,
off the instrument.

## 5. What an agent may and may not do

**M3. The `agent` role is refused only what protects people or the machine:**

| Refused (403) | Why |
|---|---|
| Answering a `requires_human` prompt | D4: an agent cannot know leads were rewired or that people are clear of a hazardous voltage |
| Changing the touch-safety keys in a profile | they decide whether a person is asked at all |
| Changing the agent limits (M4) or `allow_agents` | an agent must not raise its own ceiling or let agents in |
| Setting a VISA library path | a path is loaded into the backend process as code |

Everything else is open to an agent: reads, resolve, identify, start within the
limits, stop, abort, pause, resume, mark, profile edits of any other key, new
users, map photographs and shutdown. **Stopping is never restricted.** Either
side may end any run, whoever started it. A profile edit by an agent is a
change the operator will see in the window; the run records what it ran with
either way.

**M4. Agent limits: a per-profile envelope only the UI can set. A start from
the `agent` role is refused, before anything opens, when its resolved settings
go beyond the envelope.**

```
agent_limits:
  max_voltage_v: 30.0   # largest source V or V compliance an agent may set
  max_current_a: null   # largest source I or I compliance; null = the instrument's
  max_power_w:   null   # largest V × I the run could deliver; null = the instrument's
```

- **What is checked.** The check runs on the resolved settings, the same
  values the hazard check sees. For each mode it takes the worst case of what
  the run could put on the device. A sweep takes its whole range. A current
  source takes its compliance as the voltage.
- **Defaults: touch-safe, and no further.** 30 V is the IEC 61010-1 SELV
  bound the touch-safety check already uses. Current and power are not capped
  by default beyond the instrument's own limits: below 30 V they are a matter
  for the sample and the experiment, and the user is a scientist who knows the
  sample. Any of the three can be set, raised or cleared from the window,
  per profile.
- **Raising the voltage limit.** It does not remove the person from the loop.
  Above the profile's touch-safety threshold, M5 applies to every such run.
- **Hardware limits.** `ModelSpec` limits are checked too: an agent asking a
  2400 for 3 A gets a clear refusal, not a SCPI error.
- **Errors.** A refusal is a 422 that names the key, the value and the limit.
  An agent can then correct itself, or tell the person what it would need.
- **Who it applies to.** The envelope applies only to the `agent` role. The
  window is unaffected.

**M5. An agent-started run always asks the touch-safety question.** It does
so even on a profile where a person once ticked "don't show again".

The silence was given by a person, for runs they start themselves. It does not
carry over to a run an agent chose. With the M4 defaults it comes up only at
exactly 30 V, the threshold itself. Once someone raises `max_voltage_v`,
every agent run at or above the profile's threshold waits for a person at the
window, as a vdP run does at each rewire.

**M6. The server stamps the role into the run.** `RunRequest.client` stays
self-reported. The route adds `started_by: <role>`, which the client cannot
set, to the `run_started` payload, the session status and the file header
(`# started_by: agent`, in the header's own `key: value` form).

- The desktop shows a banner on a run an agent started.
- The results browser can filter on `started_by` once `GET /results` reads
  file headers; today it lists files by name, size and date only.

## 6. Tools

Tools map onto the routes one to one, with two exceptions:
- **`wait_for`.** A long-poll, so an agent does not spin.
- **The summaries.** An agent's context must not fill with samples.

| Tool | Route(s) | Notes |
|---|---|---|
| `get_status` | `GET /session`, `GET /health` | state, run, pending prompt (with `requires_human`), simulate flag, backend version |
| `list_instruments` | `GET /instruments/resources` | |
| `identify_instrument(address)` | `POST /instruments/identify` | model and its limits |
| `list_users`, `get_profile(user)` | `GET /users`, `GET /profiles/{user}` | read-only |
| `describe_mode(mode)` | `GET /schema/settings` | the keys of one mode, with bounds, defaults and units; not the whole schema |
| `check_settings(user, mode, overrides)` | `POST /settings/resolve` + M4 | the resolved settings, issues, warnings (what the run will warn about), derived values, hazard, and whether the agent limits allow it; the dry run an agent should always do first |
| `start_run(user, mode, sample_name, overrides, spot?)` | `POST /session/start` | 422 with issues; 409 if busy or the bus is held |
| `stop_run`, `abort_run`, `pause_run`, `resume_run` | same | |
| `mark_event(label)` | `POST /session/mark` | |
| `wait_for(until, timeout_s ≤ 120, then_stop?)` | polls `GET /session` | `until` ∈ `run_ended`, `prompt`, `samples:N`, `state:<s>`; returns on whichever comes first, with a status snapshot; `then_stop` stops the run when the condition holds and waits for its end, the way to take N readings in a mode without a count |
| `get_run_events(since_seq, types?)` | `GET /session/events` | lifecycle, errors, prompts, compliance, overpower and results; samples are left out unless asked for, and then decimated to at most 200 |
| `get_run_summary(run_id?, first_rows?)` | the run's file via `GET /results/file` | count, mean, SD, min, max and last per column; compliance count; marks; the run's metadata block; `first_rows` limits it to the first N rows |
| `list_results(user?, sample?)`, `read_result(path, rows?)` | `GET /results` | `read_result` returns the header and a bounded slice of rows, never a whole file |
| `get_map(map_id)` | `GET /maps/{map_id}` | four-point spots |

- **No `answer_prompt` tool.** Every prompt today is `requires_human`. The tool
  arrives with the first prompt that is not.
- **What a pending prompt returns.** `get_status` and `wait_for` give its text,
  its options, and "a person must answer this at the ResistaMet window". Then
  an agent can say that to the user rather than retry.
- **Summaries come from the file, not the ring.** The ring sheds samples first
  (3000 events), so on a long run it no longer holds them all. The file holds
  every row, and it is the record a paper cites.
- **Tool descriptions are part of the contract.** Each says which unit, which
  role limit and which prompt applies, so an agent learns the rules from the
  tools themselves.

## 7. Audit log

**M7. The MCP server writes one JSONL line per tool call** to
`~/.resistamet/logs/mcp/<date>.jsonl`. Each line holds:
- the time;
- the MCP client's name and version (from `initialize`);
- the tool, its arguments and its result, with large results truncated and
  marked;
- the HTTP status;
- the `run_id`, if there is one.

The file header records the run, and this log records what the agent asked
for and was told.

The agent's reasoning lives in the MCP client's transcript, not here. A paper's
data statement needs both, so `docs/mcp.md` says how to keep the client's
transcript.

## 8. Packaging and tests

- **Packaging.**
  - `resistamet_gui/mcp/` holds `__main__.py`, `client.py` (HTTP), `tools.py`,
    `waiting.py` (`wait_for`, the event digest), `summary.py`, `audit.py` and
    `server.py`.
  - It ships as the optional extra `mcp = ["mcp>=2,<3", "httpx2"]`: httpx2 is
    the HTTP client the SDK itself depends on, so there is no second one. The
    official Python SDK needs Python 3.10 or later, as the GPIB-USB driver
    already does.
  - The package does not import `session` or `api`: it is an HTTP client. An
    AST test enforces that, as for `gpib_usb`.
  - The desktop's frozen backend does not bundle it. A user installs it with
    pip and points the MCP client at `python -m resistamet_gui.mcp`.
    `docs/mcp.md` gives the Claude Code and Claude Desktop configuration.
- **Tests.**
  - API: the role map, every 403 in M3, the M4 refusals (one per mode, at the
    limit and just over it), the M5 forced prompt, and the M6 stamp.
  - MCP: each tool runs against an in-process API on the simulator, through
    the SDK's in-memory client.
  - End to end: a run is started, waited for, summarised and stopped. A
    hazardous request is refused. A vdP run leaves the agent at a
    `requires_human` prompt it cannot pass.
  - No test needs a model.

## 9. Later, not now

- **The second instrument.** Tools will take an optional `instrument` once the
  backend has more than one session. Until then they act on the one session.
- **An `answer_prompt` tool** for prompts that are not `requires_human`, for
  example "the sample looks drifted, extend the run?".
- **A profile-editing tool.** The API lets an agent edit every profile key but
  the protected ones (M3), and add users. The MCP server has no tool for it
  yet: an agent changes a run through its overrides, which leave the profile
  as the operator set it. A `set_profile` tool over `PATCH /profiles/{user}`
  may follow.
- **Bounds and units in `GET /schema/settings`.** Done after the first
  usability trial (2026-10-08): the route serves each key's JSON-schema
  fragment and a unit from its name, and the values a mode fixes;
  `describe_mode` gives one line per key.
- **The backend's version and the simulate flag** in `GET /health` or the
  status, which `get_status` would pass on. Neither is served today.
- **Unattended agent runs.** Today a hazardous or vdP run needs the window.
  The 25-hour weekend run on the thermoelectric rig shows the demand for
  unattended runs. If that becomes the need, it is a separate decision with
  its own UI-only switch, not a loophole in this one.

## 10. PR plan

| PR | Content | Behaviour change |
|---|---|---|
| A1 | `ApiState` token→role map; `allow_agents` setting (UI-only); `--allow-agents`; connection file | none unless switched on |
| A2 | M3 refusals for `agent` | none for `ui` |
| A3 | `agent_limits` in the profile schema, UI-only writes, M4 check at start, ModelSpec check for agents | none for `ui` |
| A4 | M5 forced prompt; M6 `started_by` in the event, the header and the desktop banner | header line added |
| M1 | `resistamet_gui/mcp` skeleton: discovery, client, `get_status`, read tools, audit log | new extra |
| M2 | run tools, `wait_for`, `get_run_events` | |
| M3 | `get_run_summary`, results and map tools | |
| M4 | `docs/mcp.md`, the desktop's Settings toggle and limits section | |

A1–A4 are backend PRs, each reviewable alone and each a no-op for the window.
M1–M4 add a package nothing else imports.

## 11. Decisions (owner, 2026-10-07)

1. **M4 defaults:** 30 V, the touch-safety bound; current and power uncapped
   beyond the instrument. All three can be overridden per profile from the
   window. Make the user safe without limiting what a scientist can do.
2. **Agent restrictions:** only what protects people or the machine (M3).
   An agent may edit the rest of a profile, add users, store map photographs
   and shut the backend down.
3. **An agent may start a run with no window open,** within the envelope.
   Prompts still need the window.

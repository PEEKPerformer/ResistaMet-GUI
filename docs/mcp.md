# AI agents (MCP)

`python -m resistamet_gui.mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server. With it an AI agent such as Claude Code or Claude Desktop can carry a measurement from start to finish: find the instrument, choose and check the settings, start the run, follow it, stop it and read the results. It does so through the [backend API](api.md), with a token of its own, so it is held to rules a person sets and refused what only a person at the bench can do.

Status: built on the development branch and tested against the in-package simulator, through the MCP Python SDK's in-memory client and over stdio. Not yet used on hardware or with a model in the loop. The design is in [`docs/design/mcp_layer.md`](https://github.com/PEEKPerformer/ResistaMet-GUI/blob/main/docs/design/mcp_layer.md).

## How it fits

```
 MCP client (Claude Code, Claude Desktop, …)
        │ stdio
 python -m resistamet_gui.mcp            the MCP server: a thin HTTP client
        │ HTTP, agent token
 backend (desktop app, or python -m resistamet_gui.api)
        │ HTTP + WebSocket, ui token
 desktop window                          the person at the bench
```

The MCP client starts the MCP server. The MCP server does not start a backend: it uses the one that is running, found through the [connection file](api.md#agent-access) the backend writes while agent access is on. A backend an agent started itself would have no window, and the prompts only a person can answer would go unanswered. The MCP server holds no instrument and talks no SCPI; everything goes through the same routes the desktop app uses, and the PySide6 window is not involved.

## Install

```bash
pip install -e ".[mcp]"        # the MCP SDK (Python 3.10 or later)
```

The desktop app's bundled backend does not include the MCP server. Install it into a Python environment as above, whichever backend you use.

## Let agents in

Agent access is off by default. Turn it on in one of two ways:

- **Desktop app:** Settings → AI agents → *Allow AI agents to connect*. This is stored for this PC.
- **Bare backend:** `python -m resistamet_gui.api --allow-agents --config config.json` (add `--simulate` to try it without an instrument). This lasts as long as that process.

Either way the backend writes `~/.resistamet/api/connection.json`, readable by you only, and removes it when it stops or access is turned off. Turning access off withdraws the agent's token at once. If the MCP server finds no backend, its tools say so and say how to turn access on.

## Configure the MCP client

Use the Python that has the `mcp` extra installed: the one from your virtual environment, by its full path.

**Claude Code:**

```bash
claude mcp add resistamet -- /path/to/venv/bin/python -m resistamet_gui.mcp
```

**Claude Desktop:** add the server to `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`) and restart the app:

```json
{
  "mcpServers": {
    "resistamet": {
      "command": "/path/to/venv/bin/python",
      "args": ["-m", "resistamet_gui.mcp"]
    }
  }
}
```

On Windows the command is `C:\\path\\to\\venv\\Scripts\\python.exe`.

Options: `--connection-file PATH` reads a connection file other than the default, and `--audit-dir PATH` puts the [audit log](#audit-log) elsewhere. The server logs to stderr; stdout carries the protocol.

## Tools

Each tool maps onto one or two API routes. Values are SI (V, A, Ω, s, Hz). Results are compact JSON; samples are summarised, never streamed into the conversation.

| Tool | What it does | Route |
|---|---|---|
| `get_status` | State, current or last run (with `started_by` and its data file), instrument and its limits, pending prompt | `GET /session`, `GET /health` |
| `list_instruments` | VISA resources this PC sees (idle only) | `GET /instruments/resources` |
| `identify_instrument(address)` | Model and its limits from `*IDN?` (idle only) | `POST /instruments/identify` |
| `list_users`, `get_profile(user)` | Operators; one profile's stored settings, including its agent limits, without the machine's `allow_agents` switch (a connected agent has access whatever it says) | `GET /users`, `GET /profiles/{user}` |
| `describe_mode(mode, user?)` | One line per key a mode takes: the value a user's run would have, in its unit, and whether it comes from the profile or is fixed by the mode; the default; the choices or bounds; what it means. For `vdp`, the prompts a person must answer | `GET /schema/settings`, `POST /settings/resolve`, `GET /profiles/{user}` |
| `check_settings(user, mode, overrides?)` | Dry run: resolved values, issues, warnings (what the run will warn about: a sampling rate the timing cannot reach, four-point power above its warning threshold), derived values, the touch-safety check and the agent-limit verdict. First comes `can_start`: whether `start_run` would accept the settings from the agent (valid and within its limits); `ok` says only that the settings are valid | `POST /settings/resolve` |
| `start_run(user, mode, sample_name, overrides?, spot?, prompt_timeout_s?)` | Start a run; returns the run id at once | `POST /session/start` |
| `stop_run`, `abort_run`, `pause_run`, `resume_run` | As the routes; stopping is always allowed | `POST /session/…` |
| `mark_event(label)` | A label in the next data row's event column | `POST /session/mark` |
| `wait_for(until, timeout_s ≤ 120, then_stop?)` | Wait for `run_ended`, `prompt`, `samples:N` or `state:<state>`; also returns early at a prompt the agent has not been shown or when no run is in progress, saying which. A prompt it has been shown does not end the wait ([waiting for a person](#waiting-for-a-person)). With `then_stop`, stops the run as soon as the condition holds and returns once it has ended | polls `GET /session`; `POST /session/stop` |
| `get_run_events(since_seq?, run_id?, types?, include_samples?, max_samples ≤ 200)` | What happened in a run; samples and progress logs left out unless asked for, samples thinned to at most 200 | `GET /session/events` |
| `get_run_summary(run_id?, path?, first_rows?)` | Per numeric column: unit, count, mean, SD, min, max, last; compliance rows; marks; header and end block. From the data file, during or after the run. `first_rows=N`: over the first N data rows only | `GET /results/file` |
| `list_results(user?, sample?)`, `read_result(path, offset?, rows ≤ 500)` | Data files; one file's header and a slice of its rows | `GET /results`, `GET /results/file` |
| `list_maps(user)`, `get_map(map_id, user)` | Four-point maps | `GET /maps`, `GET /maps/{map_id}` |

There is deliberately no tool to answer a prompt (every prompt today needs a person), to send SCPI, to shut the backend down (it belongs to the window), or to edit a profile. The API lets an agent edit profile keys other than the protected ones; a tool for it may follow.

Summaries read plain `.csv` files only: a compressed `.csv.gz` or an HDF5 file is reported as such.

### A fixed number of readings

Four-point runs stop by themselves after `fpp_samples`; resistance, source V and source I run until they are stopped. To take N readings in those, start the run and call `wait_for("samples:N", then_stop=true)`. The stop goes out in the same call, as soon as the backend has N samples, and the call returns once the run has ended, with `stopped: true` and `run_ended` (its `samples` is the final count). While it waits to stop a run, `wait_for` looks every 0.1 s, so the file holds N rows or a few more: those read in that tenth of a second and the one in flight when the stop arrives. `get_run_summary(first_rows=N)` then summarises exactly N. Waiting and stopping in two calls leaves the agent's round trip between them; in a trial, a run asked for 20 readings wrote 57. A prompt or a timeout does not stop the run.

### Waiting for a person

A prompt that `requires_human` holds the run until a person answers it at the window. The agent tells the user what it asks, then calls `wait_for("run_ended")`. A prompt the agent has already been shown, by any tool's reply, does not end that wait; any other prompt ends it at once, so a prompt raised just after `start_run` is never waited through unheard. The wait returns with `fired`:

- `prompt`: the next prompt (the person answered and the run moved on, to the next van der Pauw wiring, say);
- `run_ended`: the run is over (all answered, or stopped);
- `timeout`: no answer yet; call again.

`prompt_at_start` names the prompt that was waited through and says whether it is `still_pending`. `wait_for("state:running")` returns as soon as the person has answered; `wait_for("prompt")` returns at once while any prompt is pending.

## What needs a person

The backend applies these to the agent's token whatever the MCP server does ([API → Token and roles](api.md#token-and-roles)):

- **Agent limits.** A run an agent starts must stay within the profile's [`agent_limits`](settings.md#agent-limits): 30 V by default, with current and power left to the instrument, and the connected model's own limits once the backend knows the model. Beyond them `start_run` is refused with each violation (the limit, the settings that give the value, the value, the limit's value); `check_settings` gives the same verdict first. Only the window can change the limits.
- **Prompts.** A run at or above the profile's touch-safety threshold asks for acknowledgement at the window, even on a profile that silenced the warning, and a van der Pauw run asks before each of its four wirings, with the output off, so a person must be at the bench for the whole of it (`describe_mode("vdp")` says so). Both are `requires_human`: `wait_for` and `get_status` report them with "a person must answer this at the ResistaMet window", and the run waits for that person, or until `prompt_timeout_s` (900 s by default) and then ends.
- **Protected settings.** The touch-safety keys, the agent limits, `allow_agents` and a VISA library path can be changed by the window only.
- **Provenance.** Every run an agent starts has `started_by: agent` in its data file header, set by the backend from the token, and `client.name: resistamet-mcp`. The desktop app marks such a run.

Stopping is never restricted: the agent can stop any run, and so can the person.

## Audit log

The MCP server writes one JSON line per tool call to `~/.resistamet/logs/mcp/<UTC date>.jsonl`:

```json
{"time": "2026-10-07T19:31:11.279+00:00", "client": {"name": "claude-code", "version": "2.1.0"},
 "tool": "start_run", "arguments": {"user": "alice", "mode": "resistance", "sample_name": "s1"},
 "outcome": "ok", "http_status": 202, "run_id": "run-1",
 "result": {"run_id": "run-1", "status": {"state": "running", "...": "..."}}}
```

`client` is what the MCP client said about itself when it connected. `http_status` is the highest status the backend answered during the call. A result or argument set longer than 4000 characters of JSON is cut and marked (`{"truncated": true, "chars": …, "head": …}`). Tokens are never written. Run ids restart with each backend, so read `run_id` together with the time.

The data file records the run; the audit log records what the agent asked for and was told. The agent's reasoning is in neither: it lives in the MCP client's transcript. For a paper's data statement keep all three. Claude Code keeps each session as a JSONL file under `~/.claude/projects/` and deletes old ones after a while, so copy the session's file next to the data, or export the conversation with `/export`. In Claude Desktop, export the conversation from the app.

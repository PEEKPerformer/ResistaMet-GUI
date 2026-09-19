# Backend API

The measurement backend runs as a separate process (the *sidecar*) and is driven over localhost HTTP plus one WebSocket. The [desktop app](desktop.md) is one client of it; a script is another. The same run procedures execute under the PySide6 window, so a file written through the API is the same file (see [Data outputs](outputs.md)).

Status: everything on this page was exercised against the in-package simulator for this write-up. A frozen Windows build of the backend was driven against a Keithley 2420 on 2026-09-18 (identify, a resistance run, stop during settling, shutdown mid-run with the output confirmed off). The API is unversioned (`2.0-dev`) and may still change; there is no `/api/v1` prefix.

## Starting the sidecar

```bash
pip install -e ".[api]"          # fastapi, uvicorn, websockets
python -m resistamet_gui.api --simulate --config config.json
```

The frozen build is the same program under the name `resistamet-api` (it ships inside the desktop app bundle). A source install has no `resistamet-api` command; use `python -m resistamet_gui.api`.

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address. Do not expose it: the token is the only protection. |
| `--port` | `0` | Port; `0` lets the OS choose. The handshake reports the real one. |
| `--config PATH` | `config.json` in the working directory | The profile file. Relative `data_directory` values in profiles resolve against the sidecar's working directory, so a parent process should pass an absolute `--config` and set the working directory deliberately. |
| `--token` | generated (`secrets.token_urlsafe(32)`) | Bearer token. |
| `--simulate` | off | Run against the in-package simulator (a 2420 at `GPIB0::24::INSTR`). |
| `--sim-resistance OHMS` | `100` | Simulated DUT. |
| `--no-watchdog` | off | Do not exit when stdin closes. Needed when you start the sidecar from an interactive shell or in the background. |
| `--check-visa [quiet\|bus]` | | Print one JSON line describing this machine's VISA situation and exit without serving. See [GPIB → Diagnosing](gpib.md#diagnosing-with-check-visa). |
| `--visa-library`, `--gpib-interface` | the machine's configured values | Overrides for `--check-visa` only. |

### Handshake

The first and only line the sidecar ever writes to stdout:

```json
{"url": "http://127.0.0.1:53124", "token": "kq3…", "pid": 41234}
```

Read exactly one line, then talk to `url` with `token`. Logs go to stderr. The listening socket is bound before the line is printed, so the port is valid as soon as you have it.

### Token and roles

Every HTTP route except `GET /health` needs `Authorization: Bearer <token>`. A wrong token is 401; a missing header is 401 or 403 depending on the FastAPI version. The WebSocket takes the token as a `token` query parameter, because a browser cannot set headers on a WebSocket; a wrong one is refused during the handshake (the client sees HTTP 403).

The token keeps other local processes from driving the instrument. It is not an authentication system.

A token carries a role. At this commit there is one token and its role is `ui`. The role matters in two places: a prompt marked `requires_human` may only be answered by the `ui` role, and only the `ui` role may change the touch-safety settings of a profile (403 otherwise). No other role can be minted yet.

Cross-origin requests are accepted only from the desktop shell's origins (`tauri://localhost`, `http(s)://tauri.localhost`) and the UI dev server (`http://localhost:1420`, `http://127.0.0.1:1420`). That restricts browsers, not scripts.

## Session state

One sidecar drives one instrument and one run at a time.

| `state` | Meaning |
|---|---|
| `idle` | Nothing running. The only state that accepts `start`, `identify` and a resource scan. |
| `identifying` | `POST /instruments/identify` holds the bus. |
| `running` | A run's acquisition thread is alive. |
| `paused` | Continuous run paused; the output stays on. |
| `awaiting_prompt` | The run is blocked on a [prompt](#prompts). |
| `stopping` | A stop was accepted; the run is turning the output off and finalizing its file. |

`SessionStatus`, returned by `GET /session` and by every session command:

```json
{
  "state": "paused",
  "run_id": "run-1",
  "mode": "resistance",
  "path": "measurement_data/alice/1789858920_lock-demo_R_1.00mA.csv",
  "last_seq": 77,
  "pending_prompt": null,
  "instrument": {"address": "GPIB0::24::INSTR", "idn": "KEITHLEY …", "model": "2420",
                 "max_source_v": 60.0, "max_source_i": 3.05, "max_power_w": 22.0}
}
```

`run_id`, `mode` and `path` describe the current run, or the last one after it ends; all are `null` before the first. `instrument` is the instrument the last run connected to or the last identify found (`model` and the limits are `null` when `*IDN?` names a model the limits table does not know). `pending_prompt` has the fields of the `prompt` event. Every key is always present.

## Routes

Non-finite floats (an unmeasured temperature, an uncertainty that could not be computed) are `null` in every reply and event. Validation failures of a request body are FastAPI's standard 422.

### Session

| Method and path | Request | Reply | Errors |
|---|---|---|---|
| `GET /health` | | `{"status": "ok"}`. No token. Liveness only. | |
| `GET /session` | | `SessionStatus` | |
| `POST /session/start` | `RunRequest` (below) | **202** `{"run_id": "run-3"}` | 409 session not idle; 409 [instrument held by another process](#the-instrument-lock); 422 request malformed (FastAPI's error list: an unknown field, an unknown mode, a spot on a mode other than four-point) or settings rejected (`detail` is a string of `key: message` pairs) |
| `POST /session/stop` | | `SessionStatus` | Never fails; a no-op when idle |
| `POST /session/abort` | | `SessionStatus` | Never fails |
| `POST /session/pause`, `POST /session/resume` | | `SessionStatus` | 409 no run in progress |
| `POST /session/mark` | `{"label": "MARK"}` (label optional) | `SessionStatus` | 409 no run in progress |
| `POST /session/prompt` | `{"prompt_id", "choice", "fields": {}}` | `SessionStatus` | 409 no prompt pending; 409 `prompt_id` stale, already answered, or `choice` not among the prompt's `options` (the prompt stays pending); 403 prompt needs a human and the role is not `ui` |
| `GET /session/events` | query `since_seq` (0), `run_id` (all runs), `limit` (500) | `{"events": [Event…], "gap": bool, "last_seq": int}` | |
| `POST /session/shutdown` | | `{"status": "stopping"}` | |

`RunRequest` (the model exported as `contracts/settings.schema.json`; unknown fields are refused, so a misspelt one is a 422 that names it):

| Field | Type | Notes |
|---|---|---|
| `mode` | string | `resistance`, `source_v`, `source_i`, `four_point`, `sweep`, `vdp` |
| `sample_name`, `username` | string, not empty | The profile of `username` supplies every setting not overridden. |
| `overrides` | object | Flat measurement keys, e.g. `{"res_test_current": 1e-3}`. Allowed keys per mode come from `GET /schema/settings`. Refused with 422: unknown keys, the profile-owned `settling_time` and `gpib_address`, and the touch-safety keys `safety_voltage_warn_v` and `safety_voltage_warn_silenced` (a run request cannot arrange never to be asked). Values are type-checked strictly: `"1e-3"` is not a number and `"false"` is not a boolean. |
| `prompt_timeout_s` | number > 0, default 900 | How long a prompt may wait before the run is abandoned. |
| `spot` | object or null | Four-point only (422 for other modes): `{"map_id", "index", "label", "x_mm"?, "y_mm"?, "angle_deg"?}`. See [Concepts → Spots and maps](concepts.md#spots-and-maps). |
| `client` | object or null | `{"name", "version"}`, each 1–64 characters from letters, digits, space and `. _ + -`. Written to the file header as `client.*`. |

`start` returns as soon as the run thread exists. Whether the run then connects, configures and measures is told through events; a run that fails to connect still ends with `run_ended`.

`stop` and `abort` both end the run through the normal shutdown (output off, file finalized) and both release a run parked on a prompt. They differ in the reason reported: `user_stop` (which counts as `ok`) versus `aborted`. `pause` has no effect on a van der Pauw run. A mark is attached to the next sample row; several marks before one sample are joined with `; `.

### Users, profiles, settings

| Method and path | Request | Reply | Errors |
|---|---|---|---|
| `GET /users` | | `{"users": [...], "last_user": ...}` | |
| `POST /users` | `{"username"}` (1–64 chars) | **201** same shape. Idempotent; selects the user. | 422 empty name |
| `GET /profiles/{username}` | | `{"measurement": {...}, "display": {...}, "file": {...}, "output": {...}}` with this PC's [machine-local](settings.md#machine-local-settings) values filled in | |
| `PATCH /profiles/{username}` | any of the four sections, each with only the keys to change | The updated profile. Keys not sent keep their stored values. | 404 unknown user; 422 no section given, or the result would not be valid (`detail.issues` lists `section`, `key`, `message`; an old out-of-range value you are not touching does not block the edit); 409 the patch has `gpib_address`, `visa_library` or `gpib_interface` and a run is active; 403 a role other than `ui` changes a touch-safety key |
| `GET /schema/settings` | | `{"modes": {mode: {"model", "fields": [...], "override_keys": [...]}}}` | |
| `POST /settings/resolve` | `{"mode", "username", "overrides": {}, "strict": true}` | `{"settings", "derived", "ok", "issues": [{"key","message","severity"}], "hazard"}` | 422 unknown mode |

`/settings/resolve` answers "what would this run use, and what is wrong with it" without touching the instrument. `derived` has `max_rate_hz`, plus `sweep_points` for a sweep and `worst_case_power_w` for four-point. `hazard` is `{"hazardous", "voltage_v", "threshold_v", "reason"}`, the touch-safety check on the resolved values. `ok` is false when any issue has severity `error`; `start` refuses exactly those requests. JSON Schemas of the settings, events, session status and maps are in the repository under `contracts/`.

### Instruments

| Method and path | Request | Reply | Errors |
|---|---|---|---|
| `GET /instruments/resources` | query `visa_library`, `gpib_interface` (both optional: default to this PC's settings) | `{"resources": [...], "backend": {"requested","kind","library","version"}, "gpib_interface": name or null}` | 409 session not idle (a scan puts traffic on the bus); 503 VISA unavailable or the GPIB interface did not open |
| `POST /instruments/identify` | `{"address", "visa_library"?, "gpib_interface"?}` | `{"address","idn","model","max_source_v","max_source_i","max_power_w"}` | 409 session not idle; 503 anything that went wrong talking to it, including the instrument lock being held |

Passing `visa_library` or `gpib_interface` tries a value before it is saved. `backend.kind` is `ivi` (vendor library), `py` (pyvisa-py) or `unknown` (the simulator).

### Results and maps

| Method and path | Request | Reply | Errors |
|---|---|---|---|
| `GET /results` | query `user`?, `limit` (500, max 5000) | `{"root", "files": [{"path","name","user","size","modified"}]}`, newest first. `path` is relative to the data directory with forward slashes. Lists `.csv`, `.csv.gz`, `.h5`, `.json`; map summaries are left out. | |
| `GET /results/file` | query `path` | The file's text (`text/plain`) | 400 path outside the data directory; 404; 415 not a `.csv`; 413 over 32 MB |
| `GET /results/directory` | | `{"root", "exists", "separator"}` | |
| `GET /maps` | query `user` | `{"user", "maps": [map_id…]}` | |
| `GET /maps/{map_id}` | query `user` | A `SpotMap`, the structure of [`<map_id>_map.json`](outputs.md#map-summary), assembled from the run files at the time of the call | 404 no run of that user names the map; 422 malformed id |

`/results*` use the `data_directory` of the global settings in the config file; `/maps*` use the `data_directory` of the named user's profile, which is where that user's runs are written. With default settings they are the same place.

## Events

A run reports everything as events: the same objects reach an in-process sink, the WebSocket and the polling route.

```json
{"v": 1, "type": "sample", "run_id": "run-1", "seq": 14, "t": 1789858920.41,
 "payload": {"t_unix": 1789858920.40, "elapsed_s": 0.422, "compliance": "OK", "event_marker": "",
             "values": {"voltage": 0.1, "current": 0.001, "resistance": 100.0, "resistance_unc": 0.06},
             "derived": null, "delta": null}}
```

| Field | Meaning |
|---|---|
| `v` | Event schema version, `1`. |
| `type` | One of the types below. Ignore types you do not know. |
| `run_id` | `run-1`, `run-2`, … counted per sidecar process. Not unique across restarts. |
| `seq` | Per-run counter starting at 1. With `run_id` it is the resume cursor. |
| `t` | Wall-clock emit time, Unix seconds. |
| `payload` | One model per type. |

### WebSocket

```
ws://127.0.0.1:<port>/session/events/ws?token=<token>[&run_id=run-3&since_seq=120]
```

- Without `since_seq` (or with 0) you receive live events from the moment you connect, for every run. Connect before you call `start`.
- With `since_seq=N` the server first replays the retained events with `seq > N` (of `run_id`, if given), then goes live. It subscribes before replaying, so an event can arrive twice: drop any `seq` you have already seen for that run. `run_id` filters the replay only, not the live stream.
- If the history no longer reaches back to `N`, the first message is `{"type": "gap", "detail": "…", "since_seq": N}`, which is not an event envelope. Treat the stream as incomplete and re-read the run's file.
- The server ignores anything the client sends.

**History.** The sidecar keeps the last 10 000 forwarded events in memory, across runs. It is lost when the sidecar exits. At 10 Hz, with two progress logs a second, that is roughly a quarter of an hour of a run; the file is the record beyond that.

**What is forwarded.** Two filters apply before an event reaches history or any client: `compliance` events are sent only when the compliance state changes (each `sample` still carries its own `compliance`), and `log` events with code `progress` are limited to one per 0.5 s.

**Drop policy.** The acquisition thread never waits for a client. Each client has a queue of 2000 events. Above 1900 queued, `sample` and `log` events for that client are dropped; they are recoverable from the file. Everything else (lifecycle, errors, prompts, `run_ended`) uses the reserved room, and a client that cannot take even one of those is disconnected rather than sent a stream with a hole in it. Reconnect with your cursor.

**Polling.** `GET /session/events?run_id=run-3&since_seq=120` returns the same events from the same history, for clients that cannot hold a socket. Continue from the reply's `last_seq`.

### Order of a run

A simulated resistance run that was paused, resumed and stopped produced, in order (`log:<code>` are `log` events; repeats collapsed):

```
run_started, log:connecting, log:connected, instrument_connected, log:model_detected,
line_frequency, log:configuring, log:filter, file_opened, log:file_opened, log:starting,
log:settling, sample / log:progress …, log:paused, paused, log:resumed, stopping,
log:stopping, log:output_off, file_finalized, log:completed, acquisition_finished,
log:cleanup, run_ended
```

`run_ended` is always the last event of a run, whatever ended it, including runs refused before the instrument was opened. If a run's thread dies without sending one, the session sends an `error` and a `run_ended` with reason `worker_error` in its place and releases the instrument.

### Event types

| Type | Payload | When |
|---|---|---|
| `run_started` | `mode`, `sample_name`, `username`, `settings` (the full resolved settings the run uses), `started_at` | First event of a run. |
| `log` | `level` (`info`, `warning`, `error`), `code`, `message` | Progress in words. `message` is the text a UI shows; act on `code`. |
| `error` | `code`, `source` (`smu`, `aux`, `file`, `run`), `message`, `fatal` | Something failed. |
| `instrument_connected` | `address`, `idn`, `model`, `max_source_v`, `max_source_i`, `max_power_w` | `*IDN?` answered. |
| `line_frequency` | `hz`, `assumed` | Continuous modes and sweep; not van der Pauw. |
| `aux_connected` | `driver`, `address`, `channels` | An auxiliary sensor opened. |
| `file_opened` | `path`, `columns`, `units` | The data file exists and its columns are fixed. |
| `sample` | `t_unix`, `elapsed_s`, `compliance` (`OK`, `V_COMP`, `I_COMP`), `event_marker`, `values`, `derived`, `delta` | One row. `values` has `voltage`, `current` and per mode `resistance`, `resistance_unc`, `voltage_unc`, `current_unc`, plus auxiliary channels. `derived` (four-point): `ratio`, `rs`, `rho`, `sigma`, `v_unc`, `i_unc`, `method` (`f84` or `legacy`). `delta` (four-point delta mode): `v_plus`, `v_minus`, `r_f`, `r_r`. |
| `compliance` | `kind` (`Voltage`, `Current`), `stop_on_compliance` | The source is in compliance (transitions only on the wire). |
| `overpower_trip` | `measured_w`, `stop_w` | Four-point probe-safety hard stop. |
| `sweep_segment` | `direction` (`forward`, `reverse`), `voltages`, `currents`, `compliance` (`OK` / `COMP` per point) | One sweep direction, in bulk. An up-down sweep sends two. |
| `geometry_warning` | `refused`, `reason` (`off_sample`, `near_edge`), `message`, `spot`, `edge_clearance_s`, `edge_warn_pct`, `factor_here`, `factor_centre`, `relative_error`, `factor_rows`, `relative_error_rows`, `compared_with` (`rows`, `centre`) | A four-point spot's position is a problem, said before the instrument is opened. Fields as in [`spot.*`](outputs.md#spot-four-point-runs-that-carry-a-spot). |
| `spot_complete` | `spot` (or null), `path`, `stats` (`n`, `n_excluded`, `end_reason`, `rs`, `rho`, `sigma`) | A four-point file was finalized; `stats` is its [`spot_stats`](outputs.md#spot_stats-every-four-point-run) footer. |
| `vdp_geometry_complete` | `index`, `name`, `group`, `label_pos`, `v_pos`, `label_neg`, `v_neg`, `current_a` | One F76 geometry measured. |
| `vdp_result` | `rho_a`, `rho_b`, `rho_avg`, `sheet_resistance`, `q_a`, `q_b`, `f_a`, `f_b`, `homogeneous`, `asymmetry_pct`, `voltages`, `current_a`, `thickness_cm`, `sheet_resistance_uncertainty`, `rho_avg_uncertainty` | The finished van der Pauw result, as in the file footer. |
| `prompt` | `prompt_id`, `kind`, `options`, `requires_human`, `detail` | The run is blocked. |
| `prompt_resolved` | `prompt_id`, `choice` (null when a stop or the timeout ended the wait), `answered_by` (always null at this commit) | |
| `paused`, `resumed`, `stopping` | `reason` | As observed by the acquisition thread. |
| `file_finalized` | `path`, `end_metadata` | File closed with its footer. |
| `acquisition_finished` | `mode` | The loop ended; cleanup follows. |
| `run_ended` | `reason`, `ok`, `samples`, `duration_s`, `path` | Always last. |

`run_ended.reason` values: `completed`, `target_samples`, `duration`, `user_stop` (these four are `ok: true`); `aborted`, `cancelled`, `prompt_timeout`, `spot_refused`, `instrument_busy`, `compliance_stop`, `overpower`, `power_envelope`, `connect_failed`, `configure_failed`, `output_on_failed`, `aux_connect_failed`, `file_create_failed`, `read_error`, `write_error`, `worker_error`.

## Prompts

A prompt is a decision the run cannot make. The run emits `prompt`, the session state becomes `awaiting_prompt`, and `GET /session` shows it under `pending_prompt`, so a client that connects late still sees the question. Answer with `POST /session/prompt`, quoting the prompt's `prompt_id` (for example `run-2:safety_voltage_ack-1`; the run id is part of it, so an answer left over from an earlier run cannot be taken by the next) and one of its `options`. The first valid answer wins. Anything else is a 409 and the prompt stays pending.

| `kind` | Raised | `options` | `detail` |
|---|---|---|---|
| `safety_voltage_ack` | After `run_started`, before the instrument is opened, when the run's [gating voltage](concepts.md#touch-safety-warning) reaches the profile's threshold and the profile has not silenced the warning | `acknowledge`, `cancel` | `voltage_v`, `threshold_v`, `reason`, `message` |
| `vdp_geometry` | Before each of the four van der Pauw geometries, with the output off | `proceed`, `abort` | `index`, `name`, `group`, the four contact numbers `source_high`, `source_low`, `sense_high`, `sense_low`, `label_pos`, `label_neg` |

A third kind, `cable_null_shorted`, is declared in the contract; no run raises it at this commit.

Both kinds are `requires_human: true`: they assert something only a person at the bench can know (the leads were moved, the voltage is understood). Only the `ui` role may answer them. Software that holds the `ui` token can answer them, and then it is making that assertion.

`cancel` ends the run with reason `cancelled`, no instrument opened and no file. An unanswered prompt ends the run after `prompt_timeout_s` with reason `prompt_timeout`. A stop or abort releases the wait. Answering `acknowledge` with `fields: {"silence_for_profile": true}` is logged but not saved to the profile at this commit; to silence the warning, `PATCH` `safety_voltage_warn_silenced` yourself.

## The instrument lock

Two processes interleaving SCPI on one GPIB address produce readings that look plausible and are not. Every ResistaMet process therefore takes an operating-system file lock per instrument address for the length of a run or an identify: `~/.resistamet/locks/<address with non-alphanumerics as _>.lock`, for example `GPIB0__24__INSTR.lock`. The PySide6 window, the sidecar and a script using `MeasurementSession` all use the same directory, so they exclude each other.

- `POST /session/start` takes the lock before it answers. If another process holds it, the call waits up to 3 s (a run releases its lock at the very end of cleanup, so a Start right after a Stop can legitimately find it held), then answers **409** with `"<address> is in use by another ResistaMet process"`.
- The lock files are empty and are never deleted. The operating system releases a lock when its holder exits, however it exits, so there is no stale lock to clear. Deleting the files is harmless but pointless.
- The lock is per user account and per address string. It does not see other software (KickStart, LabVIEW), another user account on the same PC, or the same instrument reached under a different resource name.

## Shutdown and the watchdog

Three things stop the sidecar, all through the same ordered shutdown:

1. `POST /session/shutdown`: asks the run to stop and the server to exit; replies `{"status": "stopping"}` immediately.
2. **stdin closes.** Unless `--no-watchdog` was given, the sidecar reads its stdin and treats end-of-file as "the parent is gone". This is how a parent that crashes still gets the instrument released, and on Windows it is the route that covers a closed console window. It is also why a sidecar started with `&` or from a service manager exits at once without `--no-watchdog`.
3. SIGTERM, SIGINT (Ctrl-C) or, on Windows, Ctrl-Break.

In each case the run is asked to stop and the process waits up to 35 s for it: output off, file finalized with its footer, instrument closed, lock released. The run reports `user_stop`. Open connections get 3 s to close. Then the process exits with status 0. The desktop shell waits 40 s after closing stdin before it kills the process. For this page, SIGTERM in the middle of a simulated run left a file with its footer and all its rows.

If the run has not ended when the 35 s are over (a VISA call that does not return), the sidecar logs an error saying that the output may still be on and exits anyway. The same holds, without the log line, for anything that kills the process outright (`kill -9`, power loss): the instrument keeps its last state, which may be output on, and the file has no footer and may lack the rows since the last flush. Check the front panel.

## Example: ten samples from the simulator

Needs `pip install -e ".[api]" httpx`. Run it in an empty directory: it creates `config.json` and `measurement_data/` there.

```python
"""Start a simulated resistance run, read ten samples, stop."""
import asyncio
import json
import subprocess
import sys

import httpx
import websockets


async def main():
    # Launch the sidecar and read its one-line handshake from stdout.
    sidecar = subprocess.Popen(
        [sys.executable, "-m", "resistamet_gui.api", "--simulate",
         "--config", "config.json"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    handshake = json.loads(sidecar.stdout.readline())
    url, token = handshake["url"], handshake["token"]

    auth = {"Authorization": f"Bearer {token}"}
    ws_url = url.replace("http://", "ws://") + f"/session/events/ws?token={token}"

    async with httpx.AsyncClient(base_url=url, headers=auth) as api:
        (await api.post("/users", json={"username": "alice"})).raise_for_status()

        # Subscribe before starting so no event is missed.
        async with websockets.connect(ws_url) as events:
            reply = await api.post("/session/start", json={
                "mode": "resistance",
                "sample_name": "api-demo",
                "username": "alice",
                "overrides": {"res_test_current": 1e-3, "sampling_rate": 10.0},
                "client": {"name": "docs-example", "version": "1.0"},
            })
            reply.raise_for_status()          # 202; 409 = busy, 422 = bad settings
            run_id = reply.json()["run_id"]

            samples = 0
            async for text in events:
                event = json.loads(text)
                if event.get("run_id") != run_id:
                    continue
                if event["type"] == "sample":
                    samples += 1
                    if samples <= 10:
                        values = event["payload"]["values"]
                        print(f"{event['payload']['elapsed_s']:7.3f} s  "
                              f"{values['resistance']:.6g} ohm")
                    if samples == 10:
                        await api.post("/session/stop")
                elif event["type"] == "error":
                    print("error:", event["payload"]["message"])
                elif event["type"] == "run_ended":
                    print("ended:", event["payload"]["reason"],
                          event["payload"]["path"])
                    break

        await api.post("/session/shutdown")

    sidecar.stdin.close()                     # the watchdog also exits on this
    sidecar.wait(timeout=60)


asyncio.run(main())
```

Output against the simulator (100 Ω DUT, no noise):

```
  0.211 s  100 ohm
  0.314 s  100 ohm
  …
  1.146 s  100 ohm
ended: user_stop measurement_data/alice/1789858503_api-demo_R_1.00mA.csv
```

For real hardware, drop `--simulate`, set the address once (`PATCH /profiles/alice` with `{"measurement": {"gpib_address": "GPIB0::24::INSTR"}}`), and be ready to answer a `safety_voltage_ack` prompt if the compliance is 30 V or more.

## Without HTTP

The API is a thin layer over `resistamet_gui.session.MeasurementSession`, which has no Qt and no web dependency:

```python
from resistamet_gui.config import ConfigManager
from resistamet_gui.session.manager import MeasurementSession

events = []
session = MeasurementSession(events.append)          # any callable taking an Event
profile = ConfigManager(config_file="config.json").get_user_settings("alice")
run_id = session.start(profile, "resistance", "sample-1", "alice",
                       overrides={"res_test_current": 1e-3})
...
session.close()                                       # stop, wait up to 35 s
```

`start` raises `SessionBusy`, `InstrumentBusy` or `ValueError` where the HTTP route answers 409, 409 and 422. Call `resistamet_gui.simulator.enable_simulation()` first to run without hardware.

# Tauri Backend Split, Step 1: Headless MeasurementSession + Typed Settings: Design

**Status:** Draft (rev 3 — maintainer decisions recorded, see §11)
**Target:** ResistaMet-GUI v1.13 → v2.0 (the Tauri UI and MCP layer build on this)
**Date:** 2026-09-17

> **Decision already made (not revisited here):** a Tauri front end (React + TypeScript, uPlot) will
> replace the PySide6 UI. The Python backend stays: `instrument.py`, `calculations*.py`, `accuracy.py`,
> `data_export.py`, `sensors.py`, `_simulator.py` and `config.py`. It ships as a PyInstaller sidecar that
> exposes a localhost FastAPI service (HTTP for commands, WebSocket for events). The v2.0 MCP layer sits
> on that same API. The PySide6 UI keeps working until the Tauri UI matches it.
>
> **This document covers step 1 only:** pull a Qt-free `MeasurementSession` and a typed settings schema
> out of `workers.py` and `ui/main_window.py`.
>
> **What the plan is judged on first:** whether a human can review it. Every PR in §9 is small,
> single-purpose, and exactly one of:
> (a) a **pure move** you can check with `git diff --color-moved` (import lines are the only other change);
> (b) a **mechanical rewrite** you can check with `git diff --word-diff` (one substitution pattern, stated);
> (c) a **behavior-preserving refactor** proven by existing tests passing *unchanged*, plus any new
>     golden/parity test landed *before* it;
> (d) an **additive** change nothing existing calls;
> (e) a **named behavior change** with its own justification and test.
> Any edit to an existing test is its own called-out step.

Citations use `W` = `resistamet_gui/workers.py`, `MW` = `resistamet_gui/ui/main_window.py`. Other files
are named in full.

---

## 1. Goal & non-goals

**Goal.** Measurement runs can be started, controlled, observed and finished with **no Qt imported**.
That covers:
- the run procedures (resistance, source-V, source-I, 4PP, sweep, vdP);
- run control (stop, abort, pause, resume, mark event, answer a prompt);
- one typed event stream;
- a typed, validated settings contract that replaces widget-scraping as the only way to build run
  settings.

The PySide6 UI runs on the new code through a thin adapter and looks the same to its users.

**Non-goals for step 1.**
- **No React/Tauri code, and no PyInstaller sidecar spec.** Step 1 only adds a
  `python -m resistamet_gui.api` entry point to prove the session shape (§7, PR-51c).
- **No MCP server.** The API shape is proven for push (WebSocket) and pull (HTTP) clients (§7.4); MCP
  authorization is settled in principle (D4: only a UI credential may answer `requires_human` prompts);
  the second credential itself arrives with the MCP server in v2.0.
- **No changes to the `config.json` format** beyond one additive top-level `migrations: List[str]`
  (PR-02's one-time `output` reset, D6). Profile semantics and machine-local GPIB handling
  (`config.py:18`, `:46-78`, `:161-182`) stay as they are. PR-44 changes only *how* the file is written
  (lock + atomic replace).
- **No changes to CSV or HDF5 content.** Any change there would be a separate, named behavior-change PR.
- **Results Viewer, 4PP spot model, Export Summary, profile-file I/O and cable null are not moved yet.**
  They are classified in §5 and scheduled for step 2.
- **The 4PP GUI/CSV divergence (§2.4) is not fixed inside a refactor.** The new `sample` event carries
  the derived row (PR-32c); **PR-33 then makes the PySide6 4PP panel read those values** (D7), so the
  divergence dies before parity and the Tauri UI never inherits it.

**How this enables later steps.**
- **Step 2 (Tauri UI):** consumes the §3 events and §4 schema through generated TypeScript types (§10.2).
- **Step 3 (sidecar packaging):** wraps the `api` package from PR-50/51.
- **v2.0 MCP:** MCP tools map one-to-one onto the session commands. Prompts (§3.5) keep an LLM client
  from silently skipping an operator decision.

---

## 2. Current coupling: what ties logic to Qt today

### 2.1 Workers: a small Qt surface on a large procedure
- **The only Qt import in the workers is `from PySide6.QtCore import QThread, Signal` (W:12).**
  - `MeasurementWorker(QThread)` is at W:39, with signals at W:41-51.
  - `VdpMeasurementWorker(QThread)` is at W:1331, with signals at W:1351-1357.
- **Everything the workers import is already Qt-free:** `accuracy`, `data_export`, `instrument`,
  `sensors`, `system_utils` (W:16-36).
- **Control state already uses plain threading primitives:**
  - `_state_lock = threading.Lock()` guarding `_running`, `_paused`, `_event_marker` (W:63-133, W:1366);
  - the vdP operator wait is a `threading.Event` (W:1368, W:1563);
  - every sleep is `time.sleep`.
- **There are 81 `.emit(` calls** (`grep -c '\.emit(' resistamet_gui/workers.py`). The main ones:
  - `data_point` at W:1048;
  - `sweep_complete` at W:590-593;
  - `overpower_hit` at W:868;
  - `compliance_hit` at W:844 and W:1582;
  - `geometry_ready` / `geometry_complete` / `vdp_complete` at W:1547, W:1601, W:1667;
  - `instrument_identified` at W:167 and W:1446;
  - the remainder are `status_update` and `error_occurred`.
- **`run()` is one 985-line method (W:135-1120).** It covers connect (W:152-200), per-mode configure
  (W:202-445), aux open and exporter creation (W:450-515), sweep (W:521-600), the continuous loop
  (W:638-1091), and shutdown/finalize (W:1093-1120). This size, not Qt, is the real extraction cost.
- **The configure block writes run state that the loop reads.** `self._cable_null` (W:235), seven
  `self._fpp_*` (W:334-350) and four `self._sweep_*` (W:403-415) are set there; there are 17 reads of
  `_fpp_*/_sweep_*/_cable_null/_last_delta` after W:445. The configure block also emits (W:203, 357,
  367, 438, 443) and returns early from `run()` (W:365, W:444). §9 PR-22 is split because of this.
- **`running`/`paused` are assigned 15 times**, including self-stops from inside the run: sweep end
  (W:599), compliance stop (W:850), overpower (W:880), target samples / duration (W:1045, W:1055), loop
  end (W:1091), and `finally` (W:1120). `run()` sets `running = True` on entry (W:136), so a
  `stop_measurement()` that lands before the thread's first line is overwritten.

### 2.2 QThread behaviour the UI relies on
- `worker.start()` (MW:1339, MW:2221) and `worker.finished.connect(...)` (MW:1338, MW:2220).
  - `finished` is the only guaranteed end-of-run hook.
  - `measurement_complete` is skipped on the early returns at W:200, 365, 444, 476, 515 and 612, and on
    the unexpected-exception path (W:1116-1117, which skips W:1114).
  - `vdp_complete` fires only on success (W:1413-1426).
- `worker.wait(2000)` in `closeEvent` (MW:3525).
- `worker.filename` is read without a lock after the thread finishes (MW:2867-2869).
- `pause_measurement` / `resume_measurement` / `stop_measurement` emit `status_update` **on the calling
  (GUI) thread**; pause/resume only when `running` is true (W:1184-1196, W:1392-1396).
- `tests/test_workers.py:176-204` drives workers through `start`, `isRunning`, `wait` and
  `processEvents`.

### 2.3 Settings built by reading live widgets
- **`gather_settings_for_mode` (MW:1937-2050)** shallow-copies `measurement`, `display` and `file` from
  the profile (MW:1940-1944), then overwrites the mode's keys from widgets.
  - NPLC and sampling rate come from the tab if present (MW:2016-2025).
  - `auto_zero` comes from the tab, falling back to `'once'` (MW:2030-2033).
  - "Run until stopped" maps to duration `0.0` (MW:2035-2038).
  - `settling_time` and `gpib_address` come from the profile (MW:2039-2040).
  - `MODE_TIMING_OVERRIDES` is applied last (MW:2045-2049; `constants.py:168-171`).
- **`TestGatherSettings` checks key presence only** (`tests/test_gui_smoke.py:296-357`), except
  `test_source_v_continuous_duration` (:316-321). It cannot prove value preservation; PR-12 adds goldens.
- **The `output` section is never copied**, so `make_exporter` receives `None` and falls back to CSV
  with no compression (W:503; `data_export.py:901-915`).
- **The voltage-safety check runs before gather and reads the stored profile, not the tab values**
  (MW:2175 vs MW:2181; `_confirm_voltage_safety` at MW:2085-2091). The status-bar hazard tag also reads
  the stored profile (`_running_status_message`, MW:2059-2072, called at MW:2202 and MW:1325).
- **"Don't show again" calls `self.config_manager.set_user_settings`** (MW:2106). That method does not
  exist; only `update_user_settings` does (`config.py:161`). The resulting `AttributeError` is swallowed
  (MW:2107-2111).
- **`res_cable_null` lives only in memory** (MW:3438, MW:3449). `update_user_settings` stores whole
  sections (`config.py:173-181`), so any fix that saves `self.user_settings['measurement']` would also
  persist the null, while the tab label starts as "Cable null: OFF" on the next launch (MW:393).
- **Four layers disagree on fallback defaults** for `filter_enabled`, `filter_count`, `res_offset_comp`,
  `auto_zero` and `vdp_readings_per_polarity`: `constants.py:40-54, :100`; `dialogs.py:377-380`;
  W:189, W:232, W:432-434, W:1540; `timing.py:103-107`.
- **The `fpp_thickness_cm` → µm fallback at MW:1852-1854 is dead code**: `get_user_settings` starts from
  a deep copy of `DEFAULT_SETTINGS` (`config.py:144`), which always has `fpp_thickness_um`
  (`constants.py:61`). The live migration is in the profile-file loader (MW:1773-1776).

### 2.4 Domain logic duplicated or living in the UI
- **4PP Rs/ρ/σ are recomputed in `update_data` with the legacy K·α path and widget values**
  (MW:2458-2482). The worker writes F84 values to the CSV when F84 inputs are set (W:908-999), but it
  only emits the raw `data_dict` (W:1048).
  - The 4PP table's "elapsed" is `timestamp - ts[0]`, where `ts` is already elapsed time
    (MW:2483-2484; `buffers.py:84`). The result is an epoch timestamp.
- **vdP combined uncertainty is recomputed in the UI** (MW:1430-1436) even though the worker already
  puts it in the result dict (W:1609-1626, W:1664-1665).
- **Cable null runs a hand-written SCPI sequence on the GUI thread, with blocking sleeps**
  (MW:3397-3431), guarded by a `measurement_running` check (MW:3399-3401).
- **"Test Connection" calls pyvisa directly on the GUI thread with no running-measurement check**
  (MW:3472-3513).
- **The aux-sensor serial port is handed between the idle preview and the run by the UI**
  (MW:2203-2207, MW:2896).
- **`data_dict` is not all floats.** With aux logging on it carries `aux_fault` as a string
  (`sensors.py:457-458`; W:826-831), and the UI tests `str(value.get('aux_fault','0')) == '0'` (MW:2501).
- **The UI parses worker text by substring:** status colouring (MW:3191-3195); `_is_address_error`,
  which excludes messages containing "auxiliary sensor" (MW:2820-2841).
- **Pure helpers live in a Qt module.** `parse_engineering`, `format_engineering` and
  `precision_for_nplc` (`ui/widgets.py:45`, `:82`, `:212`) sit in a file that imports PySide6 at load
  time (`ui/widgets.py:13-15`).

---

## 3. Target architecture

### 3.1 Three layers, each a small piece

```
             ┌────────────────── adapters (thin) ──────────────────┐
             │ workers.py  QThread adapters → legacy Qt Signals     │  PySide6 UI (transition)
             │ session/manager.py  MeasurementSession (own thread)  │  api/ (FastAPI), later MCP
             └──────────────┬───────────────────────────────────────┘
                            │ Run(mode, sample_name, username, settings, control)
                            │ .execute(emitter)   ← synchronous, runs on caller's thread
             ┌──────────────┴──────────── procedures ──────────────┐
             │ session/continuous_run.py  session/vdp_run.py        │
             │ session/configure.py  samples.py  delta.py           │
             │ session/run_files.py                                 │
             └──────────────┬───────────────────────────────────────┘
                            │ calls
             instrument.py  calculations*.py  accuracy.py  data_export.py  sensors.py  (unchanged)
```

**One signature, used everywhere in this doc:** a run procedure is constructed with its inputs and its
`RunControl`, and executed with an `EventEmitter`:
`ContinuousRun(mode, sample_name, username, settings, control).execute(emitter)`. The constructor form
lets `filename` stay a property on the run (§6).

**A run procedure** (`ContinuousRun`, `VdpRun`) is a plain class. It does not own a thread. It is the
body of today's `run()`, extracted and moved in small steps rather than rewritten (§9, Phase 2).

**`MeasurementSession`** is the headless owner, used by the API. It holds:
- a plain `threading.Thread` for the active run;
- the state (`idle`, `identifying`, `running`, `awaiting_prompt`, `paused`, `stopping`);
- the one-run-at-a-time rule (MW:2166-2168 today), which also serializes `identify` (§7.1);
- the pending prompt reference for `status()`.

It is deliberately small, about 250 lines. It is a coordinator, not a new home for the logic now in
`main_window.py`.

**The PySide6 adapters** (`MeasurementWorker`, `VdpMeasurementWorker`) remain `QThread`s with the same
constructor, Signals and methods. Their `run()` is `self._run.execute(emitter)`, with a sink that
re-emits Signals. §6 explains why the adapter wraps the *procedure* rather than the session.

### 3.2 Threading model without QThread
- **One acquisition thread per run** (`threading.Thread(name=f"run-{run_id}", daemon=False)`). It does
  every instrument, serial and file I/O call for that run.
- **Instrument ownership.** While a run is active, no SCPI is sent from any other thread, including stop
  and abort (§3.3). When idle, `identify` may talk to the SMU from a request thread, but only while the
  session holds state `identifying`, so `start` returns 409 during it and vice versa (§7.1). This is an
  in-process guarantee only; cross-process contention (PySide6 app and sidecar on the same GPIB/COM
  address) is unprotected in step 1 and recorded in §11.
  - Commit `66a70f6` is relevant only indirectly: it made `VisaInstrument.close()` close only its own
    device session, because pyvisa's `ResourceManager` is a process-wide singleton and closing it severed
    every other live session. Several sessions in one process are therefore expected.
- **`SleepInhibitor.inhibit` and `uninhibit` run on the acquisition thread**, as they do today (W:518,
  W:1200). On Windows `SetThreadExecutionState` only affects the calling thread
  (`system_utils.py:132-142`).
- **Commands (HTTP handlers, Qt slots) never block on I/O.** They change `RunControl` state and return.
  `start()` validates, creates the run, spawns its thread and returns `run_id` immediately; pre-run
  prompts are raised **on the run thread** (§3.5).
- **Ordering guarantee:** run events are delivered in emit order, and `run_ended` is always last.
  - Only the run's thread emits run events (including pre-run prompts and `run_ended{cancelled}`), so
    `seq` is a plain counter on that thread and the sink is called in order with no lock held.
  - Command methods do not emit. The acquisition thread acknowledges them (`paused`, `resumed`,
    `stopping`) when it observes the change.
  - **Exception, for Qt parity only:** the legacy adapter's `pause_measurement()`,
    `resume_measurement()` and `stop_measurement()` keep emitting their status Signal directly on the GUI
    thread, with the existing `if self.running` gate on pause/resume (W:1184-1196, W:1392-1394). The Qt
    sink ignores `paused`/`resumed`/`stopping`/`prompt_resolved`, so nothing is duplicated.
- **Sinks must not block.**
  - The Qt sink emits a Signal (queued delivery to the GUI thread).
  - The API sink appends to a `collections.deque` and schedules at most one pending drain callback on the
    event loop (§7.3), so the loop's callback queue stays bounded.
  - The test sink appends to a list.

### 3.3 `RunControl` and lock discipline

`session/control.py` holds all mutable state shared between the command side and the acquisition
thread. **Fields arrive with the PR that first needs them**, so each diff justifies its own additions:

```python
class RunControl:
    """Command-side → acquisition-thread signalling. No I/O, no emits."""
    def __init__(self):
        self._lock = threading.Lock()
        self._running = False                 # PR-21: same bool semantics as W:93-127
        self._paused = False                  # PR-21
        self._event_markers = []              # PR-05 queue (D5), moved here in PR-21
        self.proceed_event = threading.Event()  # PR-21: W:1368
        self._end_reason = None               # PR-32b: first writer wins, under _lock
        self._prompt = None                   # PR-31b: PendingPrompt or None
        # PR-42b adds abort(); PR-42a adds sleep()
```

**Lock rules** (enforced in review, and stated in the module docstring):
1. Each object has at most one lock.
2. Never hold a lock during VISA, serial or file I/O, during `sleep`/`wait`, or while calling the sink.
3. Locks protect only the fields above. Worker-private state (`keithley`, `exporter`, per-mode dataclasses,
   counters) is touched by the acquisition thread only, as today (W:65-90).
4. `filename` and `run_id` are published through `file_opened` and `MeasurementSession.status()`. The
   unlocked read of `worker.filename` (MW:2869) keeps working in the adapter for the transition only.

**Stop semantics, mapped site by site.**
- **PR-21 (behavior-preserving):** `running`/`paused` stay plain lock-guarded bools. `execute()` still
  sets `running = True` on entry (W:136), so a stop issued before entry is still overwritten, exactly as
  today. Every internal `self.running = False` stays a `running = False`.
- **PR-32b (additive):** `control.finish(reason)` sets `running = False` and records the reason if none is
  recorded yet (first writer wins). The internal sites become `finish(...)`: W:599 `sweep_error`/`completed`,
  W:850 `compliance_stop`, W:880 `overpower`, W:1045 `target_samples`, W:1055 `duration`, W:1091
  `write_error`. `stop()` is `finish('user_stop')`. The PR body lists every site; a concurrent user stop
  and self-stop resolve to whichever took the lock first, and the PR says so.
- **Prompt wake-up:** stop also sets `proceed_event` (today's W:1396), so "wait for answer or stop" is one
  `Event.wait()` with no polling. The docstring says so.
- **PR-42a (named behavior change):** a stop issued before `execute()` begins is honored rather than
  overwritten (both Qt and headless paths).

**Stop vs abort.** Both end through the same shutdown path: `:OUTP OFF` → `exporter.finalize` →
`_cleanup` (W:1093-1120, W:1198-1225). `abort()` (PR-42b) additionally cancels a pending prompt; it is
used for app/sidecar shutdown and the stdin-EOF watchdog.

**Stop latency.** Neither can interrupt a VISA call in flight. The worst-case delay before the loop sees
a stop:

| Blocking wait | Where | Bound |
|---|---|---|
| VISA I/O timeout per query | `instrument.py:155`, `:167` | 5 s |
| Continuous read retry loop | W:635 (`max_retries = 5`), W:675-697 | 5 × 5 s + backoff 0.1+0.2+0.4+0.8 s ≈ **26.5 s** |
| Periodic health check `:SYST:ERR?` | W:1240-1261 (every 30 s, W:90) | 5 s |
| `*RST` settle | W:186; vdP W:1459 | 0.5 s each |
| aux `wait_ready` | W:464 | `AUX_READY_TIMEOUT_S = 5.0` (`constants.py:182`) |
| sweep bulk `:READ?` | W:524-528 | `max(10 s, points × 1 s)`; not interruptible |
| delta-mode settling + 2 reads | W:1279-1288 | `2 × fpp_delta_settling` + 2 × 5 s |
| vdP settling + averaged reads | W:1569-1574; `_read_averaged` W:1628-1633 | `2 × vdp_settling_s` + `2 × n × 5 s` |

**Realistic worst case: about 32 s for continuous modes** (retry loop plus a health check), and
effectively unbounded for a long sweep. The sidecar shutdown grace period (§7.5) is sized from this.

**PR-42a** replaces the settle sleeps with `control.sleep(s)`, which **raises `RunStopped`** (a plain
exception class) when stop is set, following the existing vdP `_VdpAborted` pattern (W:1318, W:1564-1565).
Consequence, stated in the PR: an interrupted settle **skips the read and row write that follow**, so no
unsettled sample, row or `vdp_geometry_complete` is produced. It also checks `control.stopped()` between
retries (W:675-697) and between vdP averaged reads (W:1632). Replaced sites: W:186, 609, 686, 1279, 1287,
1459, 1569, 1573. Loop pacing (W:1087) and the pause poll (W:640) are deliberately unchanged.

**Pause.** The output stays on (W:1184-1187), unchanged. What does change, in its own named PR-46 (D1):
the duration limit counts **active** time only. Today the loop sleeps 0.1 s and `continue`s past the
duration check (W:639-641) while `time.time() - self.start_time` keeps running (W:1089), so a run paused
across its own deadline ends the instant it resumes. PR-46 accumulates paused time in `RunControl` and
subtracts it in the duration check only — `elapsed_s`, `t_unix` and the CSV elapsed column stay wall-clock.

### 3.4 Event vocabulary

The envelope is identical in-process and on the wire (§7.2): `Event(v, type, run_id, seq, t, payload)`.
- `t` is the wall-clock emit time.
- Each `payload` is its own pydantic model in `session/events.py`, **added in the PR that first emits it**
  (§9), together with its regenerated `contracts/events.schema.json` diff.
- Every legacy Signal has exactly one source event, so the Qt sink in `workers.py` is an explicit
  `if/elif` table. Events without a legacy Signal are ignored by the Qt sink.

| Event | Payload (key fields) | Emitted when | Replaces | PR |
|---|---|---|---|---|
| `run_started` | `mode, sample_name, username, settings` (resolved dict), `started_at` | First statement of `execute()` | — | 32a |
| `log` | `level: info\|warning\|error, code, message` | Every current `status_update` site. `message` is the exact current text. | `status_update(str)` | 13, 30b |
| `instrument_connected` | `address, idn, model, max_source_v, max_source_i, max_power_w` | At the `instrument_identified` position (W:167, W:1446), **before** the "Detected:" status and LFR query | `instrument_identified(str)` → `model` | 30a |
| `line_frequency` | `hz, assumed` | After `:SYST:LFR?` (W:181-185). **Continuous modes only**; vdP never queries it | the fallback warning's context | 30a |
| `aux_connected` | `driver, address, channels [{key,label,unit}]` | After `wait_ready` (W:465-471) | status text | 32a |
| `file_opened` | `path, columns, units` | After `make_exporter` (W:508-512, W:1527-1530) | status text, unlocked `worker.filename` | 32a |
| `sample` | `t_unix` (W:642), `elapsed_s`, `compliance: OK\|V_COMP\|I_COMP`, `event_marker` (pending marks joined with `; `, D5), `values: Dict[str, Any]` (exact `data_dict`, **no coercion**: `aux_fault` stays the string `'0'`) | At W:1048 | `data_point(float, dict, str, str)` | 30a |
| `sample.derived` / `sample.delta` | `derived {ratio, rs, rho, sigma, v_unc, i_unc, method: f84\|legacy}`, `delta {v_plus, v_minus, r_f, r_r}` (optional fields on `sample`, 4PP only) | same | — | 32c |
| `sweep_segment` | `direction: forward\|reverse, voltages, currents, compliance` | W:590-593 (twice for `up_down`) | `sweep_complete(list, list, list)` | 30a |
| `compliance` | `kind: voltage\|current, stop_on_compliance` | W:844 (every sample, as today); vdP W:1582 | `compliance_hit(str)` | 30a, 31a |

**Compliance rate (D2).** The run procedure keeps emitting `compliance` per out-of-compliance sample, so
the Qt sink behaves exactly as today. The WebSocket/pull hub forwards only **transitions**: a `compliance`
event is sent when its `kind` differs from the last one forwarded for this run, and that state is cleared by
any intervening `sample` with `compliance: OK`. The per-sample truth stays visible in `sample.compliance`,
so no client loses information. Implemented in PR-51a, tested with a synthetic OK/V_COMP/OK/V_COMP stream.
| `overpower_trip` | `measured_w, stop_w` | W:865-868 | `overpower_hit(float, float)` | 30a |
| `error` | `code, source: smu\|aux\|file\|run, message` (current text via `humanize_connection_error`), `fatal` | W:199, 357, 443, 473, 514, 597, 611, 661, 692, 696, 871, 1041, 1117, 1423 | `error_occurred(str)`; `source` replaces the substring test (MW:2831) | 13, 30b |
| `paused` / `resumed` / `stopping` | `reason` | When the acquisition thread observes the change | — (Qt keeps its GUI-thread status, §3.2) | 32a |
| `prompt` | `prompt_id, kind, options [str], requires_human: bool, detail {}` | §3.5 | `geometry_ready(int, dict)` (for `kind=vdp_geometry`) | 31b |
| `prompt_resolved` | `prompt_id, choice, answered_by` | After the answer is consumed | — | 32a |
| `vdp_geometry_complete` | `index, name, group, label_pos, v_pos, label_neg, v_neg, current_a` | W:1601 | `geometry_complete(int, dict)` | 31a |
| `vdp_result` | the 15 fields at W:1650-1666 | W:1667 | `vdp_complete(dict)` | 31a |
| `acquisition_finished` | `mode` | Exactly where `measurement_complete` fires today (W:1114) | `measurement_complete(str)` | 30a |
| `file_finalized` | `path, end_metadata` | W:1109, W:1675, or cleanup | status text | 32a |
| `run_ended` | `reason: completed\|user_stop\|aborted\|cancelled\|target_samples\|duration\|compliance_stop\|overpower\|read_error\|write_error\|sweep_error\|<error code>`, `ok, samples, duration_s, path` | **Always last**, from `finally` after `_cleanup` | `QThread.finished` | 32b (`cancelled` in 41) |

**Why `acquisition_finished` and `run_ended` are separate.** Today `measurement_complete` fires *before*
`_cleanup`'s "Instrument disconnected." status (W:1114 vs W:1206). Mapping it onto `run_ended` would
reorder the PySide6 status log.

**Exit paths without an `error` event.** In 4PP delta mode a single read exception with
`consecutive_errors < max_retries` emits only a status message, then `if not read_success: break`
(W:658-667, W:699-700). `run_ended.reason = read_error` therefore can occur with no preceding `error`.
**PR-04 (Phase 0) fixes this** (D9) by putting the delta read through the same bounded retry loop as the
non-delta path, so `read_error` always follows an `error` event and no golden or event test encodes the bug.

**`log.code`** is a short closed set (`connecting`, `connected`, `model_unknown`, `lfr_assumed`,
`configuring`, `filter`, `power_envelope`, `aux_fault`, `event_marked`, `retry`, `recovered`, `progress`,
`write_failed`, `autosave_failed`, `compress`, `large_file`, `cleanup`, `instrument_error_queue`,
`output_off`, `completed`). PR-30b's description carries a table `line → event → level/code/source/fatal`
for every site, and the reviewer checks the diff against that table. The per-sample "Running …" text
(W:1068-1085) becomes `code=progress`; the WebSocket hub throttles it to 2 Hz (§7.3).

**Non-finite floats.** On the wire, non-finite floats serialize as `null` via pydantic's
`ser_json_inf_nan`; in-process sinks receive real floats.

### 3.5 Commands and prompts

**Commands** are `MeasurementSession` methods. The HTTP routes in §7.1 are one-line wrappers.

| Command | Valid in state | Effect | Today |
|---|---|---|---|
| `start(RunRequest)` | `idle` | resolve + validate settings (§4) → create `RunControl` + run → spawn thread → return `run_id` | MW:2165-2242, MW:1268-1339 |
| `stop()` | `awaiting_prompt`, `running`, `paused` | `control.finish('user_stop')` (wakes a prompt wait) | W:1194-1196, W:1392-1396 |
| `abort()` | any non-idle | also cancels the pending prompt (PR-42b) | — |
| `pause()` / `resume()` | continuous modes | set `control.paused`; output stays on; paused time excluded from the duration limit (PR-46) | W:1184-1192 |
| `mark_event(label)` | continuous modes, `running` | append to the marker queue (D5, PR-05) | W:1181-1182, W:888 |
| `answer_prompt(prompt_id, choice, fields={})` | `awaiting_prompt` | first valid answer wins; stale id or second answer → conflict | W:1388-1390 |
| `identify(address)` | `idle` | state `identifying` → `*IDN?` → `idle` | MW:3472-3513 |
| `status()` | any | `{state, mode, run_id, path, last_seq, pending_prompt}` | — |

**Prompts** model every blocking operator decision as data. The **run thread** emits `prompt`, sets
`control._prompt`, waits on `proceed_event` (which stop also sets), then emits `prompt_resolved`.

| `kind` | When | Options | `requires_human` | Today |
|---|---|---|---|---|
| `safety_voltage_ack` | On the run thread, **after `run_started` and before `Keithley2400(...).connect()`**, when `safety.is_potentially_hazardous(resolved, mode)` is true and the profile is not silenced | `acknowledge` (+ `fields.silence_for_profile`), `cancel` | true | Modal at MW:2074-2111 |
| `vdp_geometry` | Before each geometry, after the `clear()` (W:1546) | `proceed`, `abort` | true (a human rewires leads) | `geometry_ready` + button (MW:1350-1377) |
| `cable_null_shorted` (step 2) | Before cable null | `proceed`, `cancel` | true | MW:3405-3411 |

Notes:
- **`cancel` on `safety_voltage_ack`** ends through the normal `finally` → `run_ended{reason: cancelled}`
  with no instrument opened and no file created. The PySide6 path keeps its own modal until parity.
- **`requires_human` is UI-only (D4).** A prompt marked `requires_human` may be answered only by a client
  holding the **`ui` credential**; any other role gets 403. Step 1 mints exactly one token and it carries the
  `ui` role, so behaviour is unchanged today and the check is already in place when v2.0 adds an `mcp` token.
  An LLM client can start, stop and watch a run; it cannot assert that leads were rewired or that a hazardous
  voltage was acknowledged.
- **Disconnect policy and prompt timeout (D3).** A prompt survives client disconnect; `GET /session` shows
  `pending_prompt` so a client can re-attach. A **`MeasurementSession` run** left in `awaiting_prompt` longer
  than `prompt_timeout_s` (default 900 s, per `RunRequest`) takes the `abort()` path: output off, file
  finalized, instrument and aux ports closed, sleep inhibitor released, `run_ended{reason: prompt_timeout}`
  (PR-42c). The PySide6 path keeps waiting forever (W:1563) — an operator is standing at the bench. For vdP
  the output is already OFF during the wait (W:1578-1579), so the wait is never hazardous, only
  resource-holding.
- **Overpower is not a prompt.** Enforcement stops the run and turns the output off (W:856-880); the
  pre-flight refuses to start (W:354-365). Both are `error` + `overpower_trip` notifications.
- **No pre-answering (D4).** `RunRequest.acknowledge` is dropped from the design. Every prompt in step 1 is
  `requires_human`, and answering one before it is raised is exactly the confirmation-skipping D4 forbids. A
  field for pre-answering non-human prompts can be added when the first such prompt exists.

---

## 4. Settings schema

### 4.1 Principles
- **Pydantic v2 `BaseModel`s, in `resistamet_gui/schema/`.**
  - Field names are **identical** to today's dict keys, so every field greps one-to-one.
  - Python 3.9 (`pyproject.toml:17`; CI matrix): `typing.Optional`/`Union`/`Literal`, no
    `from __future__ import annotations` in schema modules.
- **During the transition the schema is a boundary contract. The run procedures keep reading the legacy
  nested dict.** Converting ~150 read sites to attribute access is out of scope.
- **Defaults have one source: `DEFAULT_SETTINGS`.** Each field reads its default explicitly from the dict,
  e.g. `nplc: float = DEFAULT_SETTINGS['measurement']['nplc']`. No re-typed literals, no equality test.
- **Bounds are checked against the widgets, not by eye.** A PySide6 `importorskip` test reads each tab
  widget's `minimum()`/`maximum()` on a window and compares with the model's `ge`/`le` (PR-11a/b).
- **Strict validation for API run requests; lenient for stored profiles.** A stored out-of-range value
  produces an `issue` and passes through unchanged. Unknown keys are preserved (`extra='allow'`), matching
  `config.py:119-121`.

### 4.2 Models and fields

Defaults: `DEFAULT_SETTINGS` (by reference). Bounds: current widget ranges (verified by the PR-11 test).

**`InstrumentSettings`** (every mode reads it):

| field | type | unit | constraint | today |
|---|---|---|---|---|
| `gpib_address` | str | — | non-empty; machine-local, never stored in a profile | `config.py:46-78` |
| `nplc` | float | PLC | 0.01–10 | tab for 4PP/sweep/vdP, otherwise the dialog (MW:2016-2021) |
| `sampling_rate` | float | Hz | 0.1–100; soft cap from `TimingSettings.max_rate_hz()` reported as derived, not enforced (MW:3118-3135) | tab or profile (MW:2022-2025) |
| `settling_time` | float | s | 0–10 (`dialogs.py:108`) | profile (MW:2039) |
| `auto_zero` | `Literal['on','once','off']` | — | forced `on` for `four_point` and `vdp` | tab or profile (MW:2030-2033) |
| `filter_enabled` / `filter_type` / `filter_count` | bool / `Literal['repeat','moving']` / int | — | count 1–100; forced 10 for `four_point`, `vdp` | dialog + override |
| `stop_on_compliance` | bool | — | continuous modes (W:841) | dialog |

**`ResistanceSettings`:** `res_test_current` (A, 1e-7–3.0, MW:334); `res_voltage_compliance` (V, 0.1–200,
MW:336; safety key `safety.py:68`); `res_measurement_type` (`Literal['2-wire','4-wire']`);
`res_auto_range`; `res_offset_comp` (halves the rate cap, MW:3102-3106); `res_cable_null` (Ω, ≥ 0 and
finite — procedure-written *state* (MW:3438), not operator input; it stays in the dict because W:235 reads
it).

**`VoltageSourceSettings` / `CurrentSourceSettings`:** `vsource_voltage` (V, −200–200, MW:421; safety key);
`vsource_current_compliance` (A, 1e-7–3.0); `vsource_current_range_auto`; `vsource_duration_hours` (h,
0–168; `0` = until stopped); `isource_current` (A, −3–3, MW:479); `isource_voltage_compliance` (V,
0.1–200; safety key); `isource_voltage_range_auto`; `isource_duration_hours` (h, 0–168).

**`FourPointSettings`:**

| field | type | unit | constraint |
|---|---|---|---|
| `fpp_current` | float | A | −3–3 |
| `fpp_voltage_compliance` | float | V | 0.1–200; safety key |
| `fpp_voltage_range_auto` | bool | — | — |
| `fpp_spacing_cm` | float | cm | 0.001–5.0 |
| `fpp_thickness_um` | float | µm | 0–5000; `0` = unknown. The legacy cm migration is not in the resolver (the MW:1852-1854 branch is dead, §2.3; the live one at MW:1773-1776 moves with profile I/O in step 2). |
| `fpp_alpha` / `fpp_k_factor` | float | — | 0–10 / 0.1–50 |
| `fpp_samples` | int | readings | 0–1e6; `0` = continuous (W:621) |
| `fpp_model` | `Literal['thin_film','semi_infinite','finite_thin','finite_alpha']` | — | — |
| `fpp_diameter_cm` | float | cm | 0–100; `0` = infinite |
| `fpp_geometry` | `Literal['circle','square','rectangle_2','rectangle_3','rectangle_4']` | — | — |
| `fpp_temperature_c` | `Optional[float]` | °C | `None` = not measured; resolver converts `None` ↔ NaN. **The −50 widget sentinel (MW:1983-1985) stays in the UI** and is never seen by the resolver. |
| `fpp_dopant_type` | `Literal['none','n','p']` | — | — |
| `fpp_delta_mode` / `fpp_delta_settling` | bool / float | — / s | settling 0.01–5.0 |
| `fpp_power_warn_w` / `fpp_power_stop_w` | float | W | 1e-4–10 / 1e-4–22. Derived `worst_case_power_w = |I|·|V_comp|`; **strict request with worst case > stop is rejected** (mirrors W:354-365). |
| `fpp_stop_on_overpower` | bool | — | — |

**`SweepSettings`:** `sweep_source` (`Literal['voltage','current']`); `sweep_start`/`sweep_stop` (±200
regardless of source, as MW:900-902; source-aware bounds are a follow-up); `sweep_step` (> 0);
`sweep_compliance` (1e-7–3.0 for both sources, MW:911); `sweep_delay` (s, 0–10); `sweep_direction`
(`Literal['up','down','up_down']`). Derived: `points = round(|stop−start|/step)+1`, ×2 for `up_down`
(MW:1002-1017; `instrument.py:309`).

**`VdpSettings`:** `vdp_current` (A, > 0 (W:1454), ≤ 1.0); `vdp_voltage_compliance` (V, > 0 (W:1456), ≤ 200;
safety key); `vdp_voltage_range_auto`; `vdp_thickness_cm` (cm; **strict requires > 0**, UI prompt at
MW:2139-2163, read at W:1642); `vdp_settling_s` (s, 0–10); `vdp_readings_per_polarity` (1–100).

**Field groups serialized under `measurement`** (not new top-level sections):
- `AuxSensorSettings`: `aux_log_enabled`, `aux_driver` (validated against `sensors.available_sensors()`),
  `aux_address` (`constants.py:87-89`). Constraint: `aux_log_enabled` applies only to
  `data_export.AUX_LOG_MODES = ('resistance','source_v','source_i','four_point')` (`data_export.py:45`);
  strict requests for `sweep`/`vdp` with it on get an `issue`.
- `SafetySettings`: `safety_voltage_warn_v`, `safety_voltage_warn_silenced` (`constants.py:114-115`; read
  from `user_settings['measurement']` at MW:2088-2089).

**Top-level sections:** `FileSettings` (`auto_save_interval`, `data_directory`); `OutputSettings`
(`format`, `compression`, `compression_threshold_mb`; `constants.py:138-152`); `DisplaySettings`
(frontend-only; modelled so the profile round-trips).

**`RunRequest`** (not persisted): `mode: Literal[...]`; `username` (must be in `users`;
`config.py:198-211`); `sample_name` (trimmed, non-empty; MW:2113-2137); `overrides: Dict[str, Any]` (flat
measurement keys, as tab widgets supply today); `prompt_timeout_s: float = 900` (D3; session runs only).
- In strict mode the resolver returns 422 for override keys that are not fields of the mode model or the
  shared instrument/aux groups, and for keys the resolver overwrites from the profile (`settling_time`,
  `gpib_address`, MW:2039-2040). Clients discover valid keys from `GET /schema/settings`.

### 4.3 The resolver replaces what `gather_settings_for_mode` does beyond reading widgets

`schema/resolve.py`:

```python
def resolve_run_settings(profile: dict, mode: str, overrides: dict, *, strict: bool) -> ResolvedRun:
    """Pure. Same output dict as MW:1937-2050 given the same widget values as `overrides`."""
```

Steps, in order, each a few explicit lines with a MW citation comment:
1. Shallow-copy `measurement`, `display`, `file` (MW:1940-1944), plus `output` after PR-02.
2. Strict only: reject unknown and profile-owned override keys (§4.2).
3. Apply `overrides`.
4. NPLC and sampling-rate fallback (MW:2016-2025).
5. `auto_zero` fallback `'once'` (MW:2030-2033).
6. Continuous → 0 h (MW:2035-2038; overrides carry a `*_run_continuous` flag).
7. `settling_time` and `gpib_address` from the profile (MW:2039-2040).
8. `MODE_TIMING_OVERRIDES` (MW:2045-2049).
9. Validate the mode model plus shared groups, producing `issues`.
10. Derived: `max_rate_hz` via `timing.py`, `sweep_points`, `worst_case_power_w`.
11. `hazard = safety.is_potentially_hazardous(resolved, mode)` on the **resolved** settings.

`ResolvedRun` is a dataclass: `settings: dict`, `issues: List[Issue]`, `derived: dict`, `hazard`.

### 4.4 `config.json` and profiles
- **Format unchanged in step 1.** `ConfigManager` keeps storing dicts; `get_user_settings` stays the
  profile source (`config.py:143-159`, machine-local GPIB injection at `:158`).
- **Concurrent writers.** `save_config` truncates and rewrites in place (`config.py:134-137`) and nothing
  is locked. Under FastAPI, sync handlers run in a threadpool, and PR-41 persists the silence flag from
  the run thread. PR-44 adds one `threading.Lock` around mutate-and-save plus atomic write (temp file +
  `os.replace`), as its own called-out PR before any API route.
- **Known persistence quirks, recorded rather than fixed:** the Settings dialog saves whole sections, so
  later `DEFAULT_SETTINGS` changes never reach existing users (`config.py:173-181`); `aux_address` is
  per user, not per machine (`config.py:18`); file "profiles" restore only a subset of fields
  (MW:1748-1778).

---

## 5. What moves out of `main_window.py`

Classes: **UI** stays in the frontend; **Backend** moves into `session/`, `schema/`, `data_export.py` or
`calculations*.py`; **Round-trip** is a prompt/answer pair (§3.5). Step 1 = this design; step 2 =
alongside the Tauri UI.

| Piece | Location | Class | Step |
|---|---|---|---|
| Run state: `measurement_worker`, `measurement_running`, `active_mode` | MW:46, MW:72-73 | Backend (`MeasurementSession.status`) | 1 |
| Pre-start checks: one run at a time, user, sample name, vdP thickness | MW:2166-2172, MW:1270-1285, MW:2113-2163 | Backend validation + UI prompts | 1 |
| `gather_settings_for_mode` minus widget reads; mode overrides; continuous → 0 | MW:2014-2049 | Backend (`schema/resolve.py`); widget reads and the −50 °C sentinel stay UI | 1 |
| Voltage-safety check + sticky silence | MW:2074-2111; `safety.py:79-135` | Round-trip (`safety_voltage_ack`) + backend check | 1 |
| Worker creation and signal wiring | MW:2212-2221, MW:1327-1338 | Backend (session / event sink) | 1 |
| Stop / pause / resume / mark | MW:2244-2292; W:1181-1196 | Backend commands + UI buttons | 1 |
| End-of-run handling, file path, end reason | MW:2858-2896; W:1053-1114 | Backend (`run_ended`, `file_opened`) + UI reset | 1 |
| vdP "Measure this configuration" wait | MW:1350-1377; W:1388-1390, W:1563 | Round-trip (`vdp_geometry`) | 1 |
| Status colouring by substring; address-error heuristic | MW:3191-3195, MW:2820-2841 | Backend `log.level` / `error.source` + UI render | 1 |
| Sampling-rate cap and suggestion | MW:3084-3183 | Backend derived value + UI clamp | 1 (value), 2 (UI) |
| Test Connection; GPIB picker listing | MW:3472-3513, MW:3267-3299 | Backend `identify` + resources endpoints | 1 (endpoints) |
| Buffer routing and running stats | MW:2441-2449; `buffers.py:36-99` | Backend (queryable stats for MCP) | 2 |
| 4PP per-row Rs/ρ/σ recomputation (legacy-only; epoch "elapsed") | MW:2459-2493 vs W:908-999 | Backend (`sample.derived`, emitted in step 1) | UI switch in 2 |
| 4PP live stats + combined uncertainty (BD excluded) | MW:2702-2744 | Backend + UI formatting | 2 |
| 4PP spot save/clear, per-spot stats (BD *included*, MW:3342) | MW:864-866, MW:3330-3395 | Backend spot model + UI tables | 2 |
| 4PP Export Summary (third formula copy) | MW:2942-3055 | Backend (`data_export`) + UI dialog | 2 |
| vdP per-geometry R and duplicate uncertainty | MW:1406-1436 vs W:1609-1626 | Backend (use `vdp_result`) | 2 |
| Cable null SCPI procedure on the GUI thread | MW:3412-3442 | Backend procedure + `cable_null_shorted` | 2 |
| Aux preview port lifecycle and handoff | MW:2203-2207, MW:2311-2412, MW:2896 | Backend (single port owner) | 2 |
| Results Viewer parse, time-column detection, stats | MW:1562-1677 | Backend (`/results/load`) + UI plot | 2 |
| Profile-file save/load, live µm/cm migration | MW:1719-1787 (migration MW:1773-1776) | Backend (schema I/O) + UI dialogs | 2 |
| Plot/readout timers, formatting, compliance flash, tables, filmstrip, plot export | MW:47-58, MW:2504-2587, MW:2843-2856, MW:3198-3265 | UI | — |
| Engineering-notation helpers | `ui/widgets.py:45`, `:82`, `:212` | Qt-free module (move-only) | 2 |

---

## 6. PySide6 compatibility shim

**Decision:** during the transition, `MeasurementWorker` and `VdpMeasurementWorker` **stay `QThread`
subclasses** in `workers.py`, with an unchanged public surface: constructors; the Signals at W:41-51 and
W:1351-1357; `start`, `wait`, `isRunning`, `finished`; `stop_measurement`, `pause_measurement`,
`resume_measurement`, `mark_event`, `proceed`; `filename`.

Final shape (after Phase 3):

```python
class MeasurementWorker(QThread):
    data_point = Signal(float, dict, str, str)
    ...                                              # unchanged declarations

    def __init__(self, mode, sample_name, username, settings, parent=None):
        super().__init__(parent)
        self._control = RunControl()
        self._run = ContinuousRun(mode, sample_name, username, settings, self._control)
        self.mode = mode
        self.settings = settings

    def run(self):
        self._run.execute(EventEmitter(sink=self._to_signals))

    def _to_signals(self, ev):
        if ev.type == 'log':
            self.status_update.emit(ev.payload.message)
        elif ev.type == 'sample':
            p = ev.payload
            self.data_point.emit(p.t_unix, p.values, p.compliance, p.event_marker)  # values uncoerced
        elif ev.type == 'error':
            self.error_occurred.emit(ev.payload.message)
        ...                                          # one branch per legacy Signal (§3.4)

    def pause_measurement(self):                     # GUI-thread emit kept (W:1184-1187)
        if self._control.running:
            self._control.paused = True
            self.status_update.emit(f"Measurement ({self.mode}) paused")

    @property
    def filename(self):
        return self._run.filename                    # same unlocked read as today (MW:2869)
```

**Why wrap the procedure rather than `MeasurementSession`:**
1. **`QThread` semantics come for free.** `finished` fires after `run()` returns, `wait(2000)` works in
   `closeEvent` (MW:3525), and `isRunning`/`processEvents` in `tests/test_workers.py:176-204` behave the
   same. A bridge that fakes these from a Python thread is exactly the clever indirection to avoid.
2. **Queued Signal delivery stays Qt-native** (the emitting thread is still a `QThread`).
3. **`SleepInhibitor` still runs on the same thread** for inhibit and uninhibit (§3.2).
4. **`tests/test_workers.py` passes unchanged**, which is the evidence the extraction preserves behaviour.

**What stays shared, and what does not.** Shared: procedure code and event contract. Not shared: thread
ownership, pre-run prompts and the one-run rule (the ~250-line session). `main_window.py` keeps its own
pre-run modals until parity and never calls `MeasurementSession`. The vdP adapter's `proceed()` answers
the pending prompt (after PR-31b); `stop_measurement()` emits its status on the GUI thread, then calls
`control.finish('user_stop')`, which also releases the wait (W:1396).

**Removal.** Once the Tauri UI reaches parity, `workers.py`, `ui/` and the Qt test files are deleted in one
PR, after every assertion is ported (§8).

---

## 7. API sketch (just enough to validate the session shape)

`resistamet_gui/api/`, behind an optional extra `api = ["fastapi", "uvicorn", "httpx"]` (`httpx` is
required by FastAPI's `TestClient`). pydantic becomes a core dependency in PR-10.

### 7.1 HTTP (prefix `/api/v1`)

| Method & path | Body → response | Session call / guard |
|---|---|---|
| `GET /health` | → `{app_version, api_version: 1, simulate, visa_backend, config_path, data_dir}` | — |
| `GET /instruments/resources` | → `[str]` | `list_resources()`; **409 unless idle** |
| `POST /instruments/identify` | `{address}` → `{idn, model}` or `error` | `session.identify`; **409 unless idle** (state `identifying` blocks `start`) |
| `GET /users`, `POST /users` | — | `ConfigManager.get_users/add_user` (`config.py:198-211`) |
| `GET /profiles/{user}` / `PATCH /profiles/{user}` | section dicts | `get_user_settings` / `update_user_settings`; **409 if the patch contains `gpib_address` while a run is active** (it routes to `set_gpib_address`, `config.py:161-170`) |
| `GET /schema/settings`, `GET /schema/events` | → JSON Schema | generated (§10.2) |
| `POST /settings/resolve` | `{username, mode, overrides}` → `{settings, issues, derived, hazard}` | `resolve_run_settings(strict=False)` |
| `POST /session/start` | `RunRequest` → `202 {run_id}`; `409` if not idle; `422` + issues | `start` |
| `POST /session/stop`, `/abort`, `/pause`, `/resume` | → `{state}`; `409` if invalid now | same |
| `POST /session/mark` | `{label}` | `mark_event` |
| `POST /session/prompts/{prompt_id}` | `{choice, fields}` → `200`; `409` if stale/answered | `answer_prompt` |
| `GET /session` | → status snapshot incl. `pending_prompt` | `status` |
| `GET /session/events?run_id=&since_seq=&limit=` | → `{events, gap?}` (HTTP pull over the same ring) | — |
| `POST /shutdown` | → `{state}` | abort + join (§7.5) |

**Binding and auth:** `127.0.0.1` only; a per-launch random token from the parent (`--token` or env) is
required on every request (`Authorization: Bearer`) and the WS query.

### 7.2 WebSocket envelope and resume cursor

`GET /api/v1/events?run_id=X&since_seq=N` (WS):

```json
{"v": 1, "type": "sample", "run_id": "20260917T141502-3f9a", "seq": 1842, "t": 1789654502.117,
 "payload": {"t_unix": 1789654502.101, "elapsed_s": 184.2, "compliance": "OK", "event_marker": "",
             "values": {"voltage": 0.1003, "current": 0.001, "resistance": 100.3, "resistance_unc": 0.012}}}
```

- **`v`** is the contract major version. Adding events/optional fields does not bump it; clients ignore
  unknown `type`s and fields. Removing/renaming bumps `v` and `/api/v2`.
- **`seq` starts at 0 for each run; the resume cursor is `(run_id, since_seq)`.** If `run_id` does not
  match the ring's run, or the gap is not fully retained, the server sends
  `{"type":"gap","payload":{"run_id":…, "from":a, "to":b}}` and the client re-syncs from `GET /session`.
- **One ordered ring** (capacity 3000) for the current or most recent run. On eviction, the oldest
  `sample`/`log{progress}` is removed first; other events are evicted only when no droppable event
  remains. Replay is therefore in order, with gaps only in droppable types.

### 7.3 Backpressure (`api/event_hub.py`)
- **Thread → loop hand-off:** the API sink appends to a `deque` and, if no drain is pending, calls
  `loop.call_soon_threadsafe(hub.drain)` once. The loop's callback queue holds at most one drain.
- **`EventHub.publish(event)`** runs on the loop: append to the ring, then for each client queue
  (bounded, 2000):
  - `sample` / `log{code=progress}` are dropped (and a `gap` noted) when the queue is above its
    droppable high-water mark (1900). The last 100 slots are reserved for non-droppable events.
  - `log{code=progress}` is rate-limited to 2 Hz per client.
  - A non-droppable event that does not fit even in the reserve disconnects that client, which must
    resume via the cursor.
- The acquisition thread never waits on a client. Tested with a stalled fake client.

### 7.4 MCP reuse (v2.0, not built here)
- MCP tools map one-to-one onto the §7.1 commands. A request/response client uses
  `GET /session/events` (pull) and `GET /session` instead of the WebSocket, so both client kinds use one
  contract.
- Prompts can arrive mid-run (vdP), so an MCP client learns of them by polling `GET /session` or the pull
  route; it must call `answer_prompt` explicitly — and per D4 an `mcp` credential is refused (403) on any
  `requires_human` prompt, so a human decision cannot be automated away.
- The MCP server is another client of the same session; one run lock and one event history serve both.

### 7.5 Sidecar shutdown order
1. The parent closes the sidecar's stdin or calls `POST /shutdown`.
2. The sidecar calls `abort()` and joins the run thread for up to **35 s** (the §3.3 continuous worst
   case plus margin), letting `:OUTP OFF` → finalize → `_cleanup` run.
3. The sidecar exits.
4. Only after that grace period does the parent (Tauri, step 3) kill the process tree.

**Accepted residual risk:** a hard kill mid-VISA call (or during a long sweep `:READ?`) leaves the output
ON. The UI must warn about it. Step 3 adds a startup check: if the previous run's `run_ended` was never
recorded, the sidecar sends `:OUTP OFF` to that address before accepting commands.

---

## 8. Testing strategy

**Rule: refactor PRs do not edit existing tests.** These pass unchanged in every PR of Phases 2–3:
`tests/test_workers.py` (1082 lines of signal, SCPI-log and on-disk assertions); `tests/test_gui_smoke.py`;
`tests/test_e2e_simulator.py` (own invocation, as CI does, `pytest.ini:11`); `tests/test_e2e_aux_*.py`.

**New test files** (headless ones use the Qt-free `fake_rm` fixture, `tests/conftest.py:19-48`, and the
`SleepInhibitor` monkeypatch pattern, `tests/test_workers.py:38-45`):

| File | Covers | PR |
|---|---|---|
| `tests/test_schema_no_qt.py` | Subprocess: `import resistamet_gui.schema` leaves `PySide6` out of `sys.modules` (pattern of `test_cli_smoke.py`) | 10 |
| `tests/test_settings_schema.py` | every settings dict in `test_workers.py:48-153, 872-882` validates; lenient keeps unknown keys; strict rejects vdP thickness 0, 4PP worst case > stop, unknown/profile-owned override keys | 10, 11a, 11b |
| `tests/test_settings_bounds.py` | `importorskip("PySide6")`: widget `minimum()/maximum()` == model `ge/le` per field | 11a, 11b |
| `tests/test_gather_golden.py` + `tests/fixtures/gather_golden/<mode>_<case>.json` | `importorskip("PySide6")`; own ~15-line window fixture duplicated from `test_gui_smoke.py:25` (not moved, not `sim_window_factory`, which patches pyvisa process-wide via `simulator.py:57`). For each mode and cases (defaults; non-default widget values; `*_run_continuous` on; −50 °C sentinel; tab NPLC ≠ profile NPLC; a profile without `auto_zero`; `MODE_TIMING_OVERRIDES` modes), `gather_settings_for_mode` == committed golden. Machine-dependent keys (`gpib_address`, `data_directory`) come from the explicit tmp config. | 12 |
| `tests/test_settings_resolve.py` | Pure resolver: same cases as the goldens, fed the widget values as `overrides`, == the same golden files; hazard on resolved values | 12 |
| `tests/test_event_models.py` | Envelope round-trip, NaN → null on the wire, `seq` monotonic; `sample.values` keeps `aux_fault == '0'` as `str` | 13, 30a |
| `tests/test_contracts_up_to_date.py` | Regenerate JSON Schema in memory == committed files | 14 |
| `tests/test_session_no_qt.py` | Subprocess: `import resistamet_gui.session` (extended to `resistamet_gui.api` in PR-50a) leaves `PySide6` out | 21 |
| `tests/test_qt_sink.py` | Adapter re-emits a sample with `aux_fault='0'` as a `str`, uncoerced | 30a |
| `tests/test_run_events.py` | `ContinuousRun`/`VdpRun` with a list sink: order, `run_ended` last with the right `reason` on every exit path (W:200, 365, 444, 476, 515, 599, 612, 699 (delta retries exhausted, after PR-04), 850, 880, 1045, 1055, 1091, 1117) | 32b |
| `tests/test_sample_derived.py` | `sample.derived` equals the CSV row values for F84 and legacy inputs | 32c |
| `tests/test_session.py` | state machine, one run at a time, `identify` vs `start` 409, stop/abort from `awaiting_prompt`, answer races, `cancelled` → no file, inhibitor on the run thread, concurrent self-stop vs user stop reason | 40, 41 |
| `tests/test_session_interruptible.py` | stop during settle returns within 0.2 s; **no row / sample / `vdp_geometry_complete` after stop-during-settle**; pre-entry stop honored | 42a |
| `tests/test_session_e2e.py` | Headless ports of `test_e2e_simulator.py:81-512`, listed per test in the PR body, including cable-null subtraction via the `res_cable_null` setting (:447) and abort-on-shutdown via the session (:483). The spot save/clear test (:363) is ported with the spot model in step 2 and says so. | 43 |
| `tests/test_config_concurrency.py` | parallel `update_user_settings` never yields invalid JSON; atomic replace | 44 |
| `tests/test_api_*.py` | `TestClient`: routes, 409/422, token required, WS envelope, resume cursor, stalled client, sidecar handshake read through a pipe | 50a–51c |

**CI for the API tests.** They `importorskip("fastapi")`, and CI installs only `requirements.txt`
(`.github/workflows/test.yml:52`). PR-50a adds a job that installs `.[api]` and runs
`pytest -rs tests/test_api_*.py`; its checklist confirms no skips.

**Frozen PySide6 exe.** `build.yml` runs only on tags and its smoke test is `--version`, which exits before
Qt or `main_window` is imported (`build.yml:46-54`). PR-10 adds a `--self-test` flag that imports
`resistamet_gui.schema` and `resistamet_gui.ui.main_window` and exits, runs it in `build.yml`, adds
`workflow_dispatch`, and PR-10 and PR-15 are each built via dispatch before merge.

**Harness notes:** headless tests take config path and data directory explicitly (`constants.CONFIG_FILE`
is bound as a default argument at import, `config.py:29`); `tools/bench_v2_export.py:24,85` drives
`MeasurementWorker` on the bench, so rerun it on the 2420 after PR-25b-1 and PR-42a.

**Existing tests that change:** none in step 1. `tests/e2e_utils.py` is already Qt-free (imports at
`:7-11`); `test_session_e2e.py` imports its CSV helpers as they are. Qt test files are deleted only in the
post-parity removal PR.

---

## 9. Migration plan

Every PR leaves `python resistamet-gui.py` working and the full suite green. Phases run in order; PRs in a
phase run in order unless marked independent. Kinds: **Move**, **Mech** (word-diff), **Refactor**,
**Add**, **Behavior**.

**Phase 0: isolated fixes** (independent; land first so goldens don't encode known bugs) —
**landed on `phase0/reviewable-baseline`**, one commit each, full suite green (603 passed).
- **PR-00 (Tests):** the GUI smoke fixture patched `constants.CONFIG_FILE`, but `ConfigManager` binds it
  as a default argument at import time, so a full-suite run wrote its test users into the working
  `config.json`. Found while writing PR-01's test; the fixture now passes the tmp path explicitly.
- **PR-01 (Behavior):** "Don't show again" persists, saving only the measurement section with
  `res_cable_null` removed.
- **PR-02 (Behavior):** deliver the `output` section to runs, **plus a one-time reset of every profile's
  `output` to the CSV defaults** (D6), recorded in an additive top-level `migrations` list.
- **PR-03 (Behavior):** safety check and status-bar hazard tag both use the gathered settings.
- **PR-04 (Behavior):** 4PP delta read gets the non-delta path's bounded retry loop (D9).
- **PR-05 (Behavior):** event marks queue instead of overwriting (D5).

**Phase 1: contracts, additive** — **landed**, one commit each, 701 tests green.
PR-13 and PR-14 ran after PR-15 rather than before it (both additive, nothing depended on the order);
the capture tool now calls the window's `_overrides_from_widgets` instead of keeping its own copy.
- **PR-10 (Add):** pydantic; `schema/settings_common.py`; `test_schema_no_qt.py`; `--self-test` + build.yml.
- **PR-11a (Add):** resistance, source-V/I, sweep, vdP models + bounds test.
- **PR-11b (Add):** 4PP model + worst-case power rule + `RunRequest` + bounds test.
- **PR-12 (Add):** gather goldens + `schema/resolve.py` + resolver tests against the same goldens.
- **PR-13 (Add):** `session/events.py` envelope + `log`/`error` payloads, `session/emitter.py`, `ListSink`.
- **PR-14 (Add):** `tools/export_contracts.py` + committed JSON Schema + drift test.
- **PR-15 (Refactor):** `gather_settings_for_mode` delegates to the resolver (widget reads stay).
  Landed: main_window −32/+23 lines, goldens re-captured against the refactored window are byte-identical.

**Phase 2: de-Qt the workers in place, then move** (tests unchanged throughout) — **landed**,
15 commits, 705 tests green. `workers.py` 1696 → 221 lines: two QThread adapters plus the outputs facade.
Deviations from the plan, all called out in their commits: PR-23b also collapsed the two duplicate path
sanitizers (the deferred item) and routed vdP through `create_base_path`; the parse/row-build functions
needed `nplc`, `use_delta` and `reading_str` passed explicitly; `session/delta.py` did not appear —
`_read_delta` stayed with `ContinuousRun`, which is where its state lives.
- **PR-20 (Mech):** every `self.<signal>.emit(` → `self._out.<signal>(`; `_QtOutputs` forwarder.
- **PR-21 (Refactor):** literal `RunControl` (`running`, `paused`, `event_marker`, `proceed_event`);
  worker properties delegate; `session/control.py`, `test_session_no_qt.py`.
- **PR-22a (Move-in-place):** each `if self.mode ==` configure branch → private method on the worker.
- **PR-22b (Refactor):** `self._fpp_*/_sweep_*/_cable_null` → one small frozen dataclass per mode.
- **PR-22c (Mech):** configure methods → module-level functions with explicit `(keithley, out, settings)`.
- **PR-23a (Move-in-place):** aux open, exporter open and sweep block → private methods.
- **PR-23b (Mech):** exporter/base-path creation → module-level functions returning `(exporter, filename)`.
- **PR-24a (Move-in-place):** reading parse (W:706-829) → per-mode methods returning
  `(data_dict, compliance_status, compliance_type)`.
- **PR-24b (Move-in-place):** row build (W:892-1028) → `_build_row(...)`.
- **PR-24c (Mech):** the PR-24a/b methods → module-level functions with explicit params. The aux read,
  compliance/overpower/marker block (W:830-890) stays inline in the loop.
- **PR-25a (Move):** module-level functions → `session/configure.py`, `samples.py`, `delta.py`,
  `run_files.py`.
- **PR-25b-1 (Refactor):** inside `workers.py`, `class ContinuousRun` takes the `run()` body and methods;
  `MeasurementWorker` composes it.
- **PR-25b-2 (Move):** `ContinuousRun` → `session/continuous_run.py`.
- **PR-26-1 (Refactor):** inside `workers.py`, `class VdpRun`; `VdpMeasurementWorker` composes it.
- **PR-26-2 (Move):** `VdpRun` → `session/vdp_run.py`.

**Phase 3: typed events** (Qt behaviour unchanged) — **landed**, 8 commits, 743 tests green.
Deviations: PR-30b also converted the `out.*` calls inside `configure.py`/`samples.py` (they take the
emitter now, not the facade); PR-32b additionally names the setup-failure exits, which previously ended a
run with no report at all.
- **PR-30a (Refactor):** data events (`sample`, `compliance`, `overpower_trip`, `sweep_segment`,
  `acquisition_finished`, `instrument_connected`, `line_frequency`) + their payload models.
- **PR-30b (Refactor):** `log`/`error` classification, with the per-site table in the PR body.
- **PR-31a (Refactor):** vdP `log`/`error`/`compliance`/`vdp_geometry_complete`/`vdp_result`.
- **PR-31b (Refactor):** `vdp_geometry` prompt + `RunControl` prompt fields; adapter `proceed()` answers it.
- **PR-32a (Add):** `run_started`, `aux_connected`, `file_opened`, `file_finalized`,
  `paused`/`resumed`/`stopping`, `prompt_resolved` (Qt sink ignores the ones without a Signal).
- **PR-32b (Add):** `control.finish(reason)` at every internal stop site + `run_ended{reason}` +
  `test_run_events.py`.
- **PR-32c (Add):** `sample.derived/delta` (row builder also returns the F84/legacy values) + test.
- **PR-33 (Behavior):** the PySide6 4PP panel/table reads `sample.derived` instead of recomputing (D7).

**Phase 4: headless session** — **landed**, 10 commits, 794 tests green. Deviations: the emptied
`_QtOutputs` facade was deleted in its own commit before PR-40; PR-41's silence flag is recorded on the
event stream rather than persisted (persistence belongs with the settings routes, PR-50b); PR-45's lock is
released on the safety-decline path too, which the plan did not call out.
- **PR-40 (Add):** `session/manager.py` `MeasurementSession` (thread, state, commands, `identify`, status).
- **PR-41 (Add):** `safety_voltage_ack` on the run thread, `cancelled`, flag-only silence save.
- **PR-42a (Behavior):** interruptible settles via `control.sleep` → `RunStopped`; stop checks between
  retries and vdP averaged reads; pre-entry stop honored.
- **PR-42b (Add):** `abort()`.
- **PR-42c (Add):** `prompt_timeout_s` → abort from `awaiting_prompt`, session runs only (D3).
- **PR-43 (Tests):** headless e2e.
- **PR-44 (Behavior):** `ConfigManager` lock + atomic write.
- **PR-45 (Behavior):** per-address OS file lock around instrument and aux ports, shared by the PySide6
  start path and the session (D8).
- **PR-46 (Behavior):** the duration limit counts active time, excluding paused time (D1).

**Phase 5: API validation** — **landed**, 4 commits, 858 tests green. Deviations: a `NullNanJSONResponse`
was needed (settings legitimately carry NaN and JSON has no NaN, so the routes match the event contract's
null); the WebSocket handler watches the client socket as well as the hub, without which a disconnect was
only noticed at shutdown; the sidecar binds its own socket before serving so the handshake can name an
OS-chosen port.

**Status: step 1 is complete.** `docs/design/tauri_backend_split_status.md` has the summary.
- **PR-50a (Add):** `api/app.py` factory + token auth + session routes + API CI job.
- **PR-50b (Add):** settings/profiles/users/instruments routes with 409 guards.
- **PR-51a (Add):** WS stream + `event_hub.py` hand-off and drop policy (no replay).
- **PR-51b (Add):** ring, `(run_id, since_seq)` resume, `gap`, `GET /session/events` pull.
- **PR-51c (Add):** `api/__main__.py` entry point, handshake, stderr logging, stdin-EOF watchdog,
  `POST /shutdown`.

**Deferred to step 2:** aux preview port ownership; cable-null procedure + prompt; 4PP spot model and
Export Summary; Results Viewer endpoint; profile-file I/O incl. µm/cm migration; engineering-notation
helpers out of `ui/widgets.py`; PyInstaller sidecar spec (with `excludes=['PySide6','shiboken6']` and the
startup `:OUTP OFF` check); the 4PP table's epoch "elapsed" (MW:2483-2484 — display-only, unlike the
divergence PR-33 fixes);
source-aware sweep bounds; sweep/vdP file tag (W:1171-1177); duplicate path sanitizers (W:1122-1136 vs
W:1322-1328).

---

## 10. Reviewability

### 10.1 Module layout and size targets

Hard ceiling: **400 lines per new module**. If a move would exceed it, it splits along an existing
`if self.mode ==` boundary; it is not an occasion for new abstractions.

| Module | Responsibility | Target size | Comes from |
|---|---|---|---|
| `schema/settings_common.py` | Instrument/File/Output/Aux/Safety/Display models | ~200 | `constants.py:12-156` |
| `schema/settings_modes.py` | Six mode models + `RunRequest` | ~280 | §4.2 |
| `schema/resolve.py` | `resolve_run_settings`, `ResolvedRun`, `Issue` | ~180 | MW:2014-2049, `safety.py`, `timing.py` |
| `session/events.py` | Envelope + one payload model per event | ~300 (grows per PR) | §3.4 |
| `session/emitter.py` | `EventEmitter`, `ListSink` | ~80 | new |
| `session/control.py` | `RunControl`, `PendingPrompt`, `RunStopped` | ~130 | W:63, W:93-133, W:1366-1396 |
| `session/configure.py` | Per-mode SCPI configure → frozen dataclasses | ~300 (split `configure_fpp.py` if > 400) | W:202-445 |
| `session/samples.py` | Reading parse + CSV row per mode | ~300 (split `samples_fpp.py` if > 400) | W:706-829, W:892-1028 |
| `session/delta.py` | Delta-mode read | ~70 | W:1263-1316 |
| `session/run_files.py` | Base path, sanitizer, exporter creation | ~120 | W:483-515, W:1122-1179 |
| `session/continuous_run.py` | `ContinuousRun`: connect, aux open, sweep, loop, compliance/overpower block, health check, shutdown | ~400 (sweep → `session/sweep.py` if over) | W:135-200, W:450-700, W:830-890, W:1030-1261 |
| `session/vdp_run.py` | `VdpRun`: configure, geometry loop, result | ~380 | W:1331-1696 |
| `session/manager.py` | `MeasurementSession` | ~250 | MW:2165-2263 (checks only) |
| `workers.py` (after) | QThread adapters + event→Signal tables | ~240 | W (shrinks from 1696) |
| `api/app.py` | App factory, token dependency | ~80 | new |
| `api/routes_session.py` | Session commands, prompts, events pull, shutdown | ~170 | new |
| `api/routes_settings.py` | Users, profiles, resolve, schema, instruments | ~170 | new |
| `api/event_hub.py` | Ring, hand-off drain, per-client drop policy | ~160 | new |
| `api/events_ws.py` | WS endpoint | ~80 | new |
| `api/__main__.py` | Entry point, handshake, watchdog, logging | ~140 | `__main__.py:36-92` flags |

`main_window.py` only shrinks in step 1: PR-15 removes about 35 lines (the non-widget logic at
MW:2014-2049; widget reads MW:1950-2012 stay). No `api.py` or session god object is introduced.
`MeasurementSession` never contains procedure code; a procedure never contains thread or HTTP code.

### 10.2 One source of truth for contracts
- **Pydantic models are the contract:** `schema/settings_*.py` and `session/events.py`. Defaults come from
  `DEFAULT_SETTINGS` by reference (§4.1).
- **`tools/export_contracts.py`** writes `contracts/settings.schema.json` and
  `contracts/events.schema.json` with stable key ordering; committed, and `test_contracts_up_to_date.py`
  fails when stale. Every contract change is a JSON diff next to the Python diff.
- **TypeScript types are generated from those JSON files in step 2** (`json-schema-to-typescript`), never
  hand-edited; generated files are marked and excluded from review line counts.

### 10.3 Style rules
- Plain `threading.Thread`/`Event`/`Lock`. No asyncio outside `api/`, no executors, no QThread outside
  `workers.py`/`ui/`.
- Pydantic only for external contracts. Internal state uses `@dataclass`, as in `sensors.py`.
- No metaprogramming: no decorator registries, no `getattr`-string dispatch, no dynamic model generation.
  The event→Signal adapter is an explicit `if/elif` chain.
- Match existing style: module docstring explaining *why*, comment density of `workers.py`/`sensors.py`,
  `logger = logging.getLogger(__name__)`.
- Python 3.9 syntax: `Optional[X]`, `List[X]`.
- Prefer duplication over premature abstraction: `ContinuousRun` and `VdpRun` share no base class.
- No new `except Exception: pass`. The four existing defensive blocks around emits (W:166-169, W:843-847,
  W:867-870, W:1445-1448) move unchanged.
- **Extraction pattern (Phase 2):** method first (indentation-only diff, `self.` kept, emits and early
  returns kept; the only non-move line is the call site), then data-flow change as its own PR, then
  method → function as a mechanical PR, then the file move. Never two of these in one PR.
- Review commands in every PR description. Moves:
  `git diff --color-moved=dimmed-zebra --color-moved-ws=allow-indentation-change main...HEAD`.
  Mechanical: `git diff --word-diff`.

### 10.4 Per-PR review sheet

"Changed lines" excludes pure moves (in parentheses) and generated JSON.

| PR | Scope | Est. changed (moved) | Kind | Reviewer checklist |
|---|---|---|---|---|
| **00** | `tests/test_gui_smoke.py` fixture patches the `ConfigManager` name `main_window` imports and passes the tmp config path | ~14 test | Tests | Full suite leaves the working `config.json` byte-identical (verified by md5 before/after) |
| **01** | MW:2106: `m_save = dict(self.user_settings['measurement']); m_save.pop('res_cable_null', None); self.config_manager.update_user_settings(self.current_user, {'measurement': m_save})`; test | ~8 + 50 test | **Behavior**: silence persists, as its UI text promises | Only the `measurement` section is passed; test asserts the flag persists across `ConfigManager` reload **and** `res_cable_null` is absent on disk; tmp config path explicit |
| **02** | Add `'output': dict(self.user_settings.get('output', {}))` at MW:1940-1944; one-time `output` reset per profile guarded by a `migrations` entry (D6); test | ~3 + 35 + 60 test | **Behavior**: Settings ▸ Output takes effect (`data_export.py:901-915`), and stale pre-1.13 choices are reset once | Migration runs once (second load is a no-op, asserted); reset touches only `output`; CSV default path byte-identical; release note drafted in the PR body |
| **03** | Move `_confirm_voltage_safety` after gather, pass the gathered dict to `is_potentially_hazardous`; `_running_status_message(mode, settings)` uses the same dict (MW:2059-2072, callers MW:2202, MW:1325) | ~20 + 40 test | **Behavior**: warns on the value actually used; modal and status bar agree | Order: sample name → gather → safety → start; gather `ValueError` still shows the settings error; vdP same |
| **04** | Delta read (W:651-667) wrapped in the same `for retry in range(max_retries)` loop as the non-delta path, with the existing backoff and `*CLS`; test | ~25 + 60 test | **Behavior**: a transient delta read error retries instead of ending the run | Fake raising once then succeeding → run continues, one `status_update`; raising `max_retries` times → `error_occurred` then stop; non-delta path byte-identical; backoff site W:686 reused, not duplicated |
| **05** | `mark_event` appends under `_state_lock`; `get_and_clear_event_marker` returns `'; '.join(pending)` and clears (W:128-133, W:1181-1182); test | ~15 + 40 test | **Behavior**: marks arriving between samples all reach the row that follows | Two marks before one sample → one row, `'a; b'`; no mark → `''` (CSV header/row tests unchanged); status text per mark unchanged; lock never held across an emit |
| **10** | `pydantic>=2`; `schema/settings_common.py`; `test_schema_no_qt.py`; `--self-test` in `__main__.py`; build.yml smoke + `workflow_dispatch` | ~220 | Add | Defaults by `DEFAULT_SETTINGS[...]` reference; no `__future__` annotations; `extra='allow'`; groups under `measurement` documented; dispatch build green with self-test |
| **11a** | resistance, source-V/I, sweep, vdP models; bounds test | ~190 + 60 test | Add | Field names == keys; bounds test green; strict-only vdP thickness rule |
| **11b** | 4PP model, worst-case power rule, `RunRequest`, strict override-key checks; bounds test | ~170 + 60 test | Add | 15 fields; strict-only rules are strict-only; profile-owned keys listed |
| **12** | Goldens (captured from current `gather_settings_for_mode`), `schema/resolve.py`, resolver tests | ~180 + 200 test (+ JSON) | Add | Goldens generated by a committed script run against pre-PR code; resolver fed the same widget values matches every golden; steps in §4.3 order with citations; no widget imports in `schema/` |
| **13** | Envelope, `log`/`error` payloads, `EventEmitter`, `ListSink`, `test_event_models.py` | ~160 | Add | `seq` plain counter; NaN → null on wire; sink called with no lock held; no payload without an emitter in view |
| **14** | Export tool + `contracts/*.json` + drift test | ~120 (+ generated) | Add | Deterministic (run twice, no diff); failure message names the regenerate command |
| **15** | `gather_settings_for_mode` builds `overrides` from widgets and returns `resolve_run_settings(...).settings` | ~35 removed, ~25 added | Refactor | `test_gather_golden.py` and `TestGatherSettings` unchanged and green; widget reads remain; dispatch build green |
| **20** | 81 emit sites `self.X.emit(` → `self._out.X(`; `_QtOutputs` (~40 lines) | ~120 | Mech | Word-diff shows only that substitution; PR body lists all 81 sites (`grep -n '\.emit('` before/after both 81) |
| **21** | `RunControl` with exactly `_lock`, `running`, `paused`, `event_marker`, `get_and_clear_event_marker()`, `proceed_event`; worker properties delegate | ~110 | Refactor | Same bool semantics; `running = True` on entry kept (W:136); marker queue as landed in PR-05 (W:128-133); `clear` before `geometry_ready` (W:1546); stop sets `proceed_event` (W:1396); no lock around I/O; `test_session_no_qt.py` added |
| **22a** | Each configure branch (W:202-445) → `self._configure_<mode>(measurement_settings, nplc)` | ~25 (245) | Move-in-place | Indentation-only; `self.` and emits untouched; the only non-move lines are call sites and `if not self._configure_four_point(...): return` (W:365) / sweep equivalent (W:444) |
| **22b** | `_cable_null`, 7 `_fpp_*`, 4 `_sweep_*` → one frozen dataclass per mode returned by the configure method; 17 read sites updated | ~70 | Refactor | Each of the 17 read sites listed in the PR body; `test_workers.py` `command_log` unchanged |
| **22c** | Configure methods → module-level functions `(keithley, out, settings, ...)` | ~60 | Mech | Word-diff shows only `self.keithley`→`keithley`, `self._out`→`out`, signature lines |
| **23a** | Aux open (W:450-476), exporter open (W:483-515), sweep (W:521-600) → private methods | ~20 (150) | Move-in-place | Aux still opens *before* exporter (W:483-496); `file_ready`/`instrument_ready` flags kept; early returns become `if not ...: return` |
| **23b** | Base path + exporter creation → `run_files` functions returning `(exporter, filename)` | ~40 | Mech | `self.exporter`/`self.filename` assigned at the call site only |
| **24a** | Parse W:706-829 → `_parse_<mode>(reading, ...)` returning `(data_dict, compliance_status, compliance_type)` | ~30 (125) | Move-in-place | Cable-null subtraction (W:736-738) and `_last_delta` use unchanged; no emits inside |
| **24b** | Row build W:892-1028 → `_build_row(...)` | ~25 (135) | Move-in-place | Column order per mode unchanged (CSV header tests); F84 selection verbatim; aux splice (W:1026-1027) unchanged |
| **24c** | 24a/24b methods → functions with explicit params | ~50 | Mech | Word-diff only; W:830-890 (aux read, compliance/overpower/marker) untouched in the loop |
| **25a** | Module-level functions → `session/configure.py`, `samples.py`, `delta.py`, `run_files.py` | ~30 (700) | Move | `--color-moved` shows only import changes; no Qt import in `session/` |
| **25b-1** | `class ContinuousRun` in `workers.py` holds `run()` body + methods; `MeasurementWorker` composes it and forwards control methods | ~90 (indent 600) | Refactor | Adapter keeps constructor, Signals, `filename`, control methods, GUI-thread emits of pause/resume/stop; `finally` → `_cleanup` → `running=False` (W:1118-1120); bench `tools/bench_v2_export.py` |
| **25b-2** | `ContinuousRun` → `session/continuous_run.py` | ~10 (400) | Move | Imports only |
| **26-1** | `class VdpRun` in `workers.py`; `VdpMeasurementWorker` composes it | ~70 (indent 340) | Refactor | `running=False` before `_cleanup` (W:1425-1426); `vdp_complete` before finalize (W:1667 vs W:1675) |
| **26-2** | `VdpRun` → `session/vdp_run.py` | ~10 (380) | Move | Imports only |
| **30a** | Data events + payload models + regenerated JSON; `_QtOutputs` branches for them; `test_qt_sink.py` | ~180 + 60 test | Refactor | `instrument_connected` at W:167 position; `line_frequency` after LFR; `values` uncoerced; `acquisition_finished` exactly where `measurement_complete` was |
| **30b** | `log`/`error` at every status/error site | ~150 | Refactor | PR body table `line → event → level/code/source/fatal` for every site; message text identical |
| **31a** | vdP log/error/compliance/geometry_complete/result | ~90 | Refactor | `geometry_complete`/`vdp_complete` dicts reconstructed field-for-field |
| **31b** | `vdp_geometry` prompt; `RunControl` prompt fields; `proceed()` answers | ~90 | Refactor | `geometry_ready` payload field-for-field; stale click dropped; stop releases wait |
| **32a** | `run_started`, `aux_connected`, `file_opened`, `file_finalized`, `paused/resumed/stopping`, `prompt_resolved` | ~120 | Add | No Qt-visible change (sink ignores them); `stopping` emitted from the acquisition thread only |
| **32b** | `control.finish(reason)` at W:599, 850, 880, 1045, 1055, 1091; `stop()` → `finish('user_stop')`; `run_ended` from `finally`; `test_run_events.py` | ~100 + 200 test | Add | Every exit path in the §8 list covered, incl. delta retries-exhausted (W:699, post PR-04) and W:1117; first-writer-wins stated |
| **32c** | Row builder also returns derived values; `sample.derived/delta`; test vs CSV | ~70 + 80 test | Add | Row content unchanged (CSV tests); `derived.method` matches F84 selection (W:918-920) |
| **33** | 4PP panel consumes `sample.derived` (adapter re-emits it on a new `sample_derived(dict)` Signal); MW:2458-2482 recompute deleted | ~60 + 60 test | **Behavior**: GUI Rs/ρ/σ equal the CSV row (F84 *and* legacy configs) | Test asserts panel values == CSV row for both configs; no calculation left in `update_data`; epoch "elapsed" explicitly out of scope |
| **40** | `session/manager.py` + `tests/test_session.py` | ~250 + 200 test | Add | State transitions total; `identify` ⇄ `start` exclusion; join on `close()`; inhibitor on run thread; no procedure logic |
| **41** | `safety_voltage_ack` on the run thread before connect; `cancelled`; flag-only silence save (same pattern as PR-01) | ~110 + 120 test | Add | Prompt after `run_started`, before any VISA open; `cancel` → `run_ended{cancelled}`, no file; `res_cable_null` not persisted |
| **42a** | `control.sleep` raising `RunStopped` at W:186, 609, 686, 1279, 1287, 1459, 1569, 1573; `stopped()` checks between retries and vdP averaged reads; pre-entry stop honored | ~70 + 100 test | **Behavior**: stop takes effect during settle/backoff; the interrupted read and row are skipped; PySide6 stop also becomes responsive | Every site listed; W:1087 and W:640 unchanged; output-off + finalize still run; bench stop-during-settle on the 2420 |
| **42b** | `abort()` | ~40 + 40 test | Add | Cancels pending prompt; same shutdown path |
| **42c** | `prompt_timeout_s` on `RunRequest`; watchdog in `MeasurementSession` only | ~50 + 60 test | Add (headless-only; PySide6 path untouched) | Timeout takes the `abort()` path: output off, file finalized, `run_ended{prompt_timeout}`; a prompt answered in time cancels the watchdog; Qt path asserted to have no timeout |
| **43** | `tests/test_session_e2e.py` | ~350 test | Tests | Each ported assertion mapped to its `test_e2e_simulator.py` line in the PR body; spot test deferral stated |
| **44** | `ConfigManager` lock + temp-file `os.replace` | ~40 + 60 test | **Behavior** (write mechanics only; format unchanged) | Lock never held across anything but mutate+save; Windows `os.replace` semantics |
| **45** | `session/instrument_lock.py`: `fcntl.flock` / `msvcrt.locking` on `~/.resistamet/locks/<sanitized address>.lock` (one place per machine, not beside the config: two processes only exclude each other if they agree on where the lock is — the first version keyed it to the config directory and the bench found two backends never contending), taken by the session before `start` answers so a refusal is synchronous, held to after `_cleanup`, for the SMU address and the aux port; acquired in the shared run procedures, so PySide6 and the session both get it | ~120 + 100 test | **Behavior**: a second process is refused with `error{code: instrument_busy}` instead of colliding on the bus | No stale-lock logic (the OS releases on process death — stated and tested by killing a child); lock acquired *before* any VISA open and released after output-off; two-process test on the simulator; identify/Test Connection also guarded |
| **46** | `RunControl` accumulates paused time; duration check subtracts it (W:641, W:1089) | ~30 + 50 test | **Behavior**: a run paused across its deadline resumes and runs its remaining time | `elapsed_s`/`t_unix`/CSV elapsed unchanged (asserted); pause → wait → resume → run continues; `duration` of 0 (unlimited) unaffected |
| **50a** | `api/app.py`, `routes_session.py`, token auth, API CI job, `test_session_no_qt` extended to `api` | ~220 + 150 test | Add | `pytest -rs` shows API tests ran; handlers are one-liners; binds 127.0.0.1 |
| **50b** | `routes_settings.py` (users, profiles, resolve, schema, instruments) | ~170 + 120 test | Add | 409 guards: identify/resources unless idle, `gpib_address` patch during run |
| **51a** | `api/event_hub.py` + `api/events_ws.py` (no replay); compliance-transition filter (D2) | ~200 + 120 test | Add | One pending drain max; reserved capacity; stalled-client test; OK/V_COMP/OK/V_COMP stream → two forwarded `compliance` events, `sample.compliance` untouched |
| **51b** | Ring, `(run_id, since_seq)`, `gap`, `GET /session/events` | ~120 + 100 test | Add | Wrong `run_id` → `gap`; replay in order |
| **51c** | `api/__main__.py`, handshake `print(..., flush=True)`, stderr logging, stdin-EOF watchdog, `POST /shutdown` | ~160 + 80 test | Add | Subprocess test reads the handshake through a pipe; logs never on stdout (`logging_config.py:68` writes stdout today); shutdown order per §7.5 |

---

## 11. Risks & decisions

| Risk | Severity | Mitigation |
|---|---|---|
| Phase 2 extraction reorders per-sample side effects (compliance stop, overpower `:OUTP OFF`, marker consume) | High | That block (W:830-890) is never extracted; parse and row build go in separate move-in-place PRs (24a/b); `test_workers.py` asserts `command_log` and CSV rows; bench after PR-25b-1 |
| Hard kill of the sidecar mid-VISA leaves output **on** and ports held | High | Graceful order with a 35 s grace period (§7.5); stdin-EOF watchdog (PR-51c); accepted residual risk, UI warning, startup `:OUTP OFF` check in step 3 |
| **Cross-process instrument contention:** PySide6 app and sidecar both open GPIB0::24 / the aux COM port during the transition; the one-run rule is per process | High | **PR-45** takes a per-address OS file lock in the shared run procedures, so both processes are covered (D8); release notes keep the operator rule as belt-and-braces |
| **Frozen PySide6 exe breaks on pydantic_core** while CI stays green (`build.yml` tag-only, `--version` smoke skips Qt; no pydantic in `resistamet-gui.spec:27-43`) | High | PR-10 `--self-test` in build.yml + `workflow_dispatch` builds for PR-10 and PR-15 |
| **PyInstaller sidecar + VISA on Windows.** No backend pinned (`instrument.py:162`); NI-VISA not bundled (`resistamet-gui.spec:12-14`); `pyserial` missing from `requirements.txt` (in `pyproject.toml:58`); no `serial`/uvicorn hiddenimports; the spec lists `PySide6`/`shiboken6` as hiddenimports (`:35-36`) | High | Step 3: one-dir sidecar spec with `excludes=['PySide6','shiboken6']`, explicit hiddenimports, `--visa-library`, `/health` reports backend; `test_session_no_qt` covers `api`; verify on the lab machine over `ssh resistamet` |
| Relative `config.json` / `measurement_data` resolve against the sidecar's cwd (`constants.py:9`, `:139`) | High (data safety) | `--config` and `--data-dir` required; `/health` reports absolute paths |
| Concurrent config writes corrupt `config.json` (`config.py:134-137`, no lock) | Med | PR-44 before any API route |
| Windows `SetThreadExecutionState` is per-thread (`system_utils.py:132-142`) | Med | Inhibit/uninhibit on the acquisition thread; asserted in `test_session.py` |
| Durability weaker than documented: fsync every `auto_save_interval` (W:1057-1063), not per row (`data_export.py:452-453`) | Med | Unchanged; documented in the API |
| Same-second filename collision with scripted runs (W:1172, W:1505; `'w'` open at `data_export.py:497`) | Med | One run at a time; collision suffix as a named follow-up before MCP |
| Local API drivable by any local process | Med | Per-launch bearer token + 127.0.0.1 |
| Simulator patch is process-wide (`simulator.py:57`) | Low | Sidecar chooses at launch; headless tests use `fake_rm` |

**Decisions (2026-09-17).** The ten open questions in rev 2 were answered; each now has a PR in §9.

| # | Question | Decision | Where |
|---|---|---|---|
| **D1** | Pause semantics | Output stays **on** (unchanged); the duration limit counts **active** time only | PR-46, §3.3 |
| **D2** | Compliance event rate | Run procedure keeps per-sample emits (Qt unchanged); the API hub forwards **transitions** only | PR-51a, §3.4 |
| **D3** | Prompt timeout | Session runs abort after `prompt_timeout_s` (default 900 s) with output off and the file finalized; the PySide6 path never times out | PR-42c, §3.5 |
| **D4** | Who answers `requires_human` prompts | **UI credential only**; any other role gets 403. No pre-answering — `RunRequest.acknowledge` is dropped | §3.5, §7.4, PR-41 |
| **D5** | Mark events | **Queue** them; all marks pending at the next sample are joined with `; ` | PR-05 |
| **D6** | Profiles carrying long-dead `output` settings | Reset every profile's `output` to the CSV defaults **once**, recorded in an additive top-level `migrations` list | PR-02 |
| **D7** | 4PP GUI/CSV divergence | Fix it in PySide6 now, via `sample.derived` — it is a data-integrity bug, not a cosmetic one | PR-33 |
| **D8** | Cross-process instrument contention | Per-address **OS file lock** in the shared run procedures (no stale-lock logic; the OS releases it on death) | PR-45 |
| **D9** | 4PP delta single-read failure | Fix in Phase 0 so goldens and event tests never encode it | PR-04 |
| **D10** | Where generated contracts live | `contracts/` at the repo root — the Tauri app will be a sibling of the Python package, not inside it | PR-14, §10.2 |

**Consequences for the plan's shape.** Phase 0 grows from 3 to 5 PRs (all small, all independent), Phase 3
gains PR-33, and Phase 4 gains PR-42c, PR-45 and PR-46. Total: **5 named behavior-change PRs outside
Phase 0** (33, 42a, 44, 45, 46), each isolated and individually revertible.

**Still open (deliberately, not needed for step 1):** the second credential itself (v2.0 mints the `mcp`
token; D4 only fixes the rule), and whether the Tauri app lives in this repo or a sibling one — D10 assumes
this repo but only affects one path in PR-14.

# Tauri Backend Split, Step 1 — Status

**Status:** Complete on `phase0/reviewable-baseline` (65 commits, not pushed)
**Date:** 2026-09-17
**Design:** `tauri_backend_split.md`

Step 1 of the Tauri migration is done: the measurement layer is Qt-free, typed,
and drivable headlessly, and the PySide6 app runs on it unchanged.

## What exists now

```
resistamet_gui/
├── schema/          settings contract (pydantic) + resolver
├── session/         the run layer: control, events, procedures, session
├── api/             localhost HTTP + WebSocket over a session
├── workers.py       221 lines: two QThread adapters + the event->Signal sink
└── ui/              unchanged except where a behaviour fix was needed
```

`workers.py` went from 1,696 lines to 221. Nothing in `session/` imports Qt,
which `test_session_no_qt` enforces for `session` and `api` both.

## How to drive it without a GUI

```bash
python -m resistamet_gui.api --port 0 --simulate --config config.json
# {"url": "http://127.0.0.1:53124", "token": "...", "pid": 41234}
```

Then `POST /session/start`, watch `/session/events/ws`, `POST /session/stop`.
The same procedures run under the GUI's QThread adapters.

## Behaviour changes that shipped

Each is its own commit; none changes CSV or HDF5 content.

| Change | Why |
|---|---|
| Safety warning's "don't show again" persists | it called a method that does not exist, and the error was swallowed |
| Output settings reach runs (+ one-time reset) | format/compression were saved but never applied |
| Safety check uses the gathered settings | a tab edited after the last save started with no warning |
| 4PP delta read retries | one transient failure ended the run, silently |
| Event marks queue | a second mark before the next sample replaced the first |
| 4PP panel shows the worker's numbers | the panel recomputed without the F84 corrections the CSV had |
| Stop interrupts the settling wait | a 10 s settle meant a 10 s stop, plus an unsettled sample |
| Duration counts measuring time | a run paused past its deadline ended on resume |
| config.json written atomically, under a lock | truncate-in-place could lose every profile |
| One process per instrument address | two processes could interleave SCPI on one bus |

## What step 1 deliberately does not include

No React or Tauri code, no PyInstaller sidecar spec, no MCP server. The API
exists to prove the session shape, not to ship.

Still open, and listed in the design doc: the `mcp` credential itself (D4 fixes
the rule, v2.0 mints the token), persisting the safety-silence flag from a
headless client, and the step-2 items — Results Viewer, 4PP spot model, cable
null, profile file I/O.

## Next

1. **Bench-verify on the 2420.** Everything here passed against the simulator
   and the fake; the SCPI order is unchanged but the stop path, the instrument
   lock and the sidecar's shutdown deserve real hardware.
2. **Step 2:** generate TypeScript types from `contracts/`, build the Tauri UI
   one tab at a time against this API.
3. **Step 3:** the PyInstaller sidecar spec, with the Windows VISA questions
   the design doc lists as risks.

# Tauri Desktop UI — Status (steps 2 and 3)

**Status:** First complete pass on `phase0/reviewable-baseline`, not pushed
**Date:** 2026-09-17
**Depends on:** step 1 (`tauri_backend_split.md`, `tauri_backend_split_status.md`)

Every measurement mode the PySide6 app has is driveable from the Tauri UI,
against the same Python backend, through the same API the MCP layer will use.
The frozen backend ships inside the app bundle and the shell launches it.

## What exists

```
desktop/
├── src-tauri/           Rust shell: spawns the backend, reads its handshake,
│                        hands url+token to the webview, shuts it down cleanly
├── src/                 React 19 + TypeScript (strict), uPlot for plots
│   ├── generated/       types + field metadata from contracts/*.schema.json
│   ├── lib/             api client, resuming WebSocket, engineering formatting
│   ├── state/           session / samples / ui / overrides / sweep / vdp / spots
│   ├── components/      shell, primitives, plots, dialogs, settings form
│   └── views/           continuous (R, source V, source I, 4PP), sweep, vdP, results
└── sidecar/             frozen backend (PyInstaller one-dir), bundled as a resource
```

| View | Has |
|---|---|
| Resistance / Source V / Source I | live plot with time window, large readout, Start/Pause/Stop/Mark (M), settings resolved by the backend as you type, hazard and rate warnings |
| Four-point probe | the above plus Rs/ρ/σ readout from the backend's derived values, per-spot statistics, a spot table with spread across spots |
| I-V sweep | curve per direction, point count, least-squares R and R² |
| van der Pauw | four-step wizard with the contact diagram coloured per wiring, readings table, result with f(Q) verdict and combined uncertainties |
| Results | file list (newest first, per operator), CSV preview with metadata and any column vs time |
| Dialogs | operator picker (blocks until chosen), profile settings incl. instrument scan/identify, blocking safety prompt |

Design: dark by default, tokens in one file, Wong data colours shared with
PySide6, rail collapses to icons below 1280 px.

## Verified

- Every view exercised in a browser against `--simulate`: a resistance run
  start to stop, the 60 V safety prompt (acknowledge and cancel), a full
  four-geometry vdP with result, results preview.
- The Tauri shell in dev mode spawned the **frozen** backend from the bundle's
  resource directory and the webview connected to it (log: `backend via
  Executable(.../resistamet-api)`, WebSocket accepted). Handshake ≈ 1 s.
- `npm run build`, `npm run check:types`, `cargo check`, and the full Python
  suite (872 tests) green.

## Not yet

- **Hardware.** Nothing here has touched the 2420. The backend's stop path,
  instrument lock and sidecar shutdown are the things to watch on the bench.
- **Windows build.** `desktop.yml` is written, not yet run. Expect the first
  run to surface PyInstaller hidden-import gaps (pyvisa backends, h5py) — the
  workflow smoke-tests the frozen backend before building the shell so the
  failure is legible.
- **Backend items step 2 depends on** but works around for now: the 4PP spot
  model (spots are summarised in the UI), cable null (not in the new UI yet),
  persisting the safety-silence flag from a headless client, `SessionStatus`
  in the exported contract (hand-typed in `lib/api.ts`).
- PySide6 remains the shipping UI until the above is closed and the bench
  check passes.

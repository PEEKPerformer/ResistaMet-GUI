# Tauri Desktop UI — Status (steps 2 and 3)

**Status:** On `phase0/reviewable-baseline`, pushed, CI green
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

## CI (2026-09-18)

`desktop.yml` and `test.yml` are green on the branch. The first runs found
three platform assumptions, all fixed: a CRLF checkout read as a stale
contract; the results route returned `alice\file.csv` on Windows; and WiX
refuses a non-numeric pre-release identifier (`2.0.0-dev` → `2.0.0-1`). They
also caught a real regression — a stop landing in a settling wait was reported
as an output fault, which opened a modal in the PySide6 app and hung the Linux
e2e job — now fixed with tests. Windows produces msi and nsis installers with
the frozen backend inside; macOS a dmg. The frozen backend's handshake and
`/health` are exercised on both before the shell is built.

## GPIB on a Mac (2026-09-18)

NI's last macOS GPIB driver (NI-488.2 21.5.1, 2022) does not load on macOS
13+, so a Mac with NI-VISA installed sees serial and USB but no GPIB. Two
things landed for that:

- **A per-machine VISA backend choice** (`visa_library`: automatic, vendor
  VISA, pyvisa-py) in Settings ▸ Instrument, next to the address. Scan and
  Identify use the selection before it is saved and say which
  implementation answered. Every ResourceManager the app opens goes through
  `visa_backend.resource_manager`.
- **A user-space driver for the NI GPIB-USB-HS family** over libusb
  (`resistamet_gui/gpib_usb`), registered as a pyvisa-py session, so
  `GPIB0::24::INSTR` works on a Mac through pyvisa-py. MIT, written from a
  facts-only protocol specification by an agent that never saw the GPL
  drivers; see `ni_usb_gpib_clean_room.md`. The frozen macOS backend
  bundles libusb.

## Not yet

- **Hardware.** Nothing here has touched the 2420. The backend's stop path,
  instrument lock and sidecar shutdown are the things to watch on the bench.
- **The NI USB driver on a real adapter.** Proven only against scripted
  fakes. Needs the GPIB-USB-HS from laptop2 on a Mac with a 2400: attach,
  `*IDN?`, a resistance run, a stop mid-settle, Scan while idle.
- **Installing the Windows build on the lab PC** and confirming pyvisa finds
  NI-VISA from inside the frozen backend.
- **Prologix / AR488 adapters** need their `PRLGX-ASRL::…::INTFC` resource
  opened before the instrument address resolves; the app does not do that.
- **Backend items step 2 depends on** but works around for now: the 4PP spot
  model (spots are summarised in the UI), cable null (not in the new UI yet),
  persisting the safety-silence flag from a headless client, `SessionStatus`
  in the exported contract (hand-typed in `lib/api.ts`).
- PySide6 remains the shipping UI until the above is closed and the bench
  check passes.

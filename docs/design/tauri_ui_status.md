# Tauri Desktop UI — Status (steps 2 and 3)

**Status:** On `phase0/reviewable-baseline`, pushed, CI green; backend bench-checked on the lab 2420 and on a 2400 over the NI USB driver; UI bench report's three blockers and five majors fixed (see `tauri_ui_bench_2026-09-18.md`)
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
  bundles libusb, and CI asserts that the bundled copy loads from inside
  the bundle.
- **`resistamet-api --check-visa`**, which prints the resolved VISA
  implementation, its version, and whether the NI USB driver found libusb,
  as one JSON line. This is the diagnostic for "the app sees no
  instruments" on a PC with no development tools — the failure that cost an
  afternoon on the lab laptop was a VISA library present with no GPIB
  driver behind it.

## Bench, lab desktop, 2026-09-18

The frozen Windows backend from CI run 35380847071, extracted from the msi
into a scratch folder (nothing installed), driven over an SSH tunnel against
the Keithley 2420 (serial 1230523) with a ~10 Ω DUT. The shipping PySide6 GUI
was open on the PC but idle throughout.

| Check | Result |
|---|---|
| `--check-visa bus` from inside the bundle | NI-VISA 22.5 via `visa32.dll`; `GPIB0::24::INSTR` enumerated; NI USB driver correctly unavailable on Windows |
| Identify | IDN in 1.2 s, model 2420 with 60 V / 3.05 A / 22 W limits |
| Resistance run, defaults, 10 s | 29 samples, 10.081 Ω, spread 0.015 %, `run_ended user_stop ok`, stop latency 0.48 s |
| Stop during a 6 s settle | `run_ended user_stop ok samples=0`, no error event, stop latency 0.15 s |
| Start immediately after that stop | accepted; the lock's grace covered the previous run's cleanup |
| `/session/shutdown` mid-run | server stopped, file finalized with footer (103 rows), sleep re-enabled, then `OUTP?` = 0 asked directly over NI-VISA |

One observation, not a regression: with **Auto-range on, the 2420's
auto-ohms chooses its own test current** (100 mA for a 10 Ω DUT on the 20 Ω
range) and the profile's 1 mA "test current" is not what flows. The SCPI is
identical in the shipping worker, so this is how the lab has always run
resistance mode; the UI should say so, or grey the field out, when
Auto-range is on.

## Not yet

- **The Tauri UI itself on the lab PC.** The backend paths above are
  proven; the installer has not been run on the PC and the React UI has
  not driven the 2420 yet.
- **The NI USB driver on a real adapter: done 2026-09-18.** GPIB-USB-HS
  01CEE482 with a Keithley 2400 on this Mac: the first contact found the
  read reply's tail shorter than the specification derived and an
  instrument-side race after take control that wedged the adapter; both
  fixed on the bench (`bc74095`, `beb1db1`). Three consecutive resistance
  runs, stop mid-settle, immediate restart and shutdown mid-run all pass
  through the driver; compliance detection verified on it too.
- **Prologix / AR488 adapters** need their `PRLGX-ASRL::…::INTFC` resource
  opened before the instrument address resolves; the app does not do that.
- **Backend items step 2 depends on** but works around for now: the 4PP spot
  model (spots are summarised in the UI), cable null (not in the new UI yet),
  persisting the safety-silence flag from a headless client, `SessionStatus`
  in the exported contract (hand-typed in `lib/api.ts`).
- PySide6 remains the shipping UI until the above is closed and the bench
  check passes.

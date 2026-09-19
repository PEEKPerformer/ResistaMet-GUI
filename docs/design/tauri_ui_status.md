# Tauri Desktop UI — Status (steps 2 and 3)

**Status:** On `phase0/reviewable-baseline`, pushed, CI green; backend bench-checked on the lab 2420 and on a 2400 over the NI USB driver; the UI bench's three blockers and five majors fixed
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
- **A per-machine GPIB interface** (`gpib_interface`, 2026-09-19) for
  Prologix-style adapters, under the backend choice. pyvisa-py sends
  `GPIB<n>::<addr>::INSTR` to such an adapter only while its
  `PRLGX-ASRL<n>::<device>::INTFC` or `PRLGX-TCPIP<n>::<host>::INTFC`
  resource is open, so `visa_backend.resource_manager` opens it on the
  manager and keeps it there (pyvisa holds sessions weakly; an unreferenced
  interface closes at once). Ignored with a warning under a vendor library.
  Scan, Identify and `--check-visa bus` report whether it opened.
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

## The driver on a real adapter, 2026-09-18

GPIB-USB-HS 01CEE482 with a Keithley 2400 (serial 1175680) at primary
address 3 on this Mac. First contact found two things the specification had
wrong — the read reply's tail is 16 bytes, not 28, and the 2400 drops
addressing bytes that arrive within a millisecond of take control, which
wedged the adapter until it was power-cycled — both fixed on the bench
(`bc74095`, `beb1db1`). Through the driver: identify, three consecutive
resistance runs (99.51 Ω at 10 mA), stop mid-settle, immediate restart,
shutdown mid-run with the output confirmed off, and the compliance
detection of `1568506`.

## NI's driver as the oracle, 2026-09-19

With the adapter off the Mac for the weekend, the lab desktop's NI-488.2
stack drove its own GPIB-USB-HS under USBPcap: 22 one-operation captures
(`docs/design/captures/ni_usb_gpib_2026-09-19/`), decoded into §10 of the
protocol specification by a fresh Reader agent. They corrected the
specification in several places — reads over 1 KB and long writes use two
instructions the GPL-derived spec did not know, with the data raw on the
adapter's second bulk endpoint pair; serial poll is its own instruction;
the timeout table was right. The implementer built those paths from §10
alone, with the framed paths kept and a one-flag switch back to them
(`RESISTAMET_GPIB_RAW_TRANSFERS=0`); the `GPIB0::INTFC` board resource
landed the same day. None of it has met the adapter yet — that is Monday's
first job (`ni_usb_gpib_clean_room.md`, "What it has not had").

## The UI on the lab PC, 2026-09-18

Installed per-user (2.0.0-1, NSIS) and driven through WebView2's debug
port; every mode, dialog and shutdown path exercised against the 2420.
Three blockers (string settings fields crashed the settings dialog blank;
the instrument lock was keyed to the config directory, so a second backend
could destroy a live run; compliance undetected in resistance mode) and
five majors (auto-range silently overriding test current and compliance;
stale vdP results after an abort; identical axis labels; sweep source
switch reinterpreting units) were found and fixed the same day; the
sixteen minor and twelve cosmetic items are tracked outside the repo; the fixes were verified in the
dev UI against the simulator (and on a 2400 for compliance), not yet on the
PC. The first CI build also lacked a WebSocket implementation entirely
(`websockets` was never a declared dependency), fixed in `f5d0e29`.

## The punch list and the spot model, 2026-09-19

Of the UI bench's sixteen minor and twelve cosmetic items, all but four are
closed, verified against the simulator (layout at 1284, 1100 and 950 px):
marks on the live plot, a sweep legend, the instrument badge and the log
surviving a reload (the UI backfills from the backend's event ring), an
in-view notice with Retry when the backend is unreachable, final totals kept
on screen, Greek symbols no longer uppercased, hints and headers that fit,
and the copy. On the backend: file names keep their source value and a run
can never be written over an existing file; a stop during the settle
finalizes its file; van der Pauw announces `run_started`; log messages use
the operator's words; files record which client wrote them; `SessionStatus`
is in the exported contract; sweep compliance is bounded in its own unit,
with the hazardous-voltage prompt proven to cover a current-sourced sweep.

Four-point spots are now part of the record rather than of a widget
(`four_point_probe_spots.md`): a run carries its spot and map, the file
records it with per-spot statistics, maps are assembled from the run files
and served at `/maps`, the PySide6 window sends the spot too, and the
correction factor is known at any position on a circular or rectangular
sample, so a spot near an edge is warned about with the error it costs.

Left for a decision or for bench data: the runtime power stop that
validation makes unreachable, the predicted maximum rate, ρ shown as 0 when
the thickness is unknown, and the 45 s watchdog grace after the window dies.

## Not yet

- **The UI fixes on the lab PC itself** — everything above was verified
  against the simulator.
- **The new driver paths (raw reads/writes, serial poll, SRQ wait, INTFC) on
  a real adapter** — written from captures on 2026-09-19, never run.
- **The map in the Tauri UI** (spots panel from `/maps`, the sample outline,
  the optional photo, the figure) — waits on the open questions in the spots
  design.
- **Prologix / AR488 adapters.** The interface is wired (`gpib_interface`)
  but has never met an adapter: the tests run pyvisa-py against a scripted
  stand-in on a socket. And a Keithley does not connect through it yet —
  `VisaInstrument.connect` refuses an address that `list_resources()` does
  not return, and pyvisa-py lists nothing behind a Prologix adapter. A
  strict xfail in `test_visa_backend.py` holds the place. Also unverified:
  on these sessions pyvisa-py rejects `read_termination`, so `connect`
  leaves pyvisa's default `\r\n` write termination in place.
- **Cable null** is not in the new UI yet; **persisting the safety-silence
  flag** from a headless client is not done.
- PySide6 remains the shipping UI until the above is closed and the bench
  check passes.

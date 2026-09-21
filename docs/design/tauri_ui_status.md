# Tauri Desktop UI — Status (steps 2 and 3)

**Status:** On `phase0/reviewable-baseline`, pushed, CI green; backend bench-checked on the lab 2420 and on a 2400 over the NI USB driver; the UI bench's three blockers and five majors fixed; the UI has driven the 2400 on a Mac through the driver (2026-09-21)
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
(`a967f91`, `13d92ca`). Through the driver: identify, three consecutive
resistance runs (99.51 Ω at 10 mA), stop mid-settle, immediate restart,
shutdown mid-run with the output confirmed off, and the compliance
detection of `adce01e`.

## NI's driver as the oracle, 2026-09-19

With the adapter off the Mac for the weekend, the lab desktop's NI-488.2
stack drove its own GPIB-USB-HS under USBPcap: 22 one-operation captures
(`docs/design/captures/ni_usb_gpib_2026-09-19/`), decoded into §10 of the
protocol specification by a fresh Reader agent. They corrected the
specification in several places — reads over 1 KB and long writes use two
instructions the GPL-derived spec did not know, with the data raw on the
adapter's second bulk endpoint pair; serial poll is its own instruction;
the timeout table was right. The implementer built those paths from §10
alone. After a whole-package review they were made opt-in
(`RESISTAMET_GPIB_NI_INSTRUCTIONS=1`): the framed paths and the serial-poll
sequence that ran on the bench stay the default until the new instructions
have met the adapter; the `GPIB0::INTFC` board resource
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
(`websockets` was never a declared dependency), fixed in `4fbf2dc`.

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

## The driver's second bench day and the UI on a Mac, 2026-09-21

The adapter back on the Mac, the checklist from the audit run against the
2400. Three defects in the driver, all in the protocol specification and
fixed the same day from it: this unit's timeout expiries are not the
captured unit's (1.25 × {0.1, 0.3, 1, 3, 16, 33} s against powers of two
in microseconds), so the host wait now outlasts both; a timed-out
1–15-byte read carries a stale last-block count that the parser trusted
over the 0x38 count field; and a framed read whose answer exceeded about
4.5 kB left the adapter unusable until unplugged — NI never frames a read
above 1024 bytes, and the driver now does not either, reading long answers
as 1024-byte pieces behind one addressing (35 kB in 6.3 s). The opt-in raw
0x0b path reads the same data correctly but times out after 20 s whatever
code it is sent on this unit, an open question in the specification. The
0x10 serial poll works; framed writes up to 2048 bytes need no zero-length
packet at a 512 multiple.

Then the dev UI, against the backend on `@py`: Identify & save, a
resistance run with live plot, Stop, a 21-point I-V sweep with its fit
(99.58 Ω on the 100 Ω reference), all through the driver. Opening Settings
crashed the dialog on a fresh config — `GET /profiles` returned the
config-level `users` and `last_user` beside the sections and the patch
builder walked the null — fixed on both sides. Pulling the USB cable
during a run ended the run with a read error and a finalised file, and
the next run started after the replug with no restart; but the cleanup's
`:OUTP OFF` had no link to travel, so the instrument's output stayed on
until the next session addressed it. That wants a rule (a warning the
operator cannot miss, or a retry when the adapter returns) and is
recorded as an open question. Cosmetic at a 714-pixel viewport: the
sample name and the R² tile clip.

## Not yet

- **The UI fixes on the lab PC itself** — everything above was verified
  against the simulator.
- **SRQ wait and the INTFC session on a real adapter** — never run. The
  raw read/write paths and the 0x10 poll ran on 2026-09-21 (above); the
  raw path stays opt-in until its timeout question is settled.
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

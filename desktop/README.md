# ResistaMet desktop

The Tauri shell over the Python measurement backend. The shell launches the
backend, reads its one-line handshake, and hands the URL and token to the React
UI; everything about measuring lives on the Python side.

## Layout

```
desktop/
├── src/                 React + TypeScript UI
│   ├── generated/       TypeScript types generated from ../contracts (do not edit)
│   ├── lib/             API client, event stream, formatting, field presentation
│   ├── state/           small stores (session, samples, ui, overrides, sweep, vdp, spots)
│   ├── components/      shell, primitives, plots, dialogs, forms
│   └── views/           one directory per measurement view + results
├── src-tauri/           Rust shell: backend process, window, bundling
├── sidecar/             the frozen backend, when built (see Packaging)
├── scripts/             generate-types.mjs
└── index.html, vite.config.ts, tsconfig.json
```

## Prerequisites

- Node 20+ and npm
- Rust (`rustup`, stable) — <https://rustup.rs>
- The Python backend in a venv at the repo root (`pip install -e ".[dev,api]"`)

## Develop

```bash
cd desktop
npm install
RESISTAMET_SIMULATE=1 npm run tauri dev
```

`tauri dev` starts Vite and the shell. The shell finds the backend in this
order: `RESISTAMET_PYTHON`, a frozen `resistamet-api` in the bundle's resources
or beside the executable, the repo's `.venv`, then `python3` on PATH. In a
source checkout it runs with the repo as working directory and its
`config.json`, so it shares profiles and `measurement_data` with the PySide6 app.
`RESISTAMET_SIMULATE=1` runs against the in-package simulator.

To work on the UI in a plain browser instead, start the backend yourself and
pass its handshake to the page:

```bash
.venv/bin/python -m resistamet_gui.api --port 8765 --simulate --token dev --no-watchdog
# then: npm run dev  →  http://localhost:1420/?backend=http://127.0.0.1:8765&token=dev
```

`--no-watchdog` matters: the backend otherwise exits when its stdin closes,
which is what a backgrounded shell does to it.

## Types from the contract

`npm run gen:types` regenerates `src/generated/*.ts` from
`../contracts/*.schema.json`: the models, plus `FIELD_META` (bounds, enums,
defaults per field) and `MODE_MODEL`. `npm run check:types` fails when they are
stale; CI runs it. Never edit the generated files. Labels and units are the
UI's business and live in `src/lib/fields.ts`, keyed against the generated
types so a backend rename is a compile error.

## Packaging

The backend is frozen with PyInstaller (`resistamet-api.spec` at the repo root,
one-dir, no Qt) into `desktop/sidecar/resistamet-api`, which
`tauri.conf.json` bundles as a resource:

```bash
pyinstaller resistamet-api.spec --noconfirm --clean --distpath desktop/sidecar
cd desktop && npm run tauri build
```

`.github/workflows/desktop.yml` does this for Windows (msi, nsis) and macOS
(dmg) on every `v*` tag and attaches the bundles to the release. The frozen
backend is smoke-tested in CI — handshake, `/health`, clean exit on stdin close
— before the shell is built around it.

`sidecar/resistamet-api/` holds only a README in the repository so the resource
path exists at compile time; the build fills it.

## Instruments on a Mac

NI ships no GPIB driver for current macOS (NI-488.2 ended at 21.5.1, whose
kernel extension does not load on macOS 13+). The backend therefore has its
own user-space driver for the NI GPIB-USB-HS family over libusb
(`resistamet_gui/gpib_usb`), used through pyvisa-py. Settings ▸ Instrument ▸
VISA backend chooses: *Automatic* takes NI-VISA when installed (right on the
lab's Windows PCs), *pyvisa-py* is the Mac route. The frozen macOS backend
bundles libusb; a source checkout needs `brew install libusb`.

To ask an installed build what it can see, without a GUI:

```bash
resistamet-api --check-visa            # which VISA library, and can libusb load
resistamet-api --check-visa bus        # also enumerate resources (touches the bus)
resistamet-api --check-visa --visa-library @py
```

One JSON line, exit 1 when no VISA implementation opened. This is the
diagnostic for "the app sees no instruments": a vendor library can be present
and still have no GPIB driver behind it.

Routes pyvisa-py can reach: the NI GPIB-USB-HS (`GPIB0::24::INSTR`), a
Keithley over RS-232 (`ASRL/dev/cu.…::INSTR`), or a 2450 over Ethernet
(`TCPIP::…::INSTR`). A Prologix GPIB-USB or AR488 adapter is supported by
pyvisa-py too, but needs its `PRLGX-ASRL::…::INTFC` interface resource
opened before the instrument address works; the app does not do that yet.
The NI driver has been written from the protocol specification and tested
against a scripted fake only; its first run on a real adapter is still to
come.

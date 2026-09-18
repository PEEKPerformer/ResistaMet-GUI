# ResistaMet desktop

The Tauri shell over the Python measurement backend. The shell launches
`python -m resistamet_gui.api`, reads its one-line handshake, and hands the URL
and token to the React UI; everything about measuring lives on the Python side.

## Layout

```
desktop/
├── src/                 React + TypeScript UI
│   └── generated/       TypeScript types generated from ../contracts (do not edit)
├── src-tauri/           Rust shell: backend process, window, bundling
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
npm run tauri dev
```

`tauri dev` starts Vite and the shell; the shell spawns the backend from the
repo's `.venv` (override with `RESISTAMET_PYTHON=/path/to/python`).

To work on the UI in a plain browser instead, start the backend yourself and
pass its handshake to the page:

```bash
.venv/bin/python -m resistamet_gui.api --port 8765 --simulate --token dev
# then: npm run dev  →  http://localhost:1420/?backend=http://127.0.0.1:8765&token=dev
```

## Types from the contract

`npm run gen:types` regenerates `src/generated/*.ts` from
`../contracts/*.schema.json`. `npm run check:types` fails when they are stale;
CI runs it. Never edit the generated files.

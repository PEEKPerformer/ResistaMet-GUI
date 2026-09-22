# Frozen backend goes here

`tauri.conf.json` bundles this directory as the `resistamet-api` resource. CI
fills it by running, from the repo root:

    pyinstaller resistamet-api.spec --noconfirm --clean --distpath desktop/sidecar

For a source checkout it may stay empty: the shell then falls back to the
repo's `.venv` (see `src-tauri/src/backend.rs`). This file exists so the path
does, which tauri-build requires at compile time.

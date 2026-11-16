# Desktop app

A second front end for the same measurements: a Tauri shell with a React user interface, talking to the Python measurement backend over the [API](api.md). It starts the backend itself, and the backend does all the measuring and writes all the [files](outputs.md). The instrument code and the file formats are the ones the PySide6 window uses.

!!! warning "In development"
    The PySide6 window (`resistamet-gui`) is the released application. The desktop app lives on the development branch and changes daily; this page describes it as of 2026-09-19. It was driven against a Keithley 2420 on a lab PC on 2026-09-18, every mode and dialog; the defects found then were fixed and re-checked against the simulator (and compliance detection on a 2400), not yet again on that PC. There is no cable null in it yet.

## Running it

### From source

You need Node 20 or later with npm, Rust (stable, from <https://rustup.rs>), and the Python backend in a virtual environment named `.venv` at the repository root:

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev,api]"
cd desktop
npm install
RESISTAMET_SIMULATE=1 npm run tauri dev      # simulator; drop the variable for real hardware
```

The shell looks for a backend in this order: the interpreter named by `RESISTAMET_PYTHON`; a frozen `resistamet-api` in the app bundle's resources or next to the executable; the repository's `.venv`; `python3` (`python` on Windows) on the PATH. `RESISTAMET_PYTHON`, `RESISTAMET_SIMULATE` and the use of the checkout are honored by development (debug) builds only; an installed build ignores all three, so a variable left set on a lab PC cannot point it at the simulator. While the backend runs with `--simulate`, a fixed **SIMULATED** badge stays at the top of the window.

!!! note "A source checkout shares the PySide6 app's data"
    Run from a checkout with `tauri dev`, the backend's working directory is the repository root and its profile file is the repository's `config.json`, so the desktop app and `resistamet-gui` started from the same checkout share operators, profiles and `measurement_data/`.

To work on the user interface in a plain browser, start the backend yourself and pass its address to the page:

```bash
.venv/bin/python -m resistamet_gui.api --port 8765 --simulate --token dev --no-watchdog
cd desktop && npm run dev      # then open http://localhost:1420/?backend=http://127.0.0.1:8765&token=dev
```

### Installers

`.github/workflows/desktop.yml` builds Windows installers (msi and NSIS) and a macOS dmg for `desktop-v<version>` tags, with the backend frozen by PyInstaller inside the bundle, and uploads them to the release of that tag once someone has created it. No release carries them yet. A machine running an installer build needs no Python. It still needs a way to reach the instrument: NI-VISA with NI-488.2 on Windows; on a Mac the bundled backend includes libusb for the [built-in NI GPIB-USB driver](gpib.md#ni-gpib-usb-hs-on-macos-and-linux).

## Where things are

| | Source checkout | Installed app |
|---|---|---|
| `config.json` (operators, profiles, machine-local settings) | Repository root | The app's per-user data directory |
| Data files | `data_directory` from the profile; the default `measurement_data` resolves against the repository root | The same setting; the default resolves to `measurement_data` inside the app's data directory. Set an absolute data directory under Settings ▸ Files & output to put data somewhere you back up. |
| Backend log | The terminal you started `tauri dev` from | One file per launch, `backend-<milliseconds since 1970>.log`, in the app's log directory; the newest ten are kept |
| Instrument locks | `~/.resistamet/locks/` | the same |
| Window state (selected operator, sample name, each operator's tab values, theme, current map id) | the webview's local storage | the same |

The app's identifier is `edu.uconn.adamson.resistamet`, and the two directories are wherever Tauri puts per-user data and logs for that identifier. By Tauri's convention that is `~/Library/Application Support/<identifier>` and `~/Library/Logs/<identifier>` on macOS, and `%APPDATA%\<identifier>` and `%LOCALAPPDATA%\<identifier>\logs` on Windows. These paths were not checked on an installed build for this page; the **Results** view shows the data directory's absolute path, which settles it.

Files are laid out under the data directory exactly as the PySide6 app lays them out: one folder per operator, names as in [Data outputs](outputs.md#file-names). Every file the desktop app starts records `client.name: resistamet-desktop` and the app's version in its header.

## The window

**Top bar.** The sample name (locked during a run), a chip with the instrument the backend last identified or connected to, a chip with the state of the connection to the backend, the operator, and Settings. **Left rail.** The six measurement modes and Results; a dot marks the mode that is running. Only one run exists at a time, whichever view you look at. **Bottom.** The log: the backend's messages in the operator's words, refilled from the backend after a reload.

An **operator** must be chosen before anything else; the picker opens by itself and creates profiles. Every file is stored under the operator's name, and the operator's profile supplies the settings.

Each measurement view has its settings panel on the right. Values you type there are this tab's values on top of the profile, kept per operator; they are sent to the backend as you type, and the backend answers with what the run would actually use, any issues, the achievable sampling rate and the touch-safety check. **Start** is enabled only when the backend is reachable, an operator and a sample name exist, no run is active and the backend accepts the settings. Numeric fields take engineering input (`100u`, `1e-4`, `0.1 mA`); the prefix is case-sensitive (`m` is milli, `M` is mega), and text that is not wholly a number is refused, not guessed at.

### Resistance, Voltage source, Current source

Live plot with a time window (30 s, 5 min, 1 h, all), a large readout, Start / Pause / Stop / **Mark**. Mark (or the `M` key when the focus is not in a field) puts a label on the next data row and on the plot. Notices above the plot say why Start is unavailable, warn that a voltage at or above the touch-safety threshold will be asked about, and warn when the requested rate exceeds what the timing settings can deliver.

In Resistance mode with **Auto range** on, the instrument's auto-ohms function chooses its own test current and voltage limit, so the fields for them are disabled; the current that flowed is in the file's `I_meas` column.

### Four-point probe

The continuous view plus three panels:

- **Spot.** Rs, ρ and σ of the current sample as the backend derived them, and, when the run ends, the spot's statistics from the backend's `spot_complete` event: mean ± combined uncertainty, n, and whether the run was cut short. The name field labels the next spot.
- **Spots.** The current map's spots as the backend assembles them from the run files (`GET /maps/{id}`): mean, RSD, the edge effect where known, and the spread across spots. **Redo** on a row makes the next run measure that spot again; the newer run stands for the spot and the older file stays on disk. **New map** makes the next run start a new map; the files of the old one stay.
- **Map.** The sample outline to scale with one marker per spot that has a position, colored by the chosen quantity, with optional labels and a nearest-spot fill (not an interpolation). It needs the sample's shape and dimensions from the **Sample** group of the settings panel. Click the drawing to say where the next run measures, or type the coordinates (millimeters from the center of the sample, y up); with the drawing focused the arrow keys move the position by 0.1 mm, with Shift by 1 mm. The marker shows the four tips to scale at the array angle, which is one setting for the whole map. It turns red, and Start is disabled, when a tip is on or beyond the edge; it turns amber near an edge, where the backend will report the size of the effect when the run starts. A run without a position is allowed. **Redo** puts a spot's recorded position back.

Each four-point run is sent with its spot (map id, index, label, and the position if one was given), so the [map](concepts.md#spots-and-maps) exists in the files and survives the app. A new map starts with the first run after the app starts, after the operator or sample name changes, and on New map.

The map is still being built, and this paragraph will date fastest. At the time of writing there is no sample photo underlay and no figure export, and none of the map has been used at a bench.

A run refused because a tip would be off the sample shows the backend's message in the view with "Nothing was measured."

### I-V sweep

Configure, run once, look at the curve: one trace per direction, the point count, and a least-squares resistance with R² as a sanity check on an ohmic sample. The fit leaves out points taken in compliance and fits the two legs of an up-down sweep separately, giving one figure only when they agree. The instrument runs the sweep itself and returns all points at once, so there is no live trace and no Pause. The compliance field is a current for a voltage sweep and a voltage for a current sweep ([bounds](settings.md#i-v-sweep-compliance)).

### van der Pauw

A four-step wizard for ASTM F76 Method A. At each geometry the backend stops at a prompt with the output off; the view shows which contact each of Force HI, Force LO, Sense HI and Sense LO goes to on a diagram of the sample, and waits for **Measure**. Both current polarities are taken automatically. Readings fill a table as they arrive, and the result panel gives sheet resistance and resistivity with combined uncertainties and the f(Q) homogeneity verdict.

### Results

The operator's files, newest first, and a preview of one: the metadata header and footer as written, and any numeric column plotted against elapsed time. Only `.csv` files are previewed; `.csv.gz`, `.h5` and legacy `.json` files are listed. The absolute path of the data directory is shown at the top.

### Settings

Profile settings that do not belong to one tab, in six sections: **Timing** (NPLC, sampling rate, settling time, auto zero, hardware filter, stop on compliance), **Instrument**, **Aux sensor**, **Safety**, **Files & output**, and **Display** (the theme, stored on this computer, not in the profile). Instrument holds the three [machine-local](settings.md#machine-local-settings) values: VISA backend, GPIB interface (Prologix) and address. **Scan** and **Identify** use the backend and interface as typed, before they are saved, and report which VISA implementation answered. They are refused while a run is active.

### Prompts

When a run is blocked on a decision (the touch-safety acknowledgement, a van der Pauw rewiring) a dialog appears that cannot be dismissed; its buttons are the only way on. It is driven by the backend's state, so it reappears after a reload. The touch-safety dialog has no "don't ask again" box, because an answer cannot silence a profile yet; to silence the warning, set **Warning silenced for this profile** under Settings ▸ Safety.

## Closing the window

With a run in progress, the close button, Alt+F4 and, on macOS, Cmd+Q first ask **Exit confirmation**: *Keep running* (the default) or *Stop and exit*. If the backend does not answer within 3 s the question is asked anyway, since a run cannot be ruled out. With no run the window closes at once.

Exiting does not abandon the instrument:

1. The shell closes the backend's stdin.
2. The backend stops the run: `:OUTP OFF`, the file finalized with its footer (end reason `user_stop`), the instrument closed, the lock released. It allows itself 35 s for that.
3. The shell waits up to 40 s for the backend to exit and only then kills it.

So closing the window mid-run ends the run cleanly and keeps the data. The same ordered shutdown was checked on a 2420 through `POST /session/shutdown` on 2026-09-18, with the output confirmed off afterwards by asking the instrument directly.

What this cannot cover: the backend or the PC being killed outright, or a VISA call that does not return within the 35 s. Then nobody tells the instrument anything, and the output stays as it was. If the backend process dies while the window is open, the app says so in a blocking alert (**The measurement backend has exited**; the output state is unknown, check the front panel) and offers **Restart backend**. If the app's shell dies without closing anything (a crash, a forced quit), the backend notices that its stdin closed and performs step 2 on its own.

Reloading the page inside the window (or a crashed webview) does not touch the backend: the run continues, and the view catches up from the backend's status and event history.

# Settings reference

Open via the **Settings** button or menu. Four tabs: Measurement, Display, File, Output. All values are persisted per-user-profile in `config.json` (gitignored, in the working directory), except the three [machine-local settings](#machine-local-settings). Cross-references in this page link to [Concepts](concepts.md) for term explanations.

The tab layout below is the PySide6 window's. The [desktop app](desktop.md) edits the same profiles through the [API](api.md), and the setting names in `code font` are the keys both of them, `config.json` and an API `overrides` dict use. Defaults come from `DEFAULT_SETTINGS` in `resistamet_gui/constants.py`; bounds from the models in `resistamet_gui/schema/`. A value outside its bounds in a stored profile still loads; it is reported as an issue, and an API run (`POST /session/start`) is refused with 422 until it is fixed.

## Measurement tab

### General instrument

| Setting | Default | Range / type | What it does |
|---|---|---|---|
| **GPIB Address** (`gpib_address`) | `GPIB0::24::INSTR` | VISA resource string, not empty | Where to find the instrument. **Detect Devices** scans the VISA buses and offers the result in a picker. [Machine-local](#machine-local-settings). |
| **VISA backend** (`visa_library`) | `""` (Automatic) | `""`, `@ivi`, `@py`, or the path of a VISA library | Which VISA implementation opens the bus. Automatic is pyvisa's own default: the vendor library (NI-VISA) when one is installed, otherwise pyvisa-py. `@py` forces pyvisa-py, which is the route to an NI GPIB-USB-HS on macOS or Linux, a Prologix adapter, or a serial link. See [GPIB and VISA backends](gpib.md). [Machine-local](#machine-local-settings). |
| **GPIB interface** (`gpib_interface`) | `""` (none) | `""`, `PRLGX-ASRL[board]::<device>::INTFC` or `PRLGX-TCPIP[board]::<host>[::port]::INTFC` | The interface resource of a Prologix-style adapter, opened before the instrument. pyvisa-py only; ignored with a logged warning under a vendor library. See [GPIB → Prologix and AR488](gpib.md#prologix-and-ar488-adapters) for the grammar and for what does not work yet. [Machine-local](#machine-local-settings). |
| **Sampling Rate** | `10.0 Hz` | `0.1 – 100 Hz` | Default readings per second. Each measurement tab has its own spinbox that overrides this. Each per-tab spinbox is dynamically capped based on the active (NPLC × auto-zero × filter × offset-comp) combination; if you commit a value above the cap the status bar reports the cap and suggests the cheapest single setting change to reach the requested rate. See [Concepts → Rate-cap predictor](concepts.md#rate-cap-predictor). |
| **NPLC** | `1` | `0.01 – 10` | [Integration time](concepts.md#nplc-number-of-power-line-cycles) per reading. Lower = faster + noisier; higher = slower + cleaner. |
| **Settling Time** | `0.2 s` | `0 – 10 s` | Delay after output enable before the first reading. Lets DUT + instrument stabilize. |

!!! note "Where to set the VISA backend and the GPIB interface"
    At this commit only the desktop app has fields for them (Settings ▸ Instrument). The PySide6 Settings dialog does not; it honors the stored values when it connects for a run or for **Test Connection**, but its **Detect Devices** scan always uses pyvisa's default backend. Without the desktop app, set them with `PATCH /profiles/{user}` or by editing `machines.<hostname>` in `config.json`.

### Machine-local settings

`gpib_address`, `visa_library` and `gpib_interface` describe a PC and its cabling, not an operator. They are stored in `config.json` under `machines.<hostname>`, where `<hostname>` is what the operating system reports for the PC (`socket.gethostname()`), and never in a user's profile or in the shared `measurement` block:

```json
"machines": {
  "bench-pc-1": {"gpib_address": "GPIB0::24::INSTR"},
  "office-mac": {"gpib_address": "GPIB0::3::INSTR", "visa_library": "@py"}
}
```

What that means when one `config.json` is shared between PCs through a synced folder or a network drive:

- Each PC reads and writes only its own entry. Operators, profiles and every other setting are shared; the address, the backend and the interface are not.
- When a profile is read, the three values for the current PC are filled into its `measurement` section, so a run, the API and both UIs see them as ordinary settings. When a profile is saved, they are taken back out and written to the PC's entry.
- A PC with no entry yet falls back to a value left in the shared `measurement` block by an older version, then to the default. The first save on that PC moves the value into its own entry and removes the shared copy.
- An empty `gpib_address` is never stored. An empty `visa_library` or `gpib_interface` is a real choice (Automatic, none) and is stored.
- Two PCs that report the same hostname share one entry. Rename one of them.
- Through the API these three keys cannot be changed while a run is in progress (409), and `gpib_address` can never be sent as a run override.

### Resistance defaults

Applied to the Resistance tab on launch; the tab itself has live widgets that override these per-run.

| Setting | Default | Notes |
|---|---|---|
| **Test Current** | `1 mA` | DC current sourced through the DUT |
| **Voltage Compliance** | `5 V` | Maximum allowed across the DUT |
| **Measurement Type** | `2-wire` | Or `4-wire` for Kelvin |

### Voltage Source defaults

| Setting | Default | Notes |
|---|---|---|
| **Source Voltage** | `1.0 V` | Accepts negative values |
| **Current Compliance** | `100 mA` |  |
| **Duration (hours)** | `0` (= run until stopped) | Set non-zero for unattended runs |

### Current Source defaults

| Setting | Default | Notes |
|---|---|---|
| **Source Current** | `1 mA` | Accepts negative values |
| **Voltage Compliance** | `5 V` |  |
| **Duration (hours)** | `0` (= run until stopped) |  |
| **Stop on compliance** | `False` | Auto-Stop when compliance is reached. Useful for protecting sensitive samples in source-V too. |

### Advanced instrument settings

| Setting | Default | Notes |
|---|---|---|
| **Enable Hardware Filter** | `True` | Use the Keithley's built-in averaging filter (see [Concepts → Hardware averaging filter](concepts.md#hardware-averaging-filter)) |
| **Filter Type** | `repeat` | `repeat` (N readings → 1 result, then repeat) or `moving` (running average) |
| **Filter Count** | `5` | `1 – 100` |
| **Enhanced accuracy in Resistance mode** | `True` | Offset-compensated ohms. See [Concepts → Enhanced R mode](concepts.md#enhanced-r-mode). |
| **Touch-safety warn threshold** (`safety_voltage_warn_v`) | `30 V` | Voltage at or above this triggers the warning before run start. `0` disables. The PySide6 spin box accepts `0 – 1100 V`; the settings schema allows `0 – 200 V`, so a threshold above 200 V is reported as an issue and an API or desktop-app run is refused until it is lowered. See [Concepts → Touch-safety warning](concepts.md#touch-safety-warning). |
| **Suppress touch-safety warning for this profile** | `False` | Equivalent to clicking "Don't show again" on the modal. Uncheck to re-enable. |

### Four-point sample outline and spot position

Seven settings describe the sample a four-point spot sits on and how its position is judged. They exist for the position check of a [spot](concepts.md#spots-and-maps); they do not change any number in a data row.

| Setting | Default | Bounds | What it does |
|---|---|---|---|
| `fpp_sample_shape` | `unbounded` | `unbounded`, `circle`, `rectangle` | The outline a spot's position is checked against. `unbounded` means "use the legacy keys" (below). |
| `fpp_sample_diameter_mm` | `0` | `0 – 1000 mm` | Circle diameter. `0` = not entered; a circle with no diameter is an error. |
| `fpp_sample_width_mm` | `0` | `0 – 1000 mm` | Rectangle side along y, across the probe array at 0°. `0` = not entered. |
| `fpp_sample_length_mm` | `0` | `0 – 1000 mm` | Rectangle side along x, along the probe array at 0°. `0` = not entered. |
| `fpp_array_angle_deg` | `0` | `−360 – 360°` | Direction of the probe array on the sample, counterclockwise from +x. Used when a spot does not give its own angle. |
| `fpp_edge_warn_pct` | `1.0` | `0 – 100 %` | A spot is flagged as near an edge when assuming a centered probe would cost more than this. The comparison is against the factor the rows really apply when there is one; see [Data outputs → `spot.*`](outputs.md#spot-four-point-runs-that-carry-a-spot). |
| `fpp_position_correction` | `warn` | `warn` only | The position effect is reported in the file and as a warning, never applied. No other value is implemented: the API refuses one (422), and the PySide6 path ignores a hand-edited one with a log warning and still records `warn` in the file. |

Only the dimensions the chosen shape has are read: a width left over from a rectangle is ignored once the shape is a circle. Positions are in millimeters from the center of the sample.

**Relation to `fpp_geometry` and `fpp_diameter_cm`.** Those two older settings (Four-Point Probe tab: specimen shape and diameter) still drive the ASTM F84 / Smits table look-up behind every `Rs_ohm_sq` in the data rows. The `fpp_sample_*` keys feed only the position check. The two are tied together as follows:

- While `fpp_sample_shape` is `unbounded`, the outline is derived from the legacy keys, so an existing profile needs no change: `circle` with `fpp_diameter_cm` = D gives a circle of diameter 10·D mm; `square`, `rectangle_2`, `rectangle_3`, `rectangle_4` give a rectangle whose width is 10·D mm and whose length is 1, 2, 3 or 4 times that; `fpp_diameter_cm` = 0 gives an unbounded sheet, for which no position check is made.
- When `fpp_sample_shape` names a shape, that outline is used for the position check, with any aspect ratio. The rows still use the legacy keys. If the two describe different samples the run is allowed and `POST /settings/resolve` returns a `warning` issue on `fpp_sample_shape` saying so; the file then shows the disagreement as a large `spot.relative_error_rows`.

At this commit neither UI has fields for these seven settings. Set them as API overrides on a four-point run, with `PATCH /profiles/{user}`, or in `config.json`.

### I-V sweep compliance

`sweep_compliance` limits the quantity the sweep *measures*, so its unit follows `sweep_source`:

| `sweep_source` | `sweep_compliance` is a | Bounds | Default |
|---|---|---|---|
| `voltage` | current limit | `1e-7 – 3.15 A` | `0.1` |
| `current` | voltage limit | `1e-7 – 210 V` | `0.1` |

3.15 A is the compliance ceiling of the 3 A class (2420, 2425) and 210 V that of the 2400's 200 V range. They are the family's envelope, not the connected model's; the instrument rejects what it cannot do. The value is not converted when you switch the source: 0.1 means 0.1 A on a voltage sweep and 0.1 V on a current sweep. `sweep_start` and `sweep_stop` are bounded at ±200 and `sweep_step` at 200 for both sources.

**Hazardous-voltage check.** For a voltage-sourced sweep the voltage that counts is `max(|sweep_start|, |sweep_stop|)`; for a current-sourced sweep it is `sweep_compliance`, because an open circuit drives the output to its voltage limit. When that value reaches the touch-safety threshold, the PySide6 window shows its warning dialog before starting, and a run started through the API stops at a `safety_voltage_ack` prompt before the instrument is opened (see [API → Prompts](api.md#prompts)). `POST /settings/resolve` reports the same check as `hazard` without starting anything.

!!! note "Auto-Zero lives on each tab, not in Settings"
    Pre-v1.10 it was here, but per-tab makes more sense: 4PP and vdP force `auto_zero='on'` regardless; sensor modes (resistance, source_v, source_i) expose the knob on the tab itself so re-tuning the speed/accuracy trade doesn't require a Settings dialog detour.

## Display tab

| Setting | Default | Notes |
|---|---|---|
| **Enable Real-time Plots** | `True` | Disable for high-rate runs (>50 Hz) where rendering eats CPU |
| **Resistance Plot Color** | `#D55E00` (Wong vermillion) | Matches the V/I/R/P live-readout label colors |
| **V Source Plot Color** | `#0072B2` (Wong blue) | |
| **I Source Plot Color** | `#009E73` (Wong green) |  |
| **Data Buffer Size (points)** | `0` (Unlimited) | `0` = no cap. pyqtgraph downsamples on render (peak-mode + `clipToView`) so a 17-hour / 270k-sample run is ~13 MB and stays smooth. Set a finite cap only if RAM is tight. |

!!! warning "Pre-v1.10 buffer-size default was 1000"
    Old configs that explicitly set `buffer_size` to a small number silently truncated the live trace on overnight runs. New default is unlimited; existing configs override unless you reset.

## File tab

| Setting | Default | Notes |
|---|---|---|
| **Auto-save Interval** | `60 s` | How often to flush data to disk during a measurement. Lower = less data loss on crash, but more I/O. |
| **Data Directory** | `measurement_data` | Root for run output. A subdirectory is created for each user. **Browse...** button opens a folder picker. |

## Output tab

See [Data Outputs](outputs.md) for the full format reference; this tab just selects which backend to use.

| Setting | Default | Notes |
|---|---|---|
| **Output format** | `csv` | One of: `csv` (default, single `.csv` with `#`-prefixed metadata header), `hdf5` (requires `h5py`), `csv+legacy_json` (pre-2.0 dual emit for back-compat with downstream pipelines) |
| **Compression** | `never` | Applies only to the `csv` backend. `never` / `always` / `auto`. Default is `never` because Excel, Origin, and most lab tools can't open `.csv.gz` directly. HDF5 is always internally gzip-compressed regardless. |
| **Auto-compression threshold** | `5 MB` | Only used by the `auto` compression policy |

!!! info "HDF5 grayed out?"
    If `h5py` isn't installed, the HDF5 option is disabled with a tooltip explaining why. `pip install h5py` and restart to enable it.

## Configuration storage

Settings are kept in `config.json` in the working directory (gitignored — per-user, not portable). Structure mirrors the four Settings tabs:

```json
{
  "measurement": {...},
  "display": {...},
  "file": {...},
  "output": {...},
  "users": ["alice", "bob"],
  "last_user": "alice",
  "machines": {"bench-pc-1": {"gpib_address": "GPIB0::24::INSTR"}},
  "user_settings": {
    "alice": {"measurement": {...}, ...},
    "bob": {"measurement": {...}, ...}
  }
}
```

When a user is selected, their per-user overrides deep-merge on top of the global defaults, and the current PC's [machine-local](#machine-local-settings) values are filled in. The file is written atomically (temporary file, `fsync`, then rename), so a crash during a save leaves the previous file intact. Editing this file by hand works but is error-prone — prefer the dialog. If the file gets corrupted, delete it and ResistaMet will recreate from `DEFAULT_SETTINGS` (in [`resistamet_gui/constants.py`](https://github.com/PEEKPerformer/ResistaMet-GUI/blob/main/resistamet_gui/constants.py)).

## Profiles menu

The top-level **Profiles** menu has two entries that operate on the currently-active tab (Resistance / Voltage Source / Current Source / Four-Point Probe — Sweep and vdP are excluded):

- **Save Profile for Current Mode...** writes the active tab's measurement settings to a `.json` file (suggested name `<mode>_profile.json`). Useful for "sensor A typical run" vs "sensor B typical run" templates that you swap between samples.
- **Load Profile to Current Mode...** reads a `.json` and applies known fields to the active tab's widgets. Unknown fields are ignored. Legacy `fpp_thickness_cm` is auto-converted to `fpp_thickness_um` for backward compatibility.

These are file-level profiles distinct from the per-user settings stored in `config.json`. They don't move the GPIB address (which is machine-local).

## Results Viewer tab

A 7th tab "Results Viewer" is included by default. It lets you:

- Open any v2.0 `.csv` or `.csv.gz` file produced by ResistaMet GUI (also accepts older legacy CSVs)
- Pick any column from the dropdown to plot vs `elapsed_s`
- Read the trailing metadata (run mode, sample name, start time) into the status log

No write-back — it's strictly a post-run viewer.

## Per-tab settings (not in this dialog)

Some settings live on the individual mode tabs because they're per-measurement-context, not session-wide:

- **Auto Zero** — Resistance / Voltage Source / Current Source tabs (each)
- **Enhanced accuracy** — Resistance tab (separate from the global Settings checkbox, which sets the default)
- **Source Range Auto / Voltage Range Auto** — every tab
- **Cable null** — Resistance tab
- **Delta mode (current reversal)** — Four-Point Probe tab Advanced section
- **Geometry / dopant / temperature** — Four-Point Probe tab (drive F84 corrections)
- **Sample thickness** — Four-Point Probe and Van der Pauw tabs
- **Sweep start / stop / step / direction / per-step delay** — I-V Sweep tab

These follow the same per-user persistence — they're written into the user's section of `config.json` when the user clicks **Save Settings** on the active tab.

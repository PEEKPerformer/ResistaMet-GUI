# Settings reference

Open via the **Settings** button or menu. Four tabs: Measurement, Display, File, Output. All values are persisted per-user-profile in `config.json` (gitignored, in the working directory). Cross-references in this page link to [Concepts](concepts.md) for term explanations.

## Measurement tab

### General instrument

| Setting | Default | Range / type | What it does |
|---|---|---|---|
| **GPIB Address** | `GPIB0::24::INSTR` | VISA resource string | Where to find the instrument. **Detect Devices** scans all available VISA buses and offers the result in a picker. **Machine-local**: stored keyed by hostname rather than in the shared profile, so a NAS-shared `config.json` works across lab PCs with different wiring. Any legacy single-address slot is auto-lifted on first save. |
| **Sampling Rate** | `10.0 Hz` | `0.1 – 100 Hz` | Default readings per second. Each measurement tab has its own spinbox that overrides this. Each per-tab spinbox is dynamically capped based on the active (NPLC × auto-zero × filter × offset-comp) combination; if you commit a value above the cap the status bar reports the cap and suggests the cheapest single setting change to reach the requested rate. See [Concepts → Rate-cap predictor](concepts.md#rate-cap-predictor). |
| **NPLC** | `1` | `0.01 – 10` | [Integration time](concepts.md#nplc-number-of-power-line-cycles) per reading. Lower = faster + noisier; higher = slower + cleaner. |
| **Settling Time** | `0.2 s` | `0 – 10 s` | Delay after output enable before the first reading. Lets DUT + instrument stabilize. |

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
| **Touch-safety warn threshold** | `30 V` | `0 – 1100 V`. Voltage at or above this triggers the warning modal before run start. `0` disables. See [Concepts → Touch-safety warning](concepts.md#touch-safety-warning). |
| **Suppress touch-safety warning for this profile** | `False` | Equivalent to clicking "Don't show again" on the modal. Uncheck to re-enable. |

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
  "user_settings": {
    "alice": {"measurement": {...}, ...},
    "bob": {"measurement": {...}, ...}
  }
}
```

When a user is selected, their per-user overrides deep-merge on top of the global defaults. Editing this file by hand works but is error-prone — prefer the dialog. If the file gets corrupted, delete it and ResistaMet will recreate from `DEFAULT_SETTINGS` (in [`resistamet_gui/constants.py`](https://github.com/PEEKPerformer/ResistaMet-GUI/blob/main/resistamet_gui/constants.py)).

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

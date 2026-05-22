# ResistaMet GUI

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19919751.svg)](https://doi.org/10.5281/zenodo.19919751)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Open-source graphical interface for electrical characterization using Keithley 2400-family sourcemeters, with ASTM F84-aligned four-point-probe analysis.

**Version:** 1.10.0
**Author:** Brenden Ferland

![ResistaMet GUI — I-V Sweep tab](docs/screenshots/05_iv_sweep.png)

<details>
<summary>More tabs (click to expand)</summary>

| Tab | Screenshot |
|-----|-----------|
| Resistance Measurement | ![Resistance](docs/screenshots/01_resistance.png) |
| Voltage Source | ![Voltage Source](docs/screenshots/02_voltage_source.png) |
| Current Source | ![Current Source](docs/screenshots/03_current_source.png) |
| 4-Point Probe | ![4-Point Probe](docs/screenshots/04_four_point_probe.png) |
| I-V Sweep | ![I-V Sweep](docs/screenshots/05_iv_sweep.png) |

Generated reproducibly via `python tools/generate_screenshots.py` — runs headless under Qt's offscreen platform, no instrument required.

</details>

## Try it now (no instrument required)

```bash
git clone https://github.com/PEEKPerformer/ResistaMet-GUI.git
cd ResistaMet-GUI
pip install -e .
resistamet-gui --simulate
```

This launches the full GUI against an in-package Keithley 2400-family simulator. Every measurement mode works end-to-end with no NI-VISA, no pyvisa-py, and no GPIB hardware. The simulator is the same one tests validate byte-equivalent against captured hardware traces — see [`docs/sim_fidelity.md`](docs/sim_fidelity.md).

Optional: `--sim-resistance 1000` (1 kΩ DUT) or `--sim-model 2410` (advertise a different Keithley model).

## Overview

ResistaMet GUI is a PyQt5 desktop application for controlling Keithley sourcemeters and performing electrical measurements. It supports five measurement modes (including hardware-driven I-V sweeps), real-time data visualization, multi-spot four-point-probe analysis with delta mode and ASTM F84-aligned correction factors, and dual-format data export.

## Statement of need

The Keithley 2400/2450 family is standard equipment in materials, sensor, and device-physics labs. A typical day on one of these instruments combines long-duration fixed-bias logging with event annotations, periodic four-point-probe surveys, and routine 2-/4-wire resistance measurements — usually on the same instrument and the same sample. No single open application covers that combined workflow.

Researchers today either commit to a scripting framework (`pymeasure`, `QCoDeS`) and write `Procedure` subclasses, buy per-seat vendor software (`KickStart`, Windows-only, no four-point-probe analysis), or assemble narrow single-mode tools. ResistaMet GUI is for researchers who own a Keithley sourcemeter, do not write instrument-control code, and need every mode of that bench workflow in one point-and-click application.

## Features

### Measurement Modes

| Mode | Sources | Measures | Use Case |
|------|---------|----------|----------|
| **Resistance** | Current (up to 3A) | Resistance | 2-wire/4-wire resistance measurement |
| **Voltage Source** | Voltage (-200 to +200V) | Current | Bias stress, I-V characterization |
| **Current Source** | Current (-3 to +3A) | Voltage | Material characterization |
| **Four-Point Probe** | Current | Voltage | Sheet resistance, resistivity, conductivity |
| **I-V Sweep** | Voltage or Current | Current or Voltage | Diode/device curves, hysteresis, breakdown |
| **Van der Pauw** | Current | Voltage | Sheet resistance + resistivity on arbitrary-shape samples (ASTM F76) |

### Four-Point Probe

- Sheet resistance (Rs), resistivity, and conductivity calculated in real time
- **True 4-wire sense path** — outer probes carry current, inner probes measure voltage via the Keithley's Sense terminals (`:SYST:RSEN ON`). Bench-verified against a Signatone S-302 probe head; see [`tests/hardware/rsen_diagnostic.py`](tests/hardware/rsen_diagnostic.py)
- **ASTM F84-02 correction factors** — geometry-aware F₂ from Table 3 (circles) and the Smits 1958 table (squares, rectangles L/W ∈ {2, 3, 4}), thickness correction F(w/S) from Appendix X1.1 (valid out to w/S = 2.0), optional temperature correction F_T from Table 5 for n-/p-type silicon. Activated automatically when the user supplies a finite diameter D, non-circle geometry, or temperature + dopant
- **Multi-spot tracking** — save measurements at multiple probe positions, compare uniformity
- **Live histogram** of Rs distribution
- **Current reversal (delta mode)** — alternates +I/−I to cancel thermoelectric EMF; CSV exports include per-polarity V₊, V₋, R_f, R_r columns (F84 §13.1 diagnostic)
- **Probe safety envelope** — configurable warn / hard-stop power thresholds (default 10 mW / 100 mW). A pre-flight check refuses to start if the configured worst-case `I_source × V_compliance` exceeds the hard stop; a runtime check aborts the run and disables output if measured `V × I` exceeds it. Sized for tungsten-carbide tips (Signatone SP4 family) and conservative for thin-film / conductive-polymer samples where local Joule heating can damage the sample before the probe
- Legacy K · α · (V/I) path retained for non-Si materials and custom calibration factors (e.g. NIST-traceable reference standards)
- Inter-spot uniformity statistics in export

### Van der Pauw

- **ASTM F76-08 Method A** sheet-resistance and resistivity for arbitrary-shape, hole-free samples with four periphery contacts (numbered 1–4 counter-clockwise)
- Walks the user through F76's 4 cabling geometries one at a time; current reversal (+I then −I) is automated at each geometry so thermal-EMF offsets cancel cleanly
- Solves F76 Fig. 5's implicit `f(Q)` equation `(Q-1)/(Q+1) = (f/ln 2)·arccosh{(1/2)·exp(ln 2 / f)}` numerically (hand-rolled bisection; no scipy dep)
- **F76 §11.1 homogeneity gate** — automatically flags samples where ρ_A and ρ_B disagree by more than 10%
- Per-geometry resistance bar chart visualizes uniformity; bars are color-coded by deviation from the mean (green/orange/red)
- Schematic diagram of the sample + lead routing updates as the user advances through the protocol; filmstrip preview of all 4 geometries makes the cyclic CCW rotation pattern obvious
- 37 unit tests pin values directly to F76 Section 11; bench-verified on a Keithley 2420 against a 100 µm conductive foil (1.4% asymmetry, F76 gate passes 7× over)

### I-V Sweep

- Hardware staircase sweep using the Keithley sweep engine (precise inter-step timing via the instrument's trigger model)
- Source voltage or current with configurable start, stop, step, and per-step delay
- Sweep directions: **up**, **down**, or **up-down** (forward + reverse for hysteresis curves)
- Live X-Y I-V plot (separate canvas, not time-series)
- Per-point NPLC and compliance limits

### Instrument Optimizations

- **Hardware averaging filter** -- repeat or moving average, 1-100 readings, runs on the Keithley itself (`:SENS:AVER`)
- **Auto zero control** -- `on` (accurate), `once` (fast), or `off` (fastest, drifts); ~3x speed boost in fast mode
- **Offset-compensated ohms** -- resistance mode option that cancels thermoelectric EMF in low-R DUTs
- **Cable null** -- one-button measure-and-subtract of cable/lead resistance (software-side reference)
- **Auto source delay** -- lets the instrument pick the optimal post-source settling time (`:SOUR:DEL:AUTO ON`)
- **Non-concurrent functions** -- `:SENS:FUNC:CONC OFF` for cleaner readings on the 2400 series
- **High-impedance output-off** -- `:OUTP:SMOD HIMP` protects the DUT when the output is disabled

### Engineering Notation Input

Type natural lab notation instead of raw decimals:
- `1mA` instead of `0.001000 A`
- `100uA` or `100uA` instead of `0.000100 A`
- `10mV` instead of `0.010 V`

The live readout displays in engineering notation too: `V: 2.830 mV  I: 1.000 mA  R: 2.830 Ohm`

### Data Export

- **Dual format** -- JSON (with full metadata) + CSV (Excel-friendly) written simultaneously
- **Crash recovery** -- periodic checkpoints saved as `.json.tmp`, recoverable after power loss
- **4PP summary export** -- per-spot breakdown with inter-spot uniformity RSD
- Configurable auto-save interval

### Instrument Safety

- Compliance monitoring via Keithley STAT word bit 3 + threshold fallback
- Non-blocking compliance warnings (status bar flash, no modal popup spam)
- "Test Connection" button on every tab for pre-flight verification
- Configurable stop-on-compliance
- System sleep prevention during long measurements

### UI

- Live numeric readout (large font) on all tabs
- Real-time matplotlib plots with interactive toolbar
- Resizable panels via splitters
- Tooltips on every setting explaining what it does and typical values
- Scroll-wheel protection on all spinboxes
- Tab switching allowed during measurement (read-only)
- "Run until stopped" checkbox on timed modes
- Custom event markers with text labels (press M)
- Multi-user profiles with per-user settings

## Installation

### Requirements

- Python 3.9+
- PyQt5, PyVISA, NumPy, Matplotlib (installed automatically)
- A VISA backend if you want to talk to real hardware (see below)

### Setup

```bash
git clone https://github.com/PEEKPerformer/ResistaMet-GUI.git
cd ResistaMet-GUI
pip install -e .
resistamet-gui              # real instrument (needs VISA backend)
resistamet-gui --simulate   # in-package simulator, no hardware
```

`pip install -e .` reads [`pyproject.toml`](pyproject.toml) and registers the `resistamet-gui` console command. You can also run `python resistamet-gui.py` from the repo root.

#### VISA backend (real-hardware only)

If you launch without `--simulate` you need a VISA backend so PyVISA can reach the instrument:

- **NI-VISA** ([download](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html)) — needed for NI GPIB adapters; **Windows and Linux only**. NI dropped macOS support after NI-VISA 18.5 (2020); on Apple Silicon the legacy installer will not run at all.
- **`pyvisa-py`** (`pip install pyvisa-py`) — pure-Python backend; the recommended path on macOS. Reportedly works with Prologix USB-GPIB adapters and serial sourcemeters, but I haven't tested either configuration here — only NI-VISA + a Keithley 2420 on Windows. Reports welcome.

If neither is installed and you're not using `--simulate`, **Test Connection** will fail with `ValueError: Could not locate a VISA implementation`.

#### Linux system packages (PyQt5 runtime)

Headless Linux distributions (and many CI images) don't ship the X11/Qt platform shared libraries that PyQt5 dynamically loads. On Debian/Ubuntu:

```bash
sudo apt-get install -y \
    libegl1 libxkbcommon-x11-0 libdbus-1-3 \
    libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
    libxcb-sync1 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1
```

(This is the same list our CI uses — see [`.github/workflows/test.yml`](.github/workflows/test.yml).)

## Quick Start

1. Launch and create a user profile
2. Click **Test Connection** to verify instrument communication
3. Enter a sample name
4. Set source level and compliance (type `1mA`, `5V`, etc.)
5. Click **Start**

### Four-Point Probe Workflow

1. Set source current, probe spacing, and thickness
2. Start measurement -- readings appear in table with live histogram
3. Click **Save Spot** to archive current position's stats
4. Move probe to next position, repeat
5. After all spots: histogram shows bar chart of Rs uniformity
6. Click **Export Summary** for full report

### Delta Mode (Thermoelectric Cancellation)

1. In the 4PP tab, expand **Advanced**
2. Check **Current Reversal (Delta Mode)**
3. Set settling time (default 0.1s between polarity flips)
4. Each reading now alternates +I/-I, reporting V_delta = (V+ - V-) / 2

## Project Structure

- `resistamet_gui/` — application package. Core modules: `calculations.py` (pure F84 / Smits formulas), `instrument.py` (Keithley VISA wrapper), `workers.py` (QThread measurement loop), `data_export.py` (dual JSON+CSV with crash recovery), `_simulator.py` (in-package Keithley simulator), and `ui/` (main window, dialogs, canvas, widgets).
- `tests/` — pytest suite. Unit/integration tests for every module, `test_gui_smoke.py` for Qt widget lifecycle, `test_e2e_simulator.py` for end-to-end UI-to-CSV pipeline, `test_workers.py::TestSCPIContract` for per-mode SCPI command assertions, `tests/fakes/` (the fake instrument), `tests/fixtures/scpi_traces*/` (captured hardware traces), `tests/hardware/` (real-instrument tests gated by `RESISTAMET_HARDWARE_ADDR`).
- `docs/sim_fidelity.md` — simulator validation methodology and intentional gaps.
- `scripts/community_capture.py` — self-contained tool for contributors to capture SCPI traces from other 2400-family models.

## Testing

```bash
# Unit + integration suite (pytest.ini ignores the e2e file by default)
QT_QPA_PLATFORM=offscreen pytest tests/ -v

# End-to-end suite (drives every tab through the in-package simulator,
# asserts recorded values against Ohm's law on a known fake DUT)
QT_QPA_PLATFORM=offscreen pytest tests/test_e2e_simulator.py -v

# Unit tests only (no Qt dependency)
pytest tests/ -v --ignore=tests/test_gui_smoke.py --ignore=tests/test_e2e_simulator.py
```

The e2e suite runs in its own pytest invocation because it leaves process-wide state (a `pyvisa.ResourceManager` monkey-patch and a live `QApplication`) that interacts poorly with module-scoped fixtures from earlier test files. CI runs both invocations in sequence — see `.github/workflows/test.yml`.

## Instrument Compatibility

Hardware-validated (29 SCPI fixtures + 6 documented quirks, three DUT decades):
- **Keithley 2420** (3 A model, firmware C30) — primary capture source
- **Keithley 2400** (1 A model, firmware C30) — cross-model validation, 29/29 pass

The production code identifies the connected model from `*IDN?` against a static specification table covering the 2400 / 2401 / 2410 / 2420 / 2425 / 2430 / 2440 / 2450 family and surfaces the matching source/measure envelope at connect time. The 2400-family SCPI surface is largely uniform, so the other models in that table should work in principle, but **only the 2400 and 2420 are hardware-validated in-house**. If your model isn't yet captured, see "Help validate cross-model fidelity" below — that's how the table gets filled in honestly.

### Help validate cross-model fidelity

If you have a different Keithley 2400-family instrument (2400/2401/2410/
2420/2425/2430/2440/2450) and a precision reference resistor in 4-wire
Kelvin (100 Ω, 10 kΩ, or 1 MΩ recommended), please consider running:

```bash
pip install pyvisa pyvisa-py
python scripts/community_capture.py
```

The script auto-detects the GPIB instrument, runs a polarity sanity
check, captures a small set of representative SCPI traces, and prints
instructions for opening an issue with the output zip attached. Each
accepted submission becomes one row of cross-model validation in
`tests/fixtures/scpi_traces_community/` and runs in CI on every push,
helping ensure the simulator faithfully reproduces every supported
instrument. See [`docs/sim_fidelity.md`](docs/sim_fidelity.md) for the
full validation methodology.

## Version History

See [CHANGELOG.md](CHANGELOG.md) for the full version history.

## Citation

If you use ResistaMet GUI in your research, please cite:

```
Ferland, B. (2026). ResistaMet GUI: An Open-Source Electrical Measurement Suite
for Keithley Sourcemeters [Software]. Zenodo. https://doi.org/10.5281/zenodo.19919751
```

The DOI above is the *concept DOI* and always resolves to the latest archived version. Zenodo also provides a per-version DOI for citing a specific release. A `CITATION.cff` is included in the repository for machine-readable citation metadata.

## Contributing

Contributions are welcome. Please see:

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — bug reports, feature requests, development setup, and the cross-model trace contribution path.
- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) — community guidelines.
- [Issue templates](.github/ISSUE_TEMPLATE) — bug report, feature request, and cross-model SCPI-trace submission.

The fastest way to help right now is to run [`scripts/community_capture.py`](scripts/community_capture.py) on any Keithley 2400-family instrument other than the 2400 and 2420 already validated in-house — see the "Help validate cross-model fidelity" section above.

## License

MIT License — see [`LICENSE.md`](LICENSE.md).

# ResistaMet GUI

Open-source graphical interface for electrical characterization with Keithley 2400-family sourcemeters.

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19919751.svg)](https://doi.org/10.5281/zenodo.19919751)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

![ResistaMet GUI — four-point-probe tab](screenshots/04_four_point_probe.png)

## What it does

Six measurement modes on the same Keithley 2400-family instrument, all in one point-and-click GUI:

| Mode | Sources | Measures | Typical use |
|------|---------|----------|----|
| **Resistance** | Current | Resistance | 2- or 4-wire resistance, long-duration logging |
| **Voltage Source** | Voltage | Current | Bias stress, chronoamperometry, electrochemistry |
| **Current Source** | Current | Voltage | Material characterization |
| **Four-Point Probe** | Current | Voltage | Sheet resistance + resistivity (ASTM F84) |
| **I-V Sweep** | Voltage *or* Current | Current *or* Voltage | Diode / device curves, hysteresis |
| **Van der Pauw** | Current | Voltage | Sheet resistance on arbitrary-shape samples (ASTM F76) |

Source / measure envelopes vary by model — see [Installation → Instrument compatibility](installation.md#instrument-compatibility) for the full per-model table. Per-reading instrument uncertainty from the Keithley datasheet propagates through every derived quantity (sheet resistance, resistivity, conductivity).

The live readout colors V/I/R/P labels per channel using the [Wong colorblind-safe palette](https://www.nature.com/articles/nmeth.1618) (Nature Methods, 2011) — blue V, green I, vermillion R, orange P — with the `± σ` portion dimmed so the main number stays the visual anchor.

## Try it now (no instrument required)

If you already have Python:

```bash
git clone https://github.com/PEEKPerformer/ResistaMet-GUI.git
cd ResistaMet-GUI
pip install -e .
resistamet-gui --simulate
```

The `--simulate` flag launches the full GUI against an in-package Keithley 2400-family simulator. Every measurement mode works end-to-end with no NI-VISA, no pyvisa-py, and no GPIB hardware. The simulator is byte-validated against captured hardware traces — see [Simulator Fidelity](sim_fidelity.md).

If you don't have Python, the latest GitHub release has a **standalone Windows `.exe`** — see [Installation → Windows .exe](installation.md#windows-no-python-required).

Optional flags:

- `--sim-resistance 1000` — advertise a 1 kΩ DUT (default 100 Ω)
- `--sim-model 2410` — advertise a different Keithley model (default 2420)
- `--sim-noise-rsd 1e-3` — Gaussian noise RSD applied to the measured side of each reading (default 0.0 = perfect Ohm's law)

## Documentation map

| Page | Purpose |
|---|---|
| [Installation](installation.md) | Two install paths (Python source, Windows .exe), VISA backends, Linux Qt deps, per-model envelope |
| [Quick Start](quickstart.md) | First-measurement walkthrough, per-mode workflows, useful inputs |
| [Concepts](concepts.md) | Plain-English glossary for SMU terms (NPLC, compliance, auto-zero, Enhanced R, 4PP, vdP) |
| [Settings](settings.md) | Settings dialog tour with per-knob explanation and defaults |
| [Data Outputs](outputs.md) | CSV / HDF5 / legacy JSON reference, every column for every mode |
| [Troubleshooting](troubleshooting.md) | Common errors and how to resolve them |
| [Simulator Fidelity](sim_fidelity.md) | What the in-package fake covers, what it doesn't, and how it's validated |
| [Citation](citation.md) | BibTeX, DOIs, downstream publications |

## Source, issues, contributions

Source: [PEEKPerformer/ResistaMet-GUI](https://github.com/PEEKPerformer/ResistaMet-GUI).

Bug reports, feature requests, and SCPI-trace contributions are welcome — see [CONTRIBUTING.md](https://github.com/PEEKPerformer/ResistaMet-GUI/blob/main/CONTRIBUTING.md).

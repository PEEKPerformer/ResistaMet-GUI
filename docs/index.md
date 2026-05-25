# ResistaMet GUI

Open-source graphical interface for electrical characterization with Keithley 2400-family sourcemeters.

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19919751.svg)](https://doi.org/10.5281/zenodo.19919751)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

![ResistaMet GUI — four-point-probe tab](screenshots/04_four_point_probe.png)

## What it does

Six measurement modes for the same Keithley 2400/2450-family instrument, all in one point-and-click GUI:

| Mode | Sources | Measures | Use case |
|------|---------|----------|----------|
| **Resistance** | Current (up to 3 A) | Resistance | 2-/4-wire resistance, long-duration logging |
| **Voltage Source** | Voltage (-200 to +200 V) | Current | Bias stress, chronoamperometry, electrochemistry |
| **Current Source** | Current (-3 to +3 A) | Voltage | Material characterization |
| **Four-Point Probe** | Current | Voltage | Sheet resistance + resistivity (ASTM F84) |
| **I-V Sweep** | Voltage or Current | Current or Voltage | Diode / device curves, hysteresis |
| **Van der Pauw** | Current | Voltage | Sheet resistance on arbitrary-shape samples (ASTM F76) |

Per-reading instrument uncertainty from the Keithley datasheet propagates all the way through to derived quantities like sheet resistance and resistivity.

## Try it now (no instrument required)

```bash
git clone https://github.com/PEEKPerformer/ResistaMet-GUI.git
cd ResistaMet-GUI
pip install -e .
resistamet-gui --simulate
```

The `--simulate` flag launches the full GUI against an in-package Keithley 2400-family simulator. Every measurement mode works end-to-end with no NI-VISA, no pyvisa-py, and no GPIB hardware. The simulator is the same one tests validate byte-equivalent against captured hardware traces — see [Simulator Fidelity](sim_fidelity.md).

Optional flags:

- `--sim-resistance 1000` — advertise a 1 kΩ DUT
- `--sim-model 2410` — advertise a different Keithley model

## Where next

- **[Installation](installation.md)** — full install paths including VISA backends and Linux Qt deps
- **[Quick Start](quickstart.md)** — first-measurement walkthrough, per-mode workflows
- **[Simulator Fidelity](sim_fidelity.md)** — what the in-package fake covers, what it intentionally doesn't, and how it's validated against hardware
- **[Citation](citation.md)** — citation block for papers using ResistaMet GUI

## Source code, issues, contributions

The full source lives on GitHub: [PEEKPerformer/ResistaMet-GUI](https://github.com/PEEKPerformer/ResistaMet-GUI).

Bug reports, feature requests, and SCPI-trace contributions are welcome — see [CONTRIBUTING.md](https://github.com/PEEKPerformer/ResistaMet-GUI/blob/main/CONTRIBUTING.md).

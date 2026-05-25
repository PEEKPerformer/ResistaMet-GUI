# Installation

## Requirements

- Python 3.9 or later
- PySide6, PyVISA, NumPy, Matplotlib (installed automatically)
- A VISA backend if you want to talk to real hardware (see below)

## Setup

```bash
git clone https://github.com/PEEKPerformer/ResistaMet-GUI.git
cd ResistaMet-GUI
pip install -e .
resistamet-gui              # real instrument (needs VISA backend)
resistamet-gui --simulate   # in-package simulator, no hardware
```

`pip install -e .` reads [`pyproject.toml`](https://github.com/PEEKPerformer/ResistaMet-GUI/blob/main/pyproject.toml) and registers the `resistamet-gui` console command. You can also run `python resistamet-gui.py` from the repo root.

## VISA backend (real-hardware only)

If you launch without `--simulate` you need a VISA backend so PyVISA can reach the instrument:

=== "NI-VISA (Windows / Linux)"

    [Download from NI](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html). Needed for NI GPIB adapters. Windows and Linux only — NI dropped macOS support after NI-VISA 18.5 (2020); on Apple Silicon the legacy installer will not run at all.

=== "pyvisa-py (cross-platform, recommended on macOS)"

    ```bash
    pip install pyvisa-py
    ```

    Pure-Python backend. Reportedly works with Prologix USB-GPIB adapters and serial sourcemeters, but only NI-VISA + a Keithley 2420 on Windows is verified in-house. Reports welcome.

If neither is installed and you're not using `--simulate`, **Test Connection** will fail with `ValueError: Could not locate a VISA implementation`.

## Linux system packages (PySide6 runtime)

Headless Linux distributions (and many CI images) don't ship the X11/Qt platform shared libraries that PySide6 dynamically loads. On Debian/Ubuntu:

```bash
sudo apt-get install -y \
    libegl1 libxkbcommon-x11-0 libdbus-1-3 \
    libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
    libxcb-sync1 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1
```

This is the same list our CI uses — see [`.github/workflows/test.yml`](https://github.com/PEEKPerformer/ResistaMet-GUI/blob/main/.github/workflows/test.yml).

## Instrument compatibility

Hardware-validated:

- **Keithley 2420** (3 A model, firmware C30) — primary capture source, 29 SCPI fixtures + 6 documented quirks across three DUT decades
- **Keithley 2400** (1 A model, firmware C30) — cross-model validation, 29/29 pass

The production code identifies the connected model from `*IDN?` against a static specification table covering the 2400 / 2401 / 2410 / 2420 / 2425 / 2430 / 2440 / 2450 family and surfaces the matching source/measure envelope at connect time. The 2400-family SCPI surface is largely uniform, so the other models in that table should work in principle, but only the 2400 and 2420 are hardware-validated in-house.

### Help validate cross-model fidelity

If you have a different Keithley 2400-family instrument and a precision reference resistor in 4-wire Kelvin (100 Ω, 10 kΩ, or 1 MΩ recommended), please consider running:

```bash
pip install pyvisa pyvisa-py
python scripts/community_capture.py
```

The script auto-detects the GPIB instrument, runs a polarity sanity check, captures a small set of representative SCPI traces, and prints instructions for opening an issue with the output zip attached. Each accepted submission becomes one row of cross-model validation in `tests/fixtures/scpi_traces_community/` and runs in CI on every push. See [Simulator Fidelity](sim_fidelity.md) for the full validation methodology.

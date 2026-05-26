# Installation

Two paths depending on whether you want to develop ResistaMet GUI or just run it.

## Windows — no Python required

Each tagged release attaches a standalone `ResistaMet.exe` to the GitHub release page. It bundles Python, PySide6, and all dependencies — double-click and run.

1. Go to the [releases page](https://github.com/PEEKPerformer/ResistaMet-GUI/releases/latest)
2. Download **`ResistaMet.exe`** from the Assets section
3. Make a new folder on your desktop named `ResistaMet` and move the `.exe` into it (see the warning below for why this matters)
4. Open the folder and double-click `ResistaMet.exe` to launch

You can also run it from a Command Prompt to pass flags:

```cmd
ResistaMet.exe --simulate
ResistaMet.exe --version
```

The `.exe` is built by GitHub Actions on every tag push (see [`build.yml`](https://github.com/PEEKPerformer/ResistaMet-GUI/blob/main/.github/workflows/build.yml)) using PyInstaller, then smoke-tested with `--version` before being attached to the release. ~30 MB for the v1.11.0+ Qt6 binaries.

!!! warning "Give the .exe its own folder"
    `config.json` and the default `measurement_data/` directory are created **next to wherever the .exe is launched from** (the current working directory). Logs are the only thing that go to a per-user location (`C:\Users\<you>\.resistamet\logs\`).

    Practical consequences:

    - **Drop `ResistaMet.exe` into its own folder on your desktop** (e.g. `Desktop\ResistaMet\`) before first launch. Don't run it from `Downloads\`, and don't put it in `Program Files\` (no write permission there).
    - If you create a shortcut to the `.exe`, make sure its **Start in** field points at the folder containing the `.exe`. Launching from different shortcuts with different Start-in directories will give you different `config.json` files in different places.
    - When you upgrade to a new release, drop the new `ResistaMet.exe` into the same folder as the old one. Your settings and data will be picked up automatically.
    - The **Data Directory** setting (Settings → File) accepts an absolute path if you want output to live somewhere else (e.g. a OneDrive folder, a NAS mount). The default is relative to the .exe's launch dir.

!!! info "Why Windows-only for the bundled binary?"
    NI-VISA — the standard driver for Keithley GPIB adapters — runs on Windows and Linux but was dropped on macOS after NI-VISA 18.5 (2020). Most lab PCs running 2400-series instruments are Windows, so that's what we ship the .exe for. macOS and Linux users should use the source install below.

## Python source install (all platforms)

### Requirements

- Python 3.9 or later
- PySide6, PyVISA, NumPy, Matplotlib, pyqtgraph (installed automatically by `pip install -e .`)
- A VISA backend if you want to talk to real hardware (see [VISA backend](#visa-backend-real-hardware-only) below)

### Setup

```bash
git clone https://github.com/PEEKPerformer/ResistaMet-GUI.git
cd ResistaMet-GUI
pip install -e .
resistamet-gui              # real instrument (needs VISA backend)
resistamet-gui --simulate   # in-package simulator, no hardware
```

`pip install -e .` reads [`pyproject.toml`](https://github.com/PEEKPerformer/ResistaMet-GUI/blob/main/pyproject.toml) and registers `resistamet-gui` on your PATH. You can also run `python resistamet-gui.py` from the repo root if you prefer to bypass the entry-point.

### Optional: HDF5 export

CSV is the default output format. To also enable HDF5 (`.h5`) export, install `h5py`:

```bash
pip install h5py
```

Without `h5py`, the HDF5 option in Settings → Output is disabled with a tooltip explaining why. See [Data Outputs](outputs.md) for the format reference.

## VISA backend (real-hardware only)

If you launch without `--simulate` you need a VISA backend so PyVISA can reach the instrument.

=== "NI-VISA (Windows / Linux)"

    [Download from NI](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html). Needed for NI GPIB adapters. Windows and Linux only — NI dropped macOS support after NI-VISA 18.5 (2020); on Apple Silicon the legacy installer will not run at all.

    This is the bench-validated path used by the development lab (Windows 10/11 + NI-USB-GPIB-HS adapter + Keithley 2420).

=== "pyvisa-py (cross-platform, recommended on macOS)"

    Pure-Python VISA backend.

    ```bash
    pip install pyvisa-py
    ```

    Reported to work with Prologix USB-GPIB adapters and serial-port sourcemeters, but only NI-VISA + Keithley 2420 on Windows is verified in-house. Bench reports from other configurations are welcome via [GitHub issues](https://github.com/PEEKPerformer/ResistaMet-GUI/issues).

If neither backend is installed and you're not using `--simulate`, the **Test Connection** button on each tab will fail with `ValueError: Could not locate a VISA implementation`. See [Troubleshooting → VISA backend not found](troubleshooting.md#visa-backend-not-found-valueerror-could-not-locate-a-visa-implementation).

## Linux system packages (PySide6 runtime)

Headless Linux distributions (and many CI images) don't ship the X11/Qt platform shared libraries that PySide6 dynamically loads. On Debian/Ubuntu:

```bash
sudo apt-get install -y \
    libegl1 libxkbcommon-x11-0 libdbus-1-3 \
    libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
    libxcb-sync1 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1
```

This matches the list CI uses — see [`.github/workflows/test.yml`](https://github.com/PEEKPerformer/ResistaMet-GUI/blob/main/.github/workflows/test.yml).

## Instrument compatibility

ResistaMet GUI identifies the connected model from its `*IDN?` response and surfaces the matching source/measure envelope at connect time. The full table:

| Model | Max source V | Max source I | Max power | Notes |
|---|---|---|---|---|
| **2400** | ±200 V | ±1.05 A | 22 W | Original 2400 SCPI surface |
| **2401** | ±20 V | ±1.05 A | 22 W | Low-voltage variant of the 2400 |
| **2410** | ±1100 V | ±1.05 A | 22 W | High-voltage; special handling above 100 V |
| **2420** | ±60 V | ±3.05 A | 22 W | Bench-primary; 29 SCPI fixtures captured |
| **2425** | ±100 V | ±3.05 A | 22 W |  |
| **2430** | ±100 V | ±3.05 A | 22 W | Pulse mode supports 10 A (5 W avg) |
| **2440** | ±40 V | ±5.05 A | 22 W | Highest-current model in the family |
| **2450** | ±200 V | ±1.05 A | 22 W | Touchscreen successor; TSP+SCPI surface |

Source: per-model `ModelSpec` table in [`resistamet_gui/instrument.py`](https://github.com/PEEKPerformer/ResistaMet-GUI/blob/main/resistamet_gui/instrument.py), grounded in the Keithley datasheet.

### What's bench-validated vs documented

- **Keithley 2420** (3 A model, firmware C30) — primary capture source, 29 SCPI fixtures + 6 documented quirks across three DUT decades (100 Ω, 10 kΩ, 1 MΩ)
- **Keithley 2400** (1 A model, firmware C30) — cross-model validation, all 29 fixtures pass

The other models in the table should work — the 2400-family SCPI surface is largely uniform — but **only the 2400 and 2420 are bench-validated in-house**. If your model isn't yet captured, see [Help validate cross-model fidelity](#help-validate-cross-model-fidelity) below.

### Help validate cross-model fidelity

If you have a different Keithley 2400-family instrument and a precision reference resistor in 4-wire Kelvin (100 Ω, 10 kΩ, or 1 MΩ recommended), please consider running:

```bash
pip install pyvisa pyvisa-py
python scripts/community_capture.py
```

The script auto-detects the GPIB instrument, runs a polarity sanity check, captures a small set of representative SCPI traces, and prints instructions for opening an issue with the output zip attached. Each accepted submission becomes one row of cross-model validation in `tests/fixtures/scpi_traces_community/` and runs in CI on every push. See [Simulator Fidelity](sim_fidelity.md) for the full validation methodology.

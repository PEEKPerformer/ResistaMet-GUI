# Troubleshooting

Common failures and how to resolve them. If you hit a message not listed here, please [open an issue](https://github.com/PEEKPerformer/ResistaMet-GUI/issues) with the exact text.

## Connection failures

### "Instrument at … was not detected"

**Cause:** The configured GPIB address doesn't match any instrument PyVISA can see.

**Fix:**

1. Power on the Keithley (front panel lit, beep on boot).
2. Check the GPIB cable is firmly seated on both ends.
3. Confirm the instrument's GPIB address on the front panel: **MENU → Comm → GPIB**. The default factory address is 24.
4. In ResistaMet GUI: **Settings → Measurement → Detect Devices** to scan for what PyVISA actually sees, then pick the right address from the list.

ResistaMet GUI auto-escalates this: when a measurement fails to connect with an address-like error, the friendly error dialog is followed by a GPIB-selector popup so you can pick the right address in one click. The new address is stored per-host and persists across sessions.

### "NI-VISA is not supported on macOS"

**Cause:** Running on macOS without `pyvisa-py` installed. NI-VISA was dropped on macOS after NI-VISA 18.5 (2020).

**Fix:** Either run on the lab Windows PC (recommended for routine use), or:

```bash
pip install pyvisa-py
```

`pyvisa-py` is reportedly compatible with Prologix USB-GPIB adapters but is not bench-verified in-house — your mileage may vary.

### "NI-VISA isn't installed on this PC" (Windows / Linux)

**Cause:** PyVISA can't find a VISA library to load.

**Fix:** Download and install NI-VISA from [ni.com/visa](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html), reboot, then try again. The installer is ~1 GB; coffee-break-sized.

### "Timeout while talking to the instrument"

**Cause:** PyVISA opened the connection but the instrument didn't respond to a SCPI command within ~5 s. Usually means the GPIB address mismatched (cable plugged into the right slot, but configured address points at a different bus) or the instrument is hung.

**Fix:**

1. Power-cycle the Keithley (off / wait 10 s / on).
2. If the timeout persists, double-check the GPIB address against the front panel.
3. If the issue is reproducible after fresh power-cycle, capture the full stderr and open an issue.

### "The instrument … is busy"

**Cause:** Another program (Kickstart, LabVIEW, an older ResistaMet window) has the instrument open and PyVISA can't acquire it.

**Fix:** Close the other program. On Windows, **Task Manager → Details** can confirm — look for `KickStart.exe`, `LabVIEW.exe`, or another `python.exe` holding a VISA handle.

### "VISA backend not found": `ValueError: Could not locate a VISA implementation`

**Cause:** You launched without `--simulate` but no VISA backend is installed. See [Installation → VISA backend](installation.md#visa-backend-real-hardware-only).

**Fix:** Install either NI-VISA (Windows/Linux) or `pip install pyvisa-py` (cross-platform). Or, if you're just kicking the tires and have no hardware, relaunch with `--simulate`.

## Compliance and reading anomalies

### Compliance hit (readings show `9.91e37`)

**Symptom:** A V or I reading suddenly shows `9.91e37` (or `9.9e37`), the `compliance` column in the CSV gets flagged, and the status bar reads `Compliance`.

**Cause:** The measured quantity reached the compliance limit and the Keithley clamped it. `9.91e37` is the magic sentinel value the 2400 family returns for an over-range / compliance condition; ResistaMet detects this via the SCPI STAT word's bit 3.

**Fix:** Raise the compliance setting for that channel, or lower the source level. If you intended to discover the compliance limit (e.g. you're tracing a diode breakdown), this is the expected behavior — just be aware that the rows with the sentinel are not real measurements.

### 4PP run aborts immediately with "Power envelope exceeded"

**Cause:** The pre-flight check refused to start because the worst-case product `I_source × V_compliance` exceeds `fpp_power_stop_w` (default 100 mW). This protects tungsten-carbide probe tips from melting.

**Fix:** Either lower `I_source` (typical 100 µA is safe for unknown films), lower `V_compliance`, or — if you genuinely want to run with more power — raise `fpp_power_stop_w` in the 4PP tab Advanced section. The warn threshold (`fpp_power_warn_w`, default 10 mW) is non-blocking; only the stop threshold aborts.

## Plot / display

### Live plot is choppy or laggy

**Cause:** Sampling rate × plotting overhead exceeds what your CPU + Qt can keep up with. Usually only an issue above ~50 Hz on slower machines.

**Fix:** **Settings → Display → Enable Real-time Plots → False**. Data still streams to disk; the plot just stops updating. The buffer remains intact, so when you click Stop you can re-enable plots to inspect the full trace.

### "Run until stopped" trace is silently truncated

**Cause:** You have `Data Buffer Size` set to a finite cap (legacy default was 1000). Once the buffer fills, the oldest points are dropped from the *plot only* — the CSV still has every row.

**Fix:** **Settings → Display → Data Buffer Size → 0** (or "Unlimited"). pyqtgraph downsamples on render so even a 17-hour run stays smooth. The new default is unlimited; existing configs override unless you reset.

## Data export

### Settings dialog grays out the HDF5 option

**Cause:** `h5py` isn't installed, and ResistaMet refuses to let you select a backend that will crash at runtime.

**Fix:** `pip install h5py`, restart ResistaMet GUI.

### `.csv.gz` files can't be opened in Excel

**Cause:** Excel doesn't open gzipped CSVs directly. By default ResistaMet doesn't gzip — but you may have turned compression on.

**Fix:** Either:

- **Settings → Output → Compression → Never** to stop producing `.csv.gz` going forward, or
- Decompress an existing file with `gunzip path/to/file.csv.gz` (macOS / Linux) or 7-Zip (Windows), or
- `pd.read_csv("file.csv.gz", comment="#")` works directly in pandas if you only need it programmatically.

### CSV column header is missing in legacy mode

**Cause:** You're using the `csv+legacy_json` output format. In legacy mode the CSV has only a header row and data; metadata lives entirely in the `.json` file. This is intentional — the legacy format is unchanged from pre-2.0 ResistaMet for downstream pipeline compatibility.

**Fix:** Switch to `csv` (default) for the v2.0 unified-metadata format. Or use both — pipelines that need the JSON keep working, and human analysts get the metadata in the CSV header.

## Simulator

### `--simulate` works but I want to test against a different DUT

**Fix:** Pass `--sim-resistance R_ohms` to advertise a different DUT. Examples:

```bash
resistamet-gui --simulate --sim-resistance 1000      # 1 kΩ DUT
resistamet-gui --simulate --sim-resistance 1e6        # 1 MΩ DUT
resistamet-gui --simulate --sim-model 2410            # advertise as a 2410
resistamet-gui --simulate --sim-noise-rsd 1e-3       # add 0.1% Gaussian noise
```

By default the simulator returns perfect Ohm's-law readings (`--sim-noise-rsd 0.0`); pass a non-zero RSD to inject Gaussian noise on the measured side of each reading. See [Simulator Fidelity](sim_fidelity.md) for what's modeled and what isn't.

### Simulator behaves slightly differently from my real Keithley

**Cause:** Most likely. The in-package simulator is reverse-engineered from captured SCPI traces on a specific 2420 + 2400 pair (firmware C30), not built from spec. Some firmware-version-dependent behavior is intentionally simplified.

**Fix:** See [Simulator Fidelity](sim_fidelity.md) for what's validated and what's known to differ. If you find a divergence we don't document, please run `scripts/community_capture.py` against your instrument and [open an issue](https://github.com/PEEKPerformer/ResistaMet-GUI/issues) with the trace attached.

## Catch-all: anything else

If the error message doesn't match anything here:

1. Check the log file at `~/.resistamet/logs/resistamet_YYYYMMDD.log` (one per UTC date, rotated at 5 MB × 3 backups) for the full traceback.
2. [Open an issue](https://github.com/PEEKPerformer/ResistaMet-GUI/issues/new/choose) with:
    - ResistaMet GUI version (`resistamet-gui --version`)
    - OS + Python version
    - Keithley model + firmware (from `*IDN?`, surfaced in the status bar after Test Connection)
    - The exact error text and the relevant log excerpt
    - The exact steps that produced the error

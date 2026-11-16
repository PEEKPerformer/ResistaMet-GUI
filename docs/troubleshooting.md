# Troubleshooting

Common failures and how to resolve them. If you hit a message not listed here, please [open an issue](https://github.com/PEEKPerformer/ResistaMet-GUI/issues) with the exact text.

## Connection failures

### No instruments detected, or the scan list has no GPIB entry

**Symptom:** **Detect Devices** (PySide6) or **Scan** (desktop app, Settings ▸ Instrument) says VISA sees no resources, or lists only serial ports.

**Cause:** Either no VISA implementation loads, or one loads that has no driver for your GPIB adapter. The second is the normal state of a Mac with NI-VISA installed, and of a PC with NI-VISA but without NI-488.2.

**Fix:** Ask the backend what it has. No GUI and no development tools needed:

```bash
python -m resistamet_gui.api --check-visa          # source install
resistamet-api --check-visa                        # the frozen backend inside the desktop app
resistamet-api --check-visa bus --visa-library @py # also list resources, through pyvisa-py
```

It prints one JSON line: which VISA library answered and its version, whether the built-in NI GPIB-USB driver can load libusb, which NI adapters are on USB, and with `bus` the resource list. [GPIB → Diagnosing](gpib.md#diagnosing-with-check-visa) explains each field. Typical readings:

- `"ok": false`: no VISA implementation at all. Install NI-VISA (Windows) or use pyvisa-py.
- `"kind": "ivi"` on a Mac: the vendor library was chosen and cannot see GPIB. Set the VISA backend to **pyvisa-py**.
- `"ni_usb": {"available": false}`: libusb or pyusb is missing (`brew install libusb`, `pip install -e ".[usb]"`).
- The adapter is under `adapters` but no `GPIB0::…::INSTR` under `resources`: the bus was scanned and nothing listened. Instrument power, cable, GPIB address on the front panel.

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

**Fix:** Either run on a Windows PC with NI-VISA and NI-488.2, or use pyvisa-py, which is installed with ResistaMet. With pyvisa-py a Mac reaches a Keithley through an NI GPIB-USB-HS and the built-in driver, over RS-232, or over Ethernet; see [GPIB and VISA backends](gpib.md) for what each needs and for what has and has not been checked on hardware. A Prologix adapter does not work for runs yet ([why](gpib.md#current-limitation-a-run-does-not-connect-through-it-yet)).

The wording of this message predates the built-in driver. If NI-VISA *is* installed on the Mac you will not see this message at all; you will see an empty scan instead (previous entry).

### "NI-VISA isn't installed on this PC" (Windows / Linux)

**Cause:** PyVISA can't find a VISA library to load.

**Fix:** Download and install NI-VISA from [ni.com/visa](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html), reboot, then try again. The installer is ~1 GB; coffee-break-sized.

### "Timeout while talking to the instrument"

**Cause:** PyVISA opened the connection but the instrument didn't respond to a SCPI command within ~5 s. Usually means the GPIB address mismatched (cable plugged into the right slot, but configured address points at a different bus) or the instrument is hung.

**Fix:**

1. Power-cycle the Keithley (off / wait 10 s / on).
2. If the timeout persists, double-check the GPIB address against the front panel.
3. If the issue is reproducible after fresh power-cycle, capture the full stderr and open an issue.

### "… is in use by another ResistaMet process"

**Symptom:** Start is refused with `GPIB0::24::INSTR is in use by another ResistaMet process`; through the API, `POST /session/start` answers 409 with that text after about 3 s.

**Cause:** The instrument lock. Every ResistaMet process (the PySide6 window, the desktop app's backend, a script) takes an operating-system file lock on the instrument address for the length of a run, so that two of them cannot interleave commands on one bus. The lock files are in `~/.resistamet/locks/` (on Windows under your user folder), one per address, for example `GPIB0__24__INSTR.lock`.

**Fix:** Find the other ResistaMet that is running and stop its run or close it: a second window, a desktop app left open, a backend that outlived its window (`resistamet-api` or `python` in the process list). The operating system drops the lock the moment its holder exits, so there is never a stale lock to clean up, and deleting the `.lock` files does nothing. A Start pressed right after a Stop waits up to 3 s for the previous run to finish its cleanup before it gives this message.

The lock only knows about ResistaMet. Another program holding the instrument shows up as the next entry instead.

### "The instrument … is busy"

**Cause:** Another program (Kickstart, LabVIEW, an older ResistaMet window) has the instrument open and PyVISA can't acquire it.

**Fix:** Close the other program. On Windows, **Task Manager → Details** can confirm — look for `KickStart.exe`, `LabVIEW.exe`, or another `python.exe` holding a VISA handle.

On a Mac or Linux PC using the built-in NI GPIB-USB driver, see also [A timeout takes about 20 s](#a-timeout-takes-about-20-s-although-5-s-was-asked) and [the hung adapter](#the-ni-gpib-usb-adapter-stops-answering).

### "VISA backend not found": `ValueError: Could not locate a VISA implementation`

**Cause:** You launched without `--simulate` but no VISA backend is installed. See [Installation → VISA backend](installation.md#visa-backend-real-hardware-only).

**Fix:** Install either NI-VISA (Windows/Linux) or `pip install pyvisa-py` (cross-platform). Or, if you're just kicking the tires and have no hardware, relaunch with `--simulate`.

## NI GPIB-USB adapter with the built-in driver

These apply only when the pyvisa-py backend drives an NI GPIB-USB-HS through ResistaMet's own driver (macOS, Linux). They do not apply to NI-VISA on Windows.

### The NI GPIB-USB adapter stops answering

**Symptom:** Connecting fails with `GPIB-USB-HS accepted the initialisation message but never replied: the adapter is hung. Unplug it and plug it back in.`, or the first command after connecting never returns.

**Cause:** The adapter's firmware has stopped replying on its data endpoints. It still enumerates on USB and still shows up under `adapters` in `--check-visa`. Seen once on the bench (2026-09-18).

**Fix:** Unplug the adapter from USB and plug it back in. Nothing software can send recovers it; a USB reset was tried and does not. Restarting ResistaMet or the Keithley does not help either.

### A timeout takes about 20 s although 5 s was asked

**Symptom:** With the instrument off, disconnected or at the wrong address, the timeout error arrives after about 20 s, not after the 5 s ResistaMet asks for. A sweep of 11 to 30 points that times out takes about 41 s.

**Cause:** The adapter does not take a timeout in seconds. It takes a code from a fixed table, and it ends the read when that code's time runs out. The driver sends the code NI's own driver sends: the smallest nominal limit not below the timeout, so 5 s goes out as the 10 s code. How long the adapter then waits differs from one adapter to another. The driver bounds a whole read by the longer of the two measured adapters' waits for its code, so that it never gives up on a read NI's driver would still have finished. Under the 10 s code the adapter the driver has run on is the slower of the two: it ends a silent read after 20.0 s.

| Timeout asked of VISA | Code (nominal) | GPIB-USB-HS 01CEE482, this driver | GPIB-USB-HS 013CC9DF, NI's driver |
|---|---|---|---|
| up to 100 ms | 0xf9 (100 ms) | 0.13 s | 0.13 s |
| 101 to 263 ms | 0xfa (300 ms) | 0.38 s | 0.26 s |
| 264 ms to 1 s | 0xfb (1 s) | 1.25 s | 1.05 s |
| above 1 s, up to 3 s | 0xfc (3 s) | 3.75 s | 4.20 s |
| above 3 s, up to 10 s: ResistaMet's 5 s, sweeps of up to 10 points | 0xfd (10 s) | **20.0 s** | 16.78 s |
| above 10 s, up to 30 s: sweeps of 11 to 30 points | 0xfe (30 s) | 41.25 s | 33.56 s |
| longer | 0xff, 0x01, 0x02 (100 s, 300 s, 1000 s) | not measured | not measured |

The 01CEE482 figures were measured on the wire on 2026-09-21 (spec §7.3), with the adapter on a Mac and nothing to read at a Keithley 2400; its 0.13 s under 0xf9 is a session total, not a wire timing. The driver's code choice and its bound on the read were changed the next day, and the time to the error has not been re-measured through the changed driver. I-V sweeps ask for `max(10 s, 1 s per point)`.

With NI-VISA on Windows the adapter is driven by NI's software, not by this driver. The 013CC9DF column is what it does: a 5 s timeout reports after about 16.8 s, measured on the wire on 2026-09-19 and 2026-09-22.

**Fix:** None needed. The reading is not affected, only how long a failure takes to report. Pressing Stop during the wait is honored once the read returns. If the delay is a nuisance while you hunt for the right GPIB address, use **Scan** instead of repeated connection attempts.

## Compliance and reading anomalies

### Compliance hit (readings show `9.91e37`)

**Symptom:** A V or I reading suddenly shows `9.91e37` (or `9.9e37`), the `compliance` column in the CSV gets flagged, and the status bar reads `Compliance`.

**Cause:** The measured quantity reached the compliance limit and the Keithley clamped it. `9.91e37` is the magic sentinel value the 2400 family returns for an over-range / compliance condition; ResistaMet detects this via the SCPI STAT word's bit 3.

**Fix:** Raise the compliance setting for that channel, or lower the source level. If you intended to discover the compliance limit (e.g. you're tracing a diode breakdown), this is the expected behavior — just be aware that the rows with the sentinel are not real measurements.

### A four-point spot is refused: "Spot '…' is off the sample"

**Symptom:** A four-point run started with a spot position ends at once with `Spot 'edge-3' is off the sample: a probe tip is 0.42 s beyond the edge.` The run's end reason is `spot_refused`. No file is written and the instrument is never opened.

**Cause:** With the position (`x_mm`, `y_mm`), the array angle and the probe spacing as given, at least one of the four tips lies on or outside the sample outline. Distances in the message are in probe spacings `s`. The geometry factor diverges at the edge, so the run is refused before the output is turned on.

**Fix:** Check, in this order: the outline (`fpp_sample_shape` and its dimensions in **mm**; when the shape is `unbounded` the outline comes from `fpp_geometry` and `fpp_diameter_cm` in **cm**), that positions are measured from the **center** of the sample, the array angle (`fpp_array_angle_deg`, counterclockwise from +x, which is the rectangle's length direction), and `fpp_spacing_cm`. The four tips span 3 s along the array. See [Settings → Four-point sample outline](settings.md#four-point-sample-outline-and-spot-position).

A spot *near* an edge is not refused. It runs with a warning such as `Spot 'east edge' is 5.8 s from the edge: … differs … by 1.1 % (threshold 1 %). No position correction is applied.`, and the file records the size of the effect under [`spot.*`](outputs.md#spot-four-point-runs-that-carry-a-spot). Raise `fpp_edge_warn_pct` if the threshold is too strict for your work.

### 4PP run aborts immediately with "Power envelope exceeded"

**Cause:** The pre-flight check refused to start because the worst-case product `I_source × V_compliance` exceeds `fpp_power_stop_w` (default 100 mW). This protects tungsten-carbide probe tips from melting.

**Fix:** Either lower `I_source` (typical 100 µA is safe for unknown films), lower `V_compliance`, or — if you genuinely want to run with more power — raise `fpp_power_stop_w` in the 4PP tab Advanced section. The warn threshold (`fpp_power_warn_w`, default 10 mW) is non-blocking; only the stop threshold aborts.

## Desktop app

### "The measurement backend did not start"

**Symptom:** The desktop app opens on a card with this title and an error text instead of the measurement views.

**Cause:** The app's shell could not launch the Python backend or did not get its handshake. The text says which: `could not start the measurement backend (…)` (nothing to run at that path), `backend exited before its handshake` (it crashed on startup), `backend did not answer within 30 s`, or `backend handshake was not JSON`.

**Fix:** An installed build carries its own backend; reinstall if it is missing or damaged. From a source checkout the shell looks for, in order, `RESISTAMET_PYTHON`, a frozen `resistamet-api` next to the app, the repository's `.venv`, then `python3` on the PATH; the interpreter it finds needs the API extra (`pip install -e ".[api]"`). Run the same command by hand to see the traceback:

```bash
python -m resistamet_gui.api --port 0 --config config.json --no-watchdog
```

### "Backend unreachable" / "The measurement backend is not answering"

**Symptom:** The chip in the top bar turns to **Backend unreachable**, a red notice appears in the view, and Start is disabled.

**Cause:** The backend process is alive but not answering HTTP, for example because the PC has just woken from sleep and the connection has not recovered yet. The app polls every 2 s and the event stream reconnects by itself.

**Fix:** Press **Retry** in the notice.

### "The measurement backend has exited"

**Symptom:** A blocking alert with this title, the exit code (or "The process was killed"), and a **Restart backend** button.

**Cause:** The backend process ended without the app asking it to: a crash, or something killed it.

**Fix:** Look at the instrument first. A backend that dies this way never sent `:OUTP OFF`, so the output is in whatever state it was; turn it off at the front panel if it is on. Expect the run's file to end without its `# --- run completed ---` block and possibly without the rows since the last flush. Then press **Restart backend**. The newest `backend-….log` in the app's log directory ([where](desktop.md#where-things-are)) has the traceback if it was a crash. A run is not lost when only the *window* is closed; see [Desktop app → Closing the window](desktop.md#closing-the-window).

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

1. Get the full traceback. The PySide6 app writes `~/.resistamet/logs/resistamet_<date>.log` and prints the same lines to the terminal it was started from, so `resistamet-gui` from a terminal shows them live. The backend logs to its stderr (`python -m resistamet_gui.api … 2> backend.log`). An installed desktop app redirects its backend's stderr to a file per launch, `backend-<milliseconds since 1970>.log`, in the app's log directory (see [Desktop app → Where things are](desktop.md#where-things-are)), and keeps the newest ten; the **Log** panel at the bottom of the window holds the run's messages.
2. [Open an issue](https://github.com/PEEKPerformer/ResistaMet-GUI/issues/new/choose) with:
    - ResistaMet GUI version (`resistamet-gui --version`)
    - OS + Python version
    - Keithley model + firmware (from `*IDN?`, surfaced in the status bar after Test Connection)
    - The exact error text and the relevant log excerpt
    - The exact steps that produced the error

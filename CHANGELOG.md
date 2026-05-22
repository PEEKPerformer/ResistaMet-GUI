# Changelog

All notable changes to ResistaMet-GUI are documented here.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.10.0] - 2026-05-22

### Added
- **Per-range uncertainty propagation.** New `resistamet_gui/accuracy.py` carries the 2400-series datasheet's V/I/R measurement and source accuracy tables (1KW-2798-3, April 2021) for 2400/2401/2410/2420/2425/2430/2440. Active range is inferred from the reading via the Keithley's 105% overrange rule (no per-reading SCPI query). NPLC modifiers track the Speed buckets per datasheet footnotes. RSS propagation per GUM §5.1.6 Eq. 12. Five public functions: `voltage_uncertainty`, `current_uncertainty`, `resistance_uncertainty`, `voltage_source_uncertainty`, `current_source_uncertainty`.
- **Live readout shows ±σ.** Resistance and source modes render `R = 1.4690 ± 0.00068 kΩ` via a new `format_with_uncertainty(value, σ, unit)` helper — shared engineering prefix from the value, σ rounded to 2 sig figs (GUM §7.2.6), value rounded to the same decimal place so digits never imply more precision than σ justifies. Falls back to plain engineering format when σ isn't available.
- **CSV uncertainty columns** on every mode that derives R: `R_unc_ohm` on resistance, `I_unc_A` + `R_calc_unc_ohm` on source_v, `V_unc_V` + `R_calc_unc_ohm` on source_i, `V_unc_V` + `I_unc_A` on 4PP per-reading. vdP finalize metadata gains `sheet_resistance_uncertainty` and `rho_avg_uncertainty`.
- **Combined statistical + instrument uncertainty** on 4PP per-spot stats and the vdP result panel: `u_total = √(u_stat² + u_inst²)` where `u_stat = std/√N` (random component reduces with N) and `u_inst` is the per-reading instrument floor (treated as systematic). 4PP labels read `mean ± u_total (RSD x%; stat y / inst z)`. Shared pure helpers in `calculations.four_point_combined_uncertainty` / `calculations_vdp.vdp_combined_uncertainty` — GUI and worker call into the same code so the result panel and CSV metadata cannot drift.
- **Enhanced R mode** ties to the existing `res_offset_comp` checkbox (renamed "Enhanced accuracy (slower)" with an honest tooltip about the moderate-R regime). When active, σ_R comes from the datasheet's Enhanced R-accuracy column directly; otherwise V/I propagation. Falls back to propagation outside the table's 20 Ω–200 MΩ coverage. Bench-confirmed >20× tighter σ_R at R=20 Ω. CSV records `offset_compensated_ohms: true/false` for audit. **Default is now ON** because a precision-measurement tool should ship the accurate-by-default setting; fast scans opt out.
- **Human-touch-safety voltage warning** (gh #1). New `resistamet_gui/safety.py` runs a pure check; if a configured run could put >30 V (IEC 61010-1 SELV) on the leads, a modal warns at Start with a sticky "Don't show again" checkbox that flips `safety_voltage_warn_silenced` on the profile. Settings dialog has a re-enable toggle. Status bar appends "⚡ N V live" while a hazardous run is active. Warn-then-proceed — never blocks.
- **NPLC-aware sig figs** in vdP and 4PP result labels and live-readout fallback paths. NPLC ≥ 0.5 → 6 figs (6½-digit hardware), 0.05–0.5 → 5 figs, <0.05 → 4 figs.
- **Resistance-mode `FORM:ELEM`** upgraded to `VOLT,CURR,RES,STAT` so V and I are captured alongside the instrument-reported R. Enables the uncertainty propagation above with no measurable throughput cost.
- **Reproducibility metadata.** CSV metadata header now records `auto_zero`, `voltage_auto_range` / `current_auto_range` per mode, and `offset_compensated_ohms` for resistance — sufficient to recover which accuracy-spec row inference is operating against without reverse-engineering from the raw V/I.

### Changed
- **`auto_zero` moved from Settings dialog to per-tab UI control** on Resistance / Voltage Source / Current Source tabs. Re-tuning the speed/accuracy trade-off no longer requires opening Settings.
- **vdP thickness guard.** The `vdp_thickness_cm` default changed from `1.0e-4` (silently used 1 µm) to `0.0` (sentinel). vdP Start now prompts via `QInputDialog.getDouble` for thickness if unset, mirroring `_require_sample_name`. Prevents the silent `ρ = R_s × 1 µm` failure mode.
- **Source-mode duration defaults.** `vsource_duration_hours` and `isource_duration_hours` default `1.0 → 0.0` so unattended runs no longer auto-stop after one hour without warning.
- **Display buffer is now unlimited by default.** `display.buffer_size` default `1000 → 0`. pyqtgraph's `setDownsampling(auto=True, mode='peak')` + `setClipToView(True)` keeps long traces smooth; a 17-hour run at 9 Hz is ~13 MB and stays interactive. Bounded buffer silently truncated the live trace on overnight runs.
- **Sampling-rate cap accounts for offset-comp 2× slowdown.** When Enhanced accuracy is enabled in resistance mode, the rate-cap predictor halves the achievable rate accordingly. Toggle on the checkbox refreshes the cap immediately.

### Fixed
- **`save_active_plot` was broken on every PgLiveCanvas live tab.** It called `canvas.fig.savefig(...)` but PgLiveCanvas has no `.fig`. New `_get_active_canvas_for_save` resolves the right canvas per tab; PgLiveCanvas routes through pyqtgraph's `ImageExporter` (`SVGExporter` for `.svg`); HistogramCanvas + IVCanvas keep matplotlib `savefig`. PDF dropped from the filter list for pyqtgraph canvases (it can't write one).
- **Cable null bypass** introduced by the resistance-mode FORM:ELEM upgrade: `update_data` was routing the new V+I data dict through `add_voltage_current`, which recomputes R = V/I and skipped the cable-null-corrected R. Now routes by mode rather than dict shape so resistance keeps using `add_resistance`.

### Removed
- **`MplCanvas` class** (`~92 lines` in `ui/canvas.py`). Dead code — every live tab uses `PgLiveCanvas` since 1.9.0. `HistogramCanvas` (4PP) and `IVCanvas` (sweep) are the only matplotlib canvases that remain.
- **`plot_update_interval` and `plot_figsize` Settings dialog controls.** Theatrical — refresh rate was always capped at 16 ms in the timer setup and figsize never reached the canvas constructors. Underlying config keys preserved for back-compat.

## [1.9.0] - 2026-05-20

### Added
- **Live time-series canvases now use pyqtgraph** (Resistance / V-source / I-source / 4PP results), driven at 60 fps with peak-mode downsampling and clip-to-view so an hour-long run at 9 Hz (~30k samples) stays interactively smooth. Matplotlib remains in charge of the 4PP histogram + I-V sweep where annotation density matters more than refresh.
- **Live "value pill"** anchored to the top-right of each live plot — `R = 102.4 Ω`, `I = 1.234e-06 A`, etc. — large monospaced HUD-style readout for demo / projector visibility.
- **Per-machine GPIB address** in the shared `config.json` (`machines.<hostname>.gpib_address`). A NAS-shared config works across lab PCs with different rigs; legacy global `measurement.gpib_address` auto-migrates into the host's slot on first open.
- **Humanized connection errors** wrap the common pyvisa failure modes (resource not found, timeout, busy, library missing) into one-line messages that name a concrete next action. macOS branch points at the lab Windows PC because NI-VISA isn't available on Darwin. The GPIB selector re-opens automatically after an address-related failure.
- **Dynamic sampling-rate cap** — the per-tab Sampling Rate spinbox enforces a soft cap computed from the Keithley timing model (NPLC × auto-zero × filter count). When the user types above the cap, a one-line status-bar message names the cheapest single setting change that would actually reach the requested rate (e.g. *"set auto_zero to 'once' (re-zeros are cached during the run)"*). Model is validated against 27 bench points on a real 2400 (`docs/keithley_2400_timing_bench.json`); estimator is conservative within ~5% in the production range.
- **Per-mode timing overrides** (`MODE_TIMING_OVERRIDES`) — 4PP and vdP force accuracy-tuned `auto_zero=on, filter_count=10` at gather time regardless of shared defaults; sensor modes keep the snappy defaults. Static-spot measurements care about the tightness of Rs / ρ, not how fast the trace updates.
- **PyInstaller-built Windows .exe** via `.github/workflows/build.yml` — runs on tag pushes, smoke-tests the bundle with `--version`, attaches `ResistaMet.exe` to the GitHub release. Assumes NI-VISA is installed on the target Windows machine.
- **`resistamet_gui/timing.py`** — `estimate_max_sample_rate_hz()`, `TimingSettings`, `suggest_change_for_rate()`. Used by the dynamic cap and exposed for tests / external tooling.

### Changed
- **Sensor-friendly defaults**: `auto_zero` `on → once`, `filter_count` `10 → 5`, `plot_update_interval` `200 ms → 16 ms`. Shared `measurement` block now yields a real 9.6 Hz on the bench instead of an aspirational 10 Hz that the instrument silently delivered at 1.8 Hz. `auto_zero=on` is restored automatically when the 4PP or vdP modes start.
- **4PP defaults**: `fpp_samples` `0 (continuous) → 20`. Bench data on the lab 2400 shows the underlying V std stops shrinking past N≈20 — beyond that you're sampling drift, not noise. 20 samples × ~560 ms/sample ≈ 11 s per spot, fast enough for live mapping; SE_mean ≈ 0.25%.
- **vdP defaults**: `vdp_readings_per_polarity` `1 → 5`. Cheap statistical improvement; per-polarity averaging matches the rest of the app's noise budget.
- **Live canvas aesthetics**: off-white background, refined deep-red / deep-blue palette tuned for projector contrast, 2.5 px cosmetic line width, 8 % auto-range padding so foam-press jumps don't pancake the pre-jump baseline against the axis. Hidden top/right spines on the histogram; min/max/avg row gets vertical separators. Title font 13 pt bold for projection.
- **Measurement output is now a single CSV with a `#`-prefixed metadata header instead of a dual `.json` + `.csv` emit.** The CSV begins with `# resistamet_format_version: 2.0` followed by flattened run metadata (`# user:`, `# mode:`, `# params.*:`, `# units:`), then the column header row and streaming data rows, then a `# --- run completed ---` block with `# ended_at:` / `# total_samples:` / `# duration_s:`. Excel, Origin, and `pandas.read_csv(comment='#')` all handle it transparently. The CSV is also the crash-recovery artifact: streamed and `fsync`'d per row, no separate checkpoint sidecar needed.
- **New `output` config section** in `config.json` and Settings → Output tab:
  - `format`: `csv` (default), `hdf5` (requires optional `h5py`), or `csv+legacy_json` for the pre-2.0 dual emit.
  - `compression`: `never` (default — many lab tools can't open `.gz` directly), `always`, or `auto` (gzip when above `compression_threshold_mb`).
  - Compression fires at finalize and emits a status-bar line (e.g. `Compressed run.csv -> run.csv.gz (38.2 MB -> 4.1 MB)`).
- **HDF5 backend** writes a single `.h5` with chunked, gzip-compressed dataset and metadata in `.attrs`. `h5py` is lazy-imported and listed as an optional dependency; the Output tab disables the HDF5 row when it isn't installed.
- **`open_result_csv`** in the Results Viewer now opens both `.csv` and `.csv.gz`, tolerates legacy column names (`Elapsed Time (s)` as well as `elapsed_s`), and pulls run metadata from the `#` header into the status log. This also fixes a silent breakage where the v1.x viewer searched for `'Elapsed Time'` but exporters had been emitting `elapsed_s`.
- **Large-file status nudge.** When a run finalizes to an uncompressed `.csv` above ~20 MB (`LARGE_FILE_NOTIFY_MB`), the status bar surfaces the size and points users at Settings → Output to enable compression. Passive — no dialog, no blocking, no prompt on small runs. Stays quiet when the file was already compressed.

### Migration
- Existing pipelines that parse the per-run `.json` will not see new files after upgrade. Either update them to read the CSV header (`resistamet_gui.data_export.parse_metadata()` is a 1-call helper, supports `.csv.gz` too), or open Settings → Output and switch the format to `Legacy: CSV + JSON (pre-2.0)`.
- Existing `config.json` files auto-merge the new `output` section with defaults on first load — no manual edit required for the change to take effect.

## [1.8.2] - 2026-05-12

### Fixed
- Python 3.9 import error in `resistamet_gui/ui/widgets.py` caused by PEP 604 `dict | None` syntax. Added `from __future__ import annotations` so type hints are deferred and the file imports cleanly on Python 3.9 through 3.13. v1.8.0 and v1.8.1 worked on Python 3.10+ but failed at import time on 3.9 (the floor declared in `pyproject.toml`); CI caught this on the 3.9 matrix slot.

## [1.8.1] - 2026-05-12

### Changed
- Pressing **Start** without a sample name now opens an inline text-input prompt instead of a "Please enter a sample name" warning dialog. Enter the name in the prompt and the run continues; the top-bar sample field is also populated so it stays visible for the rest of the session. Applies to all five measurement tabs.

## [1.8.0] - 2026-05-12

### Added
- **Van der Pauw measurement mode** per ASTM F76-08 Method A. New 6th tab in the main window. The user wires 4 alligator clips to the corners of a uniform sample (numbered 1–4 counter-clockwise), then the UI walks them through F76's 4 cabling geometries one at a time, automating current reversal at each geometry (+I then −I cancels thermal-EMF offsets per F76 sec. 11.1). After 4 geometries (8 voltage readings) the worker computes ρ_A, ρ_B, ρ_avg, R_s, the asymmetry ratios Q_A/Q_B, the geometric factors f_A/f_B by solving F76 Fig. 5's implicit equation `(Q-1)/(Q+1) = (f/ln2)·arccosh{(1/2)·exp(ln2/f)}` via hand-rolled bisection, and the F76 sec. 11.1 homogeneity gate (rejects samples with |ρ_A−ρ_B|/ρ_avg > 10%).
- **`resistamet_gui/calculations_vdp.py`** — pure functions: `vdp_geometric_factor(Q)`, `vdp_resistivity_pair`, `calculate_van_der_pauw`, `f76_geometries()` (4 physical cabling configs), `f76_configurations()` (the 8 F76 voltage labels), `VdpResult` / `VdpConfiguration` / `VdpGeometry` NamedTuples. 37 unit tests pin values directly to F76 Section 11.
- **`VdpMeasurementWorker`** QThread state machine in `workers.py` — emits `geometry_ready` and waits for the UI's `proceed()` slot between geometries so the user can safely reconnect leads; sources +I, settles, reads V, sources −I, reads V at each geometry; output OFF between geometries for safe lead handling. SCPI-contract regression tests guard the wiring (RSEN ON, FORM:ELEM includes VOLT+STAT, ≥4 polarity flips, OUTP OFF between geometries).
- **CLI bench-test driver** at `tests/hardware/vdp_bench.py` — stdin- and SSH-driveable wrapper around the worker's SCPI sequence with `--geometry N` / `--state-file` / `--finalize` flags for the multi-call workflow. Mirrors the `rsen_diagnostic.py` pattern.
- **Bench-verified** on a Keithley 2420 with a 100 µm conductive foil and 4 alligator-clip contacts: ρ_A and ρ_B agree to 1.4%, R_s = 5.65 mΩ/sq, F76 sec. 11.1 homogeneity gate passes with 7× headroom.

## [1.7.0] - 2026-05-12

### Fixed
- **(critical, 4PP):** `:SYST:RSEN OFF` in the 4-Point Probe setup silently routed the voltmeter to the Force terminals instead of the Sense terminals. On the Signatone S-302 (and any probe head wired Force=outer, Sense=inner) this meant every 4PP measurement since the mode shipped was a 2-wire reading across the current-carrying outer pair — R_sample + 2·R_contact, not the intended sheet resistance. Bench-verified on a Keithley 2420: V(OFF)=3199 mV vs V(ON)=−3 µV on a copper plate with a 100 Ω perturbation resistor on the I/O HI lead. Prior 4PP data should be considered invalid; rerun against samples of record after upgrading.

### Added
- **ASTM F84-02 correction-factor decomposition** in `calculations.py`:
  - `f2_finite_diameter(s, d, geometry)` — Table 3 for circles, plus Smits 1958 tables for square + rectangle L/W ∈ {2, 3, 4}.
  - `f_thickness_correction(w, s)` — Appendix X1.1 closed form, valid out to w/S = 2.0.
  - `f_temperature_correction(rho, T, dopant)` — Table 5 C_T lookup, n- and p-type silicon.
  - `calculate_resistivity_f84()` and `calculate_four_point_probe_f84()` glue everything into ρ(T) = R·F₂·w·F(w/S)·F_sp [·F_T]. 60 unit tests pin values directly to F84 Tables 3/4/5 and the extended Smits geometry table.
- **F84 UI fields** in the 4PP tab — Diameter D and Geometry (circle / square / rectangle L/W = 2, 3, 4) next to Thickness; Temperature and Dopant in the Advanced collapsible. The legacy K/α/Model path remains the default so existing `config.json` files produce identical numbers; the F84 path activates only when the user supplies a finite D, non-circle geometry, or T+dopant.
- **Per-polarity delta logging** — when current-reversal (delta) mode is on, CSV exports now include V_plus, V_minus, R_f, R_r columns alongside the V_delta-derived row, preserving the F84 §13.1 diagnostic that opposite-polarity readings should be kept separate.
- **`TestSCPIContract`** class in `tests/test_workers.py` — for each mode, asserts the critical SCPI commands sent during setup (RSEN state, source/measure function, FORM:ELEM contents, compliance programming). This is the regression-prevention pattern that would have caught the original RSEN bug; the harness already existed (`fake_rm.opened[0].command_log`), only the assertions were missing.
- **Bench-verification artifact** at `tests/hardware/rsen_diagnostic.py` — runs in ~10 s against the live instrument, sets up the 4PP path with RSEN OFF then RSEN ON back-to-back, and prints the diagnostic delta.

## [1.6.1] - 2026-05-08

### Fixed
- Pressing **Start** on the 4-Point Probe tab in v1.6.0 raised `AttributeError` because the layout refactor removed the (hidden) MplCanvas but the start path still tried to clear it. 4PP now correctly routes through the histogram path.

## [1.6.0] - 2026-05-08

### Added
- `--simulate` CLI flag launches the full GUI against the in-package Keithley 2400-family simulator — no NI-VISA / pyvisa-py / GPIB needed (`--sim-resistance` and `--sim-model` configure the fake DUT). Powered by the same simulator that validates SCPI fidelity against captured hardware traces.
- `pip install -e .` now registers a `resistamet-gui` console command (entry point in `pyproject.toml`); `python resistamet-gui.py` from the repo behaves identically.
- `Cmd/Ctrl + 1..5` jumps to a tab.
- Reproducible screenshot generator (`tools/generate_screenshots.py`) — runs headless, no instrument required.

### Changed
- Layout overhaul: every tab now uses a single horizontal-splitter pattern (parameters left, plot/data right). Removed the Hide Params / Hide Controls workarounds that existed for the old vertical-stack layout, plus their View-menu equivalents.
- Two-column parameter forms across all five tabs (Resistance / Voltage Source / Current Source / 4-Point Probe / I-V Sweep) — roughly halves the number of form rows per tab.
- Window minimum 900×700 → 720×560; 4PP fixed-pixel widths replaced with `fontMetrics`-derived multiples; SettingsDialog tabs wrapped in QScrollArea and capped at 90% of screen height.

### Removed
- Hidden 4PP `MplCanvas` + "Show Plot" toggle (the right-panel histogram is what users look at).
- File-menu "Open Result (CSV)..." duplicate (kept the Results Viewer button).
- `QTimer.singleShot(150)` deferred 4PP splitter init.

## [1.5.1] - 2026-05-08

### Fixed
- Live-plot overlap and small-window layout breakage on non-maximized windows.

### Changed
- 4PP source current now defaults to 100 µA.

## [1.5.0] - 2026-04-30

### Added
- Stateful Keithley 2400-family simulator (`tests/fakes/fake_keithley.py`) validated byte-equivalent against 23 captured SCPI traces from real hardware (3 DUT decades: 100 Ω, 10 kΩ, 1 MΩ).
- Cross-model validation: every fixture and quirk-trigger reproduces on a Keithley 2400 (1 A) using fixtures originally captured from a 2420 (3 A); 29/29 pass cross-model.
- Model identification at connect — `Keithley2400.detect_model()` parses `*IDN?`, surfaces source/measure caps in the status bar.
- Community trace contribution path: `scripts/community_capture.py` is a self-contained one-file capture tool any contributor can run; `tests/test_community_traces.py` auto-discovers and replays accepted submissions in CI.
- Live power readout (`P = V × I`) on resistance, source-V, source-I, and 4PP modes.
- 4PP probe-safety envelope: pre-flight check refuses runs with worst-case power above the hard stop; runtime check aborts and disables output if measured V×I exceeds it; configurable warn / hard-stop thresholds.
- Hardware-tier test suite (`tests/hardware/`) gated by `RESISTAMET_HARDWARE_ADDR`; CI matrix expanded to Linux + Windows × Python 3.9–3.12.
- Sim fidelity report (`docs/sim_fidelity.md`) documents validated behaviors, intentional gaps, and recapture procedure.

## [1.4.0] - 2026-04-01

### Added
- I-V Sweep mode using the Keithley hardware sweep engine (up/down/up-down).
- Hardware averaging filter (`:SENS:AVER`, repeat/moving, 1-100 count).
- Auto zero control (on/once/off).
- Offset-compensated ohms for resistance mode (thermoelectric cancellation).
- Cable null / relative reference for lead-resistance subtraction.
- Auto source delay (`:SOUR:DEL:AUTO ON`).
- Non-concurrent measurement functions (`:SENS:FUNC:CONC OFF`).
- High-impedance output-off mode (`:OUTP:SMOD HIMP`) for DUT safety.
- Found via full Keithley 2400 manual audit; 144 tests passing.

## [1.3.0] - 2026-04-01

### Fixed
- 5 critical SCPI bugs found via live Keithley 2420 hardware testing.

### Added
- Engineering notation input for current/voltage fields.
- Live numeric readout on all tabs.
- 4PP histogram, multi-spot tracking, current reversal (delta mode).
- Dual-format data export (JSON + CSV) with crash recovery.
- 11 UX improvements (non-blocking compliance, tab switching, tooltips, etc.).
- GUI smoke test suite (142 total tests).
- System sleep prevention, instrument health monitoring.

## [1.2.0] - 2025-11-19

### Added
- Four-Point Probe measurement mode.
- Profiles system, results viewer.
- Enhanced UI with splitters and view toggles.

### Changed
- Modularized codebase architecture.

## [1.1.0] - 2025-03-25

### Added
- Voltage and current source modes.
- Enhanced data buffering, improved CSV export.

## [1.0.0]

### Added
- Initial release — basic resistance measurement.

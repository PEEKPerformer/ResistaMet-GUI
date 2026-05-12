# Changelog

All notable changes to ResistaMet-GUI are documented here.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- **ASTM F84-98 correction-factor decomposition** in `calculations.py`:
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

# Changelog

All notable changes to ResistaMet-GUI are documented here.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.13.0] - 2026-07-01

### Added
- **Four-point probe pre-run current finder** (**on by default**). `Auto-select source current` on the 4PP tab delta-probes the sample before the run, measures its resistance and the *empirical* noise floor, and picks the **smallest** source current that reaches a target number of valid significant figures (`Target sig figs`, default 4 → SNR ≈ 10⁴). Choosing the minimum sufficient current means self-heating is handled silently — the tool never asks the user to reason about it. When on, the manual Source Current field is grayed and shows the picked value; uncheck it for manual current entry (advanced). Adds ~3 s per run. It reports one of three outcomes: measurable (auto-sets the current, logs the achieved sig figs), too conductive, or too resistive/compliance — the latter two via a blocking **Proceed / Abort** dialog explaining what to change (e.g. raise the power-stop limit or lower compliance). The chosen current flows into the filename and CSV metadata.
  - New pure planner `calculations.select_four_point_current` (unit-tested; returns the achieved SNR and sig figs), new settings `fpp_autoselect_current` / `fpp_autoselect_sigfigs`, and tuning constants `FPP_AUTOSELECT_MIN_SNR` / `FPP_AUTOSELECT_MIN_CURRENT` / `FPP_AUTOSELECT_PROBE_CYCLES` / `FPP_AUTOSELECT_RESOLUTION_FLOOR`.
  - The finder gates on the measured delta noise floor rather than the datasheet accuracy spec, because the datasheet offset is systematic and cancels in delta mode — so it will not wrongly reject low-resistance samples that delta + averaging can actually resolve.
  - Handles the full conductivity range. If the seed current hits voltage compliance the finder re-probes at the lowest current: a high-but-measurable resistance is measured there (rather than mis-reported from the clamped voltage), and a genuine insulator — still in compliance at the lowest current — is reported as a **conductivity upper bound** (*"σ < X S/m"*, or an R/Rs lower bound without thickness). The bound is set by the instrument's own current-measurement floor (`estimate_current_floor`), so it reflects what a 2400 can actually resolve (~10⁻⁵–10⁻⁷ S/m; lower needs an electrometer).

## [1.12.3] - 2026-05-26

Paper-only patch. No software changes from v1.12.2.

### Fixed
- **`paper/paper.md` Acknowledgements** corrected from "received no external funding" to disclose support from the U.S. Department of Education GAANN program (award **P200A240111** to the UConn Polymer Program, PI: Mu-Ping Nieh). Standard federal-funding disclaimer included.
- **Backup advice admonition** in `docs/installation.md` demoted from `!!! warning` to `!!! tip` so it no longer stacks visually with the data-location warning above it. Synology paragraph trimmed from four sentences to two.

## [1.12.2] - 2026-05-26

`.exe` now bundles `h5py` so the HDF5 output backend is reachable for `.exe` users, plus a docs pass that swaps the README hero, rewrites Quick Start per-mode workflows as how-to steps, and strips implementation-symbol leakage from the reference pages.

### Added
- **`h5py` bundled in the Windows `.exe`.** The HDF5 output backend was previously unreachable for `.exe` users: `h5py` wasn't in the dependency chain and the GUI grayed out the option with `pip install h5py` advice that `.exe` users can't act on. Bumps the binary by ~10–15 MB and ships the HDF5 native runtime via the wheel. Source installs unchanged (`h5py` remains an optional extra: `pip install -e .[hdf5]`).
- **Van der Pauw screenshot** in the screenshot-generator harness (`tools/generate_screenshots.py`), so it stays auto-regenerated on each tag push alongside the other five tabs. Mid-protocol frame: geometries 1–2 complete, geometry 3 active; sample-diagram, filmstrip, and F76 readings table all visible.
- **Data-storage warning admonition** in `docs/installation.md` explaining that `.exe` users get `config.json` and `measurement_data/` next to the `.exe` (the launch directory), with practical consequences (don't run from `Downloads\`, don't put in `Program Files\`, "Start in" gotcha for shortcuts, upgrade workflow).
- **Backup warning admonition** in `docs/installation.md` introducing the 3-2-1 rule and pointing users at cloud-synced folders (Google Drive client, OneDrive, Dropbox) or institutional NAS. Includes a worked example of the development lab's three-PC Synology Drive Client deployment, showing why `gpib_address` is stored per-hostname.

### Changed
- **README hero image swapped from I-V sweep to Van der Pauw.** The visually richest mode (sample diagram, filmstrip, mid-protocol state) lands first; I-V sweep moves to the expandable tab gallery.
- **Quick Start per-mode workflows rewritten as how-to steps.** The vdP section was previously a `VdpMeasurementWorker` / SCPI / widget-class walkthrough; now it's four numbered steps plus the new screenshot. Other modes lost inline CSV-column listings in favor of one link to `outputs.md` at the top.
- **Reference docs stripped of implementation-symbol leakage.** `concepts.md`, `settings.md`, `troubleshooting.md`, and `outputs.md` no longer drop internal constants (`KEITHLEY_COMPLIANCE_MAGIC_NUMBER`, `MODE_TIMING_OVERRIDES`, `F76_HOMOGENEITY_TOLERANCE_PCT`, `_MODE_VOLTAGE_KEYS`), function names (`gather_settings_for_mode`, `humanize_connection_error`, `timing.suggest_change_for_rate`), or `file.py:line` references into user-facing prose. The factual content (10% F76 homogeneity gate, STAT bit 3 detection, force-on auto-zero for 4PP/vdP, machine-local `gpib_address` design) is preserved; only the code-archaeology surface is gone.
- **`docs/installation.md` Windows section reorganized.** New step 3: make a `ResistaMet\` folder on the desktop and move the `.exe` into it; step 4: open the folder and double-click. Replaces the old "put it somewhere on PATH or pinned to Start menu" step that contradicted the data-location warning right below.
- **`CITATION.cff` abstract rewritten** to be framework- and format-agnostic. Drops the stale `PySide6` / `JSON+CSV` / crash-recovery framing in favor of capability- and standard-scoped language (ASTM F84, F76) that survives format and binding swaps. Will propagate to Zenodo on this tag.
- **`docs/sim_fidelity.md` "why this design"** paragraph rewritten to drop the JOSS-reviewer manifesto framing in favor of one sentence on the methodology and its bench-access cost.

### Fixed
- **`docs/citation.md` piezoresistive characterization note** corrected: the silicone-foam paper used resistance mode, not current source mode.

## [1.12.1] - 2026-05-25

Documentation-only patch. No application behavior changes from v1.12.0.

### Added
- **Hosted docs site** at [bfer.land/ResistaMet-GUI](https://bfer.land/ResistaMet-GUI/), built with mkdocs-material and deployed via a new `docs.yml` GitHub Actions workflow. Eight pages: Home, Installation, Quick Start, Concepts (SMU glossary), Settings, Data Outputs (CSV / HDF5 / legacy JSON column reference for every mode), Troubleshooting, Simulator Fidelity, and Citation. Every claim in the docs is grounded in a specific source-code line (~11,000 lines read this pass: every Python file in `resistamet_gui/` plus `tools/generate_screenshots.py`).
- **README documentation map**: top-level README now links the 8 hosted docs pages so a visitor landing on the GitHub repo has one click to any reference material.

### Changed
- **`CITATION.cff` title scope-tightened** from "for Keithley Sourcemeters" → "for Keithley 2400-family Sourcemeters" to match what the software actually supports (`ModelSpec` table in `instrument.py`). Plain-text + BibTeX citation blocks in `README.md` and `docs/citation.md` updated to match. Per-version Zenodo DOIs are immutable so v1.12.0's archive keeps its existing title; the concept-DOI page will pick up the new title on this release.
- **README slimmed from 286 → 140 lines.** Deep per-mode descriptions, full installation paths (VISA backends, Linux Qt packages, per-model envelope, cross-model help), and detailed Quick Start expansions moved to the docs site. Everything dropped from the README is still in the repo — just one click away on the docs site. 2-sentence Statement of Need retained.
- **Screenshot generator tolerates `--out` paths outside the repo root.** The CI smoke step in `test.yml` passes `--out /tmp/screenshots-smoke`, which crashed the generator at `print(out_dir.relative_to(ROOT))` because `/tmp` isn't under ROOT. New `_pretty()` helper falls back to the absolute path when `relative_to` raises ValueError.
- **`mkdocs.yml` `site_url`** updated from the `peekperformer.github.io` default to the actual `bfer.land/ResistaMet-GUI/` custom-domain URL so canonical link tags and `sitemap.xml` are correct.
- **Paper Acknowledgements** add a one-line no-funding statement per JOSS policy.

### Fixed
- **`.gitignore` runtime artifact gaps.** `config.json` and `measurement_data/` are now gitignored at the project level. CLAUDE.md already described `config.json` as gitignored; this commit makes it true for contributors who don't share Brenden's `~/.config/git/ignore` global rule.
- **`.gitignore` for `paper/joss-docs/`** narrowed from `paper/` so future paper assets (figures, additional bib files) can ship without per-file exceptions.
- **`.claude/settings.local.json`** project-gitignored so contributors aren't relying on a global ignore rule for their personal Claude Code overrides. CONTRIBUTING.md now documents what the tracked `.claude/settings.json` project hooks do.

### Infrastructure
- **PR template** (`.github/pull_request_template.md`) prompting for summary, change type, hardware testing notes, and test plan.
- **`docs.yml` workflow** rebuilds and deploys the mkdocs site on any push to `main` that touches `docs/**`, `mkdocs.yml`, or the workflow itself.

## [1.12.0] - 2026-05-25

JOSS-submission-ready release. No user-visible application changes; this turn bundles the submission paper, gitignore hygiene, and a screenshot-generation pipeline that is honest about what the GUI actually renders.

### Added
- **JOSS submission paper.** `paper/paper.md` + `paper/paper.bib` with the six JOSS-required sections (Summary, Statement of Need, State of the Field, Software Design, Validation, Research Impact, AI Usage Disclosure). All 14 bibliography entries Crossref-verified or pulled directly from upstream CITATION.cff files (`pyvisa`, `pymeasure`, `qcodes`, `keithleygui`, `qkeithleycontrol`, `oh2023meassure`, `febba2025`, `astm_f84`, `astm_f76`, `smits1958`, `harris2020array`, `keithley2400manual`, `meg2026`, `silicone2026`). One 4PP screenshot embedded via pandoc-native `{#fig:fpp}` attribute syntax. Renders cleanly through the JOSS `openjournals/inara` container with no warnings or undefined refs.
- **Pull request template** (`.github/pull_request_template.md`) prompting for summary, change type, hardware-testing notes, and test plan. Lines up with the existing `.github/ISSUE_TEMPLATE/` set.
- **Screenshot-generator smoke test** in `test.yml` on every push/PR. Runs `tools/generate_screenshots.py` on Linux/Py3.12 to catch generator-broke regressions early — does not byte-diff against committed PNGs because macOS-vs-Linux font rendering differs slightly.
- **Auto-regenerate screenshots on tag push** via a new `regen-screenshots` job in `build.yml`, parallel to the Windows `.exe` build. Regenerates on Linux (the canonical CI platform), commits any drift back to main with a `[skip ci]` tag commit. Tag releases never go out with stale screenshots again.

### Changed
- **Screenshot generator is now honest.** `tools/generate_screenshots.py` was bypassing the production live-readout pipeline with plain `setText(f"…")` calls, hiding the colored Wong-palette V/I/R/P labels and dimmed `±σ` annotations that v1.10.0 shipped. Refactored each `fill_*` function to mirror the exact production code path: real `format_readout_html` rendering, real `accuracy.py` σ values (`voltage_uncertainty` / `current_uncertainty` / `resistance_uncertainty` / `voltage_source_uncertainty` / `current_source_uncertainty`), real `_READOUT_DIVIDER`. Each `fill_*` block now references the `main_window.py` line it mirrors so future drift is auditable.
- **4PP screenshot now populates `fpp_spots_table` + `fpp_table`.** Previously only the histogram and Current Spot Stats panel had data — both tables in the right panel were empty, suggesting a broken multi-spot survey. New `fill_four_point` synthesizes 8 spots × 30 readings, treats spots 1–7 as saved (populating `fpp_spots_table` in the 5-col format `_save_fpp_spot` writes at `main_window.py:3159`), and treats spot 8 as in-progress (populating `fpp_table` per-reading at 10 Hz in the 9-col format `_append_four_point_row` writes at `main_window.py:2485`). Also switched the 4PP live readout from misleading `Rs/ρ/σ/V/I` labels (which duplicated the right-panel) to the V/I/R/P labels the real `_update_fpp_live_readout` writes.
- **`.gitignore` scoped to reality.** Root-only `/*.md` glob (instead of recursive `*.md`) so `paper/`, `docs/`, and future contributor docs flow without per-file exceptions; `paper/` rule narrowed to `paper/joss-docs/` so the submission paper + .bib can ship. Runtime artifacts `config.json` and `measurement_data/` are now gitignored — CLAUDE.md already described `config.json` as gitignored; this makes it true.
- **Paper prose copy-edit** on the State of the Field paragraph (L35): added parenthetical commas around "to my knowledge", hyphenated "point-and-click" as a compound adjective, fixed a sentence fragment ("Resistance over time measurement, fixed-bias modes…" had no main verb), swapped `&` for "and", removed a duplicate "and" in the mode list, and replaced a comma splice with a semicolon. Editorial "we"/"our" → first-person singular to match the single-author byline.

### Fixed
- **Committed screenshots were stale.** All five screenshots in `docs/screenshots/` predated v1.10.0 and v1.11.0 by 14–17 days, so they showed neither the `±σ` uncertainty readout, the Enhanced R mode toggle, the Wong palette, nor the smoothed readout. Regenerated, committed, and (per the new CI job above) protected against silent re-staling.

## [1.11.0] - 2026-05-24

### Changed
- **GUI binding swapped from PyQt5 → PySide6** for LGPL-3.0 license compatibility. The PyInstaller `.exe` attached to each release can now be redistributed by MIT-licensed downstreams without inheriting GPL terms. Also moves the project to Qt6 (Qt5 reached EOL Oct 2025). No user-visible behavior change: same widgets, same shortcuts, same canvas backends. Verified end-to-end (521 unit + 15 e2e tests on macOS; 518 + 15 on Windows; hardware roundtrip with a real Keithley 2420 over GPIB). Windows `.exe` grows ~30 MB from the larger Qt6 binaries.
- **Matplotlib backend** switched from `backend_qt5agg` to the Qt-agnostic `backend_qtagg`, dispatching via `QT_API=pyside6` set in `__main__.py` before any matplotlib import. pyqtgraph follows the same convention via `PYQTGRAPH_QT_LIB`.

### Removed
- **Deprecated HighDPI attributes** (`Qt.AA_EnableHighDpiScaling`, `Qt.AA_UseHighDpiPixmaps`). Both became unconditional defaults in Qt6; the explicit calls are no-ops that emit DeprecationWarnings.

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

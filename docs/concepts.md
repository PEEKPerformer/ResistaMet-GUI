# Concepts & glossary

Plain-English explanations for the SMU-specific terms you'll encounter in the GUI and the CSV output. Roughly ordered by how often you'll hit them in a typical session.

## Source vs measure (SMU basics)

A **source measure unit (SMU)** can both source and measure on the same pair of leads. The Keithley 2400 family is bipolar: you pick whether voltage or current is sourced; the other quantity is measured. ResistaMet GUI exposes that as the **Voltage Source** and **Current Source** modes; resistance-mode internally sources a known current and measures the resulting voltage and reports R.

## Compliance

The maximum the *measured* quantity is allowed to reach before the instrument clamps. When you source 1 V into a short circuit, current would in principle be infinite; the **current compliance** prevents that by capping output current. Compliance protects both your DUT and the instrument.

If a reading hits compliance, the Keithley reports a magic number (`9.91 × 10³⁷`) in the affected channel. ResistaMet GUI detects this via the SCPI STAT word's bit 3 (`KEITHLEY_COMPLIANCE_MAGIC_NUMBER = 9.9e37`) and flags the row in the CSV's `compliance` column.

## 2-wire vs 4-wire

In **2-wire** measurement, the same two leads source current and measure voltage, so lead resistance is added to your reading. In **4-wire** (Kelvin), an outer pair sources current and a separate inner pair measures voltage at the DUT — lead resistance falls out of the math because the voltmeter draws ~no current. Use 4-wire whenever sample R is comparable to or smaller than your lead resistance (anything below ~10 Ω).

The Four-Point Probe mode is *always* 4-wire by definition. Resistance mode lets you pick.

## NPLC (Number of Power Line Cycles)

The integration time of each measurement, expressed in line-frequency cycles. NPLC = 1 means "integrate over 1 mains period" (16.67 ms at 60 Hz, 20 ms at 50 Hz) — the classic noise-rejection setting because mains hum averages to zero over an integer number of cycles.

The Keithley datasheet defines three nominal Speed buckets:

| Speed | NPLC | Trade-off |
|---|---|---|
| **Fast** | 0.01 | ~25× faster than Normal; offset spec relaxed by 0.05% of range (0.5% for special ranges) |
| **Medium** | 0.1 | ~3× faster than Normal; offset spec relaxed by 0.005% (0.05% for special ranges) |
| **Normal** | 1.0 | Baseline; clean mains rejection |
| **High accuracy** | 10 | ~10× slower than Normal; tightest reading |

ResistaMet GUI accepts arbitrary NPLC values; the [accuracy module](https://github.com/PEEKPerformer/ResistaMet-GUI/blob/main/resistamet_gui/accuracy.py) interpolates as a step function (≥ 0.5 → Normal modifier, 0.05–0.5 → Medium, < 0.05 → Fast). For rigor below 1 PLC, pin NPLC to one of the three canonical values.

NPLC also drives the live-readout's significant-figure count: ≥ 0.5 → 6 figs (6½-digit hardware), 0.05–0.5 → 5 figs, < 0.05 → 4 figs.

## Auto-zero

The Keithley periodically re-references its internal ADC against ground and a precision reference, then subtracts that offset from subsequent readings. Three modes:

- **`on`** — auto-zero before every reading. Most accurate; 3× slower because each reading takes 3 integrations.
- **`once`** — auto-zero once at run start, then never. ~3× speed-up. Acceptable for sensor work where the signal of interest is R(t) or V(t) and absolute-zero drift over a single run is below the noise floor.
- **`off`** — never auto-zero. Fastest, but readings drift as the instrument warms or cools.

**Default is `once`** — good for typical sensor sessions. Switch to `on` for absolute-accuracy runs longer than ~30 min, or runs where you need long-term comparability across runs. 4PP and vdP modes force `on` regardless of the shared setting (per `MODE_TIMING_OVERRIDES` in [`constants.py`](https://github.com/PEEKPerformer/ResistaMet-GUI/blob/main/resistamet_gui/constants.py)).

## Hardware averaging filter

The Keithley can average N raw readings internally and return one result, in either:

- **Repeat** mode — takes N readings, averages, returns one number, repeats. Best for stable signals.
- **Moving** mode — running average over the last N readings. Better for trending or changing signals.

Lives in the Keithley itself (no per-reading GPIB round-trip), so it's much faster than averaging client-side. Default `filter_count = 5` with `filter_enabled = True`. Set higher for noisier signals; 1 disables.

## Enhanced R mode

When measuring resistance, the Keithley 2400 family has a dedicated higher-accuracy column in its datasheet (the "Enhanced Accuracy" column on p. 7 of 1KW-2798-3, April 2021). It's active when:

- Source-readback is ON (ResistaMet always requests `:FORM:ELEM VOLT,CURR,RES,STAT` so V and I come back alongside R)
- Offset-compensated ohms is ON (`:SENS:RES:OCOM ON`)

The "Offset-compensated ohms" technique sources +I, measures V₊; sources −I, measures V₋; reports `R = (V₊ − V₋) / (2·I)`. This cancels any thermoelectric EMF in the circuit (the steady DC offset that fights you when measuring low-R DUTs).

Cost: each reading takes ~2× longer (two source/measure cycles instead of one). The sampling-rate cap halves automatically when Enhanced R is on.

Win: at low R (V-offset-dominated regime, ~ < 200 Ω) the published σ_R is dramatically tighter than V/I propagation gives. The Enhanced R table covers 20 Ω – 200 MΩ; ResistaMet falls back to V/I propagation outside that range.

**ON by default** because a precision-measurement tool should ship the accurate default. Fast scans where you don't care about EMF cancellation can turn it off in the Resistance tab.

## Sheet resistance vs resistivity vs conductivity

- **Sheet resistance** `Rs` (Ω/□, "ohms per square") — the resistance of a thin square of material, independent of square size. Material property + thickness combined. For a 4PP measurement: `Rs = K · α · (V/I)` where K is the geometric factor (4.5324 for an infinite half-plane and the classic Smits factor; modified by ASTM F84 corrections for finite geometries) and α is the finite-sample correction.
- **Resistivity** `ρ` (Ω·cm) — material property only. `ρ = Rs · t` where t is the film thickness, so requires knowing thickness.
- **Conductivity** `σ` (S/cm) — `σ = 1/ρ`.

The 4PP tab computes all three from V, I, geometric factors, and thickness. The vdP tab computes Rs first and then ρ from the F76 chain.

## Four-point probe (4PP) — quick anatomy

Four collinear probe tips, evenly spaced (default spacing `s = 0.1016 cm` = 40 mil for the Signatone SP4). Outer two source current, inner two measure voltage. Standards-aligned with ASTM F84-02; correction factors F₂ (geometry), F(w/S) (thickness), F_T (temperature, for n-/p-type silicon) compose multiplicatively to give the effective K.

## Van der Pauw (vdP) — quick anatomy

Sheet resistance of arbitrary-shape samples with four periphery contacts. Procedure per ASTM F76-08 Method A: cable up four geometries in sequence (the worker walks you through each), source `+I` then `−I` at each, gives 8 voltage readings. Solve the implicit `f(Q)` equation for the geometry asymmetry factor; F76 §11.1 homogeneity gate flags >10% ρ disagreement between the two orthogonal pairs.

## Compliance "magic number" (9.91 × 10³⁷)

When a reading exceeds range or hits compliance, the Keithley 2400 family returns `+9.91E+37` for that channel as a sentinel — IEEE 754 "not-a-number"-ish. ResistaMet detects this via the SCPI STAT word's bit 3 and falls back to a threshold check for older firmwares. Detection bumps the row's `compliance` column to a flag string.

## Touch-safety warning

IEC 61010-1 sets the SELV (Safety Extra-Low Voltage) upper bound at 30 V DC — below this, electric-shock hazard is negligible under any realistic skin-resistance condition. The 2400/2410/2425/2430/2450 can compliance-clamp at 60–1100 V depending on model, well above SELV.

Before any run whose gating voltage reaches the threshold, ResistaMet shows a warning modal. The gating voltage depends on mode (from `_MODE_VOLTAGE_KEYS` in `safety.py`):

- **Voltage Source mode**: the sourced V (`vsource_voltage`)
- **Resistance / Current Source / Four-Point / Van der Pauw**: the configured V compliance — an open-circuit current source swings *up to* compliance, so even a 1 mA test current can put 200 V on the leads if compliance is set there
- **I-V Sweep**: `max(|sweep_start|, |sweep_stop|)` when sourcing voltage, otherwise the compliance

The threshold defaults to 30 V and is per-user-profile-configurable; set `safety_voltage_warn_v = 0` to disable entirely. A "Don't show again" checkbox flips a sticky silence flag (`safety_voltage_warn_silenced`); Settings → Measurement has a re-enable toggle.

The status bar appends `⚡ N V live` while a hazardous run is active. Warning is informational — it never blocks the measurement.

## Probe power envelope

Four-point probe tips can melt or oxidize if you push too much power through them. The Signatone SP4 series tungsten-carbide tips are spec'd at ~100 mA continuous; ResistaMet ships with conservative defaults:

- `fpp_power_warn_w = 10 mW` — status-bar flash above this measured V·I
- `fpp_power_stop_w = 100 mW` — hard stop, output disabled, run aborted above this

A pre-flight check refuses to start a 4PP run if worst-case `I_source × V_compliance` would exceed the hard stop. These are *also* protective for thin films and conductive polymers, where local Joule heating can damage the sample before the probe.

## Output-off mode

When the source output is disabled (between geometries in vdP, after a Stop, etc.), the Keithley can leave the output terminal in one of several states. ResistaMet uses **high-impedance** (`:OUTP:SMOD HIMP`), which gates the output through a relay — safest for delicate DUTs because there's no low-impedance path even momentarily.

## Auto source delay

Source-then-measure timing matters: switch the source level, wait, then read. Too short and the reading captures the transient; too long and you're throwing away throughput. The Keithley has built-in tables of recommended delays per range; setting `:SOUR:DEL:AUTO ON` lets the instrument pick. ResistaMet enables this by default.

## Cable null

"Measure the lead resistance once, subtract it from every subsequent reading." One button in the Resistance tab: short the leads, click **Null**, the measured R becomes a software offset stored as `res_cable_null` until cleared. Useful for thin-film resistance work where lead/probe-tip R is a meaningful fraction of the DUT R but you can't 4-wire (e.g. through a switch matrix).

## Uncertainty: instrument vs statistical vs combined

Three flavors appear in the GUI and CSV:

- **Instrument** uncertainty (`u_inst`): floor from the Keithley datasheet's per-range accuracy specs. Doesn't reduce with more samples (it's systematic — every reading inherits it).
- **Statistical** uncertainty (`u_stat`): standard error of the mean, `std / √N`. Reduces with more samples (random component).
- **Combined** uncertainty (`u_total = √(u_stat² + u_inst²)`): the right way to roll them up under GUM §5.1.2.

The live results panel and CSV finalize metadata report all three on 4PP per-spot stats and on vdP final results. Shared pure helpers in `calculations.four_point_combined_uncertainty` and `calculations_vdp.vdp_combined_uncertainty` — GUI and worker call into the same code so the result panel and CSV metadata cannot drift.

## RSS vs linear-sum propagation

When propagating uncertainty through `R = V/I`, the Keithley user manual sums the relative uncertainties *linearly* — the conservative worst-case bound, fine for spec-sheet language. ResistaMet uses **root-sum-of-squares (RSS)** per GUM §5.1.6 Eq. 12:

`(σ_R/R)² = (σ_V/V)² + (σ_I/I)²`

This is the standard treatment for *uncorrelated* error sources under the law of propagation of uncertainty. V and I uncertainties come from different signal paths inside the Keithley, so treating them as uncorrelated is appropriate. In V-dominated or I-dominated regimes the two methods agree to ~0.1%; in the balanced regime RSS gives ~0.71× the linear-sum answer.

Full rationale + GUM citations in the docstring of [`resistance_uncertainty`](https://github.com/PEEKPerformer/ResistaMet-GUI/blob/main/resistamet_gui/accuracy.py).

## Wong palette

The 8-color colorblind-safe palette from Wong, *Nature Methods* (2011). Used throughout ResistaMet's live readouts and plot canvases so V/I/R/P live-readout labels match their trace colors:

- **V** — blue `#0072B2`
- **I** — bluish green `#009E73`
- **R** — vermillion `#D55E00`
- **P** — orange `#E69F00`

You can override these in Settings → Display if you have a strong preference, but the Wong defaults are accessible by design.

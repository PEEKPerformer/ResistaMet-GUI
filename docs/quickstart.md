# Quick Start

## First measurement

1. Launch and create a user profile
2. Click **Test Connection** to verify instrument communication (or use `--simulate`)
3. Enter a sample name
4. Set source level and compliance — type natural units like `1mA`, `5V`, `100uA`
5. Click **Start**

The live readout shows V / I / R / P with their propagated `± σ` uncertainty, color-coded per channel (Wong palette).

## Per-mode workflows

### Resistance

2-wire or 4-wire resistance with optional cable-null and offset-compensated ohms (Enhanced R mode). The cable-null one-button captures lead resistance as a software reference; the offset-comp mode cancels thermoelectric EMF in low-resistance samples.

### Voltage / Current Source

DC bias output with the complementary channel monitored. Per-tab `auto_zero` control trades speed for accuracy. Touch-safety voltage warning fires before any run that could put `> 30 V` (IEC 61010-1 SELV) on the leads.

### Four-Point Probe

1. Set source current, probe spacing, and thickness
2. Start measurement — readings appear in the table with a live Rs histogram
3. Click **Save Spot** to archive the current position's stats
4. Move probe to next position, repeat
5. After all spots: histogram switches to a per-spot bar chart, color-coded by deviation from the mean
6. Click **Export Summary** for a per-spot breakdown with inter-spot uniformity RSD

**ASTM F84-02 correction factors** activate automatically when you supply a finite diameter `D` (geometry-aware F₂), non-circle geometry, or temperature + dopant (F_T for n-/p-type silicon). Thickness correction `F(w/S)` from Appendix X1.1 is valid out to `w/S = 2.0`.

**Probe safety envelope.** Configurable warn / hard-stop power thresholds (default 10 mW / 100 mW). A pre-flight check refuses to start if the worst-case `I_source × V_compliance` exceeds the hard stop; a runtime check aborts the run and disables output if measured `V × I` exceeds it. Sized for tungsten-carbide tips (Signatone SP4 family) and conservative for thin-film / conductive-polymer samples where local Joule heating can damage the sample before the probe.

### Delta Mode (thermoelectric cancellation)

1. In the 4PP tab, expand **Advanced**
2. Check **Current Reversal (Delta Mode)**
3. Set settling time (default 0.1 s between polarity flips)
4. Each reading now alternates `+I` / `−I`, reporting `V_delta = (V₊ − V₋) / 2`

CSV exports include per-polarity `V₊`, `V₋`, `R_f`, `R_r` columns for the F84 §13.1 diagnostic.

### Van der Pauw

ASTM F76-08 Method A on arbitrary-shape, hole-free samples with four periphery contacts (numbered 1–4 counter-clockwise). The worker walks you through the four cabling geometries one at a time; current reversal (`+I` then `−I`) is automated at each geometry so thermal-EMF offsets cancel cleanly.

F76's implicit `f(Q)` equation `(Q-1)/(Q+1) = (f/ln 2)·arccosh{(1/2)·exp(ln 2 / f)}` is solved numerically (hand-rolled bisection; no scipy dependency). The §11.1 homogeneity gate automatically flags samples where ρ_A and ρ_B disagree by more than 10%.

### I-V Sweep

Hardware staircase sweep using the Keithley sweep engine (precise inter-step timing via the instrument's trigger model). Source voltage or current with configurable start, stop, step, and per-step delay. Sweep directions: `up`, `down`, or `up-down` (forward + reverse for hysteresis curves).

## Useful inputs

Type natural lab notation instead of raw decimals:

- `1mA` instead of `0.001000 A`
- `100uA` or `100µA` instead of `0.000100 A`
- `10mV` instead of `0.010 V`

The live readout displays in engineering notation too: `V: 2.830 mV  I: 1.000 mA  R: 2.830 Ω`.

Other UI conveniences:

- **Event markers** — press `M` during a run to insert a labeled event in the CSV
- **Multi-user profiles** — each user gets their own settings, saved per profile
- **"Run until stopped"** — checkbox on timed modes for indefinite logging
- **Tab switching during a run** — read-only on inactive tabs, the active one keeps logging

## Testing locally

```bash
# Unit + integration suite (sub-second)
QT_QPA_PLATFORM=offscreen pytest tests/ -v

# End-to-end suite (drives every tab through the in-package simulator,
# asserts recorded values against Ohm's law on a known fake DUT)
QT_QPA_PLATFORM=offscreen pytest tests/test_e2e_simulator.py -v

# Unit tests only (no Qt dependency)
pytest tests/ -v --ignore=tests/test_gui_smoke.py --ignore=tests/test_e2e_simulator.py
```

The e2e suite runs in its own pytest invocation because it leaves process-wide state (a `pyvisa.ResourceManager` monkey-patch and a live `QApplication`) that interacts poorly with module-scoped fixtures from earlier test files. CI runs both invocations in sequence.

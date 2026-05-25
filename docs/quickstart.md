# Quick Start

This walks you through your first measurement and the per-mode workflows. If a term is unfamiliar (NPLC, compliance, Enhanced R, etc.), see [Concepts](concepts.md).

## First measurement

1. **Launch** — `resistamet-gui` (or `resistamet-gui --simulate` for hands-free demo). On Windows with the `.exe`, double-click it.
2. **Pick or create a user profile** in the dialog that appears. Each user has their own settings and data directory.
3. **Test connection** — click the **Test Connection** button on any tab. Either:
    - In simulator mode, it should report a fake Keithley 2400 immediately.
    - With real hardware, it queries `*IDN?` and reports the detected model + firmware. If it fails, see [Troubleshooting](troubleshooting.md#connection-failures).
4. **Enter a sample name** at the top of the window — this names the CSV file written to disk.
5. **Set source level and compliance** on the active tab. Type natural units like `1mA`, `5V`, `100uA`, `0.001` — the spinboxes accept engineering notation directly.
6. **Click Start.** Live readings appear, the plot updates in real time, the bottom strip shows V / I / R / P with their `± σ` uncertainties.

**What success looks like:** The status bar reads `Status: Running`, the live readout has actual numbers (not `--`), and the plot fills in over time. If the readout shows `9.91e37` or stays at `--`, see [Troubleshooting → Compliance hit / 9.91e37](troubleshooting.md#compliance-hit-readings-show-991e37).

## Per-mode workflows

### Resistance

Source a known current, measure resulting voltage, report resistance over time. Best for sensors, conductive composites, and any DUT where you want R(t).

Key settings (on the tab itself, not Settings dialog):

- **Test current** — DC current sourced through the DUT (default 1 mA)
- **Voltage compliance** — maximum voltage allowed across the DUT before clamping
- **Measurement type** — 2-wire (includes lead resistance) or 4-wire (eliminates it)
- **Auto Zero** — `on` / `once` / `off`. See [Concepts → Auto-zero](concepts.md#auto-zero) for the speed/accuracy trade.
- **Enhanced accuracy** — checkbox enables offset-compensated ohms (cancels thermoelectric EMF; ~2× slower per reading). **ON by default** because a precision-measurement tool should ship the accurate default. See [Concepts → Enhanced R mode](concepts.md#enhanced-r-mode).
- **Cable null** — click **Null Cables** with the probes shorted; the helper runs a one-shot resistance reading at NPLC=10 and stores it as a software offset (`res_cable_null`) that's subtracted from every subsequent R reading. The 2400 series lacks `:SENS:RES:REL`, so the subtraction is Python-side. **Clear Null** removes it.

CSV columns: `elapsed_s, V_meas, I_meas, R_ohm, R_unc_ohm, compliance, event`. The R column is the instrument-reported ohms (preserves Enhanced R if enabled); `V_meas` and `I_meas` let you recompute σ_R downstream. See [Data Outputs → resistance](outputs.md#resistance).

### Voltage Source

Apply a DC voltage, monitor the resulting current. Good for chronoamperometry, electrochemistry, device biasing, and bias-stress experiments. Touch-safety warning fires before any run whose sourced V (for this mode) reaches the configured threshold (default 30 V) — see [Concepts → Touch-safety warning](concepts.md#touch-safety-warning).

Key settings: Source voltage, current compliance, duration (`0` = run until you click Stop), auto-zero.

CSV columns: `elapsed_s, V_set, I_meas, R_calc, I_unc_A, R_calc_unc_ohm, compliance, event`. `R_calc = V_set / I_meas` with uncertainty propagated via RSS — see [Data Outputs → source_v](outputs.md#voltage-source).

### Current Source

Mirror of Voltage Source: apply a DC current, monitor the resulting voltage.

CSV columns: `elapsed_s, V_meas, I_set, R_calc, V_unc_V, R_calc_unc_ohm, compliance, event`.

### Four-Point Probe

Sheet resistance, resistivity, and conductivity via a collinear 4-point probe. Standards-aligned with ASTM F84-02 corrections.

**Workflow:**

1. Set source current, probe spacing, and thickness. Default current is **100 µA** (conservative for unknown films); default spacing is **0.1016 cm** = 40 mil (Signatone SP4).
2. Start the measurement. Readings stream into the per-reading table, the live histogram fills in with the Rs distribution, and the Current Spot Stats panel updates after each reading.
3. **Save Spot** archives the current set into the saved-spots table (top right). Move probe to next position.
4. Repeat. After ≥2 spots, the histogram canvas switches to a per-spot bar chart, color-coded green/orange/red by deviation from the cross-spot mean.
5. **Export Summary…** writes a hand-rolled CSV (separate from the v2.0 streaming CSV) with overall Rs / ρ / σ mean+std, a per-spot table when ≥1 spot has been saved, and an inter-spot uniformity block (mean-of-means, std-of-means, RSD%) when ≥2 spots are saved. See [Data Outputs → 4PP per-spot summary](outputs.md#four-point-probe).

**ASTM F84-02 correction factors activate automatically** when you supply:

- a finite specimen diameter `D` → geometry-aware F₂ from Table 3 (circles) or Smits 1958 (squares, rectangles with L/W ∈ {2, 3, 4})
- thickness `w/S` → F(w/S) from Appendix X1.1, valid out to w/S = 2.0
- temperature + dopant type (`n` or `p`) → F_T from Table 5 for n-/p-type silicon

When you leave these inputs at their defaults (infinite-diameter circle, no temperature correction), the math falls back to the classical Smits-1958 `F = 4.5324` factor so existing config files keep producing the same numbers as pre-F84-aligned releases.

**Probe safety envelope** (defaults sized for tungsten-carbide Signatone SP4 tips):

- `fpp_power_warn_w` = 10 mW — status-bar flash above this measured V·I
- `fpp_power_stop_w` = 100 mW — hard stop, output disabled, run aborted above this
- Pre-flight check refuses to start if worst-case `I_source × V_compliance` exceeds the hard stop

CSV columns include per-reading V, I, V/I, Rs, ρ, σ + uncertainties on V and I — see [Data Outputs → four_point](outputs.md#four-point-probe).

#### Delta Mode (thermoelectric cancellation)

1. In the 4PP tab, expand **Advanced**
2. Check **Current Reversal (Delta Mode)**
3. Set settling time (default 0.1 s between polarity flips)
4. Each reading now alternates `+I` / `−I`, reporting `V_delta = (V₊ − V₋) / 2`

CSV gains per-polarity columns `V_plus, V_minus, R_f, R_r` for the F84 §11.2.2.2 forward/reverse diagnostic.

### Van der Pauw

ASTM F76-08 Method A for sheet resistance + resistivity on arbitrary-shape, hole-free samples with four periphery contacts (numbered 1–4 counter-clockwise).

The worker is a state machine (see `VdpMeasurementWorker` in `workers.py`): emits `geometry_ready`, blocks on a `threading.Event` until you click Measure for the geometry, sources `+I` then `−I` and averages `vdp_readings_per_polarity` readings at each, then writes `:OUTP OFF` between geometries so you can safely reconnect leads. The vdP tab has a sample-diagram widget (`VdpSampleDiagram`), a filmstrip preview of the four geometries (`VdpProtocolFilmstrip`), and a per-geometry bar chart (`VdpPerGeometryBarChart`).

- F76's implicit `f(Q)` is solved by `calculate_van_der_pauw` in `calculations_vdp.py`. The final result fields land in the trailing CSV metadata block as `vdp_result.sheet_resistance`, `vdp_result.rho_avg`, `vdp_result.homogeneous` (boolean per F76 §11.1), `vdp_result.asymmetry_pct`, `vdp_result.q_a/b`, `vdp_result.f_a/b` — see [Data Outputs → Van der Pauw](outputs.md#van-der-pauw) for the full schema.
- The default `vdp_thickness_cm` is `0.0` (sentinel "unset"). If you click Start without entering a thickness, `_require_vdp_thickness` in `main_window.py` opens a `QInputDialog.getDouble` modal asking for it in µm — prevents the silent `ρ = R_s × 1 µm` failure mode the old default had.

CSV is one row per geometry with both polarities captured — see [Data Outputs → van der Pauw](outputs.md#van-der-pauw).

### I-V Sweep

Hardware staircase sweep using the Keithley's trigger model for precise inter-step timing. Source voltage or current with configurable start, stop, step, and per-step delay. Sweep directions: `up`, `down`, or `up_down` (forward + reverse for hysteresis curves).

CSV: `point, V_source, I_meas, compliance` — one row per sweep point.

## Useful inputs

Type natural lab notation instead of raw decimals:

- `1mA` instead of `0.001000 A`
- `100uA` or `100µA` instead of `0.000100 A`
- `10mV` instead of `0.010 V`

The live readout displays in engineering notation: `V: 2.830 mV  I: 1.000 mA  R: 2.830 Ω` with the Wong-palette label color per channel.

Other UI conveniences:

- **Event markers** — press `M` during a run; a dialog asks for a label (default `MARK`) which lands in the CSV's `event` column. The mark-event button on the active tab flashes yellow for 500 ms as visual confirmation.
- **Tab keyboard shortcuts** — `Ctrl/Cmd + 1..6` jumps to tabs 1 through 6 (Resistance → vdP) without clicking
- **Multi-user profiles** — each user gets their own settings via the Settings → User Settings menu
- **Parameter profiles** — Profiles menu → Save / Load Profile for Current Mode writes/reads a JSON of the active tab's measurement-block settings (useful for per-sample-type templates)
- **"Run until stopped"** — set duration to `0` on timed modes for indefinite logging
- **Tab switching during a run** — the active tab keeps logging, inactive tabs are read-only; status bar shows "Viewing X tab (read-only) — Y measurement running"
- **Smoothed live readout** — the text readout updates at 4 Hz from a ~500 ms rolling mean over the last `max(5, round(0.5 × sampling_rate))` samples, so noisy traces stay readable. The plot, CSV, and stats still see every full-rate sample.

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

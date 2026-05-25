# Data outputs

Every measurement writes a single file (or pair, for legacy mode) named after the sample + a timestamp, into `<data_directory>/<username>/`. The format is selected in Settings → Output.

## Format choices

| Backend | Setting value | Files produced | Best for |
|---|---|---|---|
| **CSV** (default) | `csv` | `<sample>_<timestamp>.csv` | Excel / Origin / pandas. Human-readable. Optional gzip on finalize. |
| **HDF5** | `hdf5` | `<sample>_<timestamp>.h5` | Compact archival, mixed-type columns, attribute-encoded metadata. Always internally gzip-compressed. Requires optional `h5py`. |
| **CSV + Legacy JSON** | `csv+legacy_json` | `<sample>_<timestamp>.csv` + `.json` | Back-compat with pipelines written against pre-2.0 ResistaMet. The CSV has only a header row (no `#` metadata block); the JSON contains the metadata + a full data copy. |

Format version is `2.0` for the CSV and HDF5 backends, `1.0` for the legacy dual emit. Recorded in the metadata so consumers can branch on it.

## CSV layout

```
# resistamet_format_version: 2.0
# user: alice
# sample: cu-foil-spot-1
# mode: four_point
# started_at: 2026-05-25T14:33:21.451
# software_version: 1.12.0
# instrument: KEITHLEY INSTRUMENTS INC.,MODEL 2420,1230523,C30
# gpib_address: GPIB0::24::INSTR
# sampling_rate_hz: 10.0
# nplc: 1.0
# settling_time_s: 0.2
# params.source_current_A: 0.0001
# params.voltage_compliance_V: 5.0
# params.probe_spacing_cm: 0.1016
# params.thickness_um: 0.5
# params.k_factor: 4.532
# params.alpha: 1.0
# params.model: thin_film
# params.target_samples: 20
# params.auto_zero: on
# units: s,V,A,Ω,Ω/□,Ω·cm,S/cm,V,A,,
elapsed_s,V,I,V_over_I,Rs_ohm_sq,rho_ohm_cm,sigma_S_cm,V_unc_V,I_unc_A,compliance,event
0.1,0.001045,0.0001,10.45,47.36,2.37e-3,422.1,3.0e-4,3.1e-8,OK,
0.2,0.001047,0.0001,10.47,47.45,2.37e-3,421.3,3.0e-4,3.1e-8,OK,
...
# --- run completed ---
# ended_at: 2026-05-25T14:33:23.512
# total_samples: 20
# duration_s: 2.061
```

Two metadata blocks: one at the top (everything known at run start), one at the bottom (filled in at finalize). The trailing block is delimited by `# --- run completed ---`. Comments use `# ` prefix; the column header row is the first non-comment line. The `units:` line is parallel-indexed to the column header so downstream code can look up units by column name without hard-coding.

Crash safety: rows are flushed and `fsync`'d as they arrive, so the partial CSV *is* the recovery artifact — there's no separate checkpoint sidecar for the v2.0 backend (the legacy dual-emit backend writes `.json.tmp` checkpoints).

`parse_metadata(path)` in [`resistamet_gui/data_export.py`](https://github.com/PEEKPerformer/ResistaMet-GUI/blob/main/resistamet_gui/data_export.py) reads both blocks and returns a flat dict; handles `.csv` and `.csv.gz` transparently.

## HDF5 layout

Single dataset named `data` of compound dtype (every column as a variable-length UTF-8 string for type-mixing safety). Metadata lives in `attrs` keyed by the same dotted-path names the CSV uses (`params.source_current_A`, `params.thickness_um`, etc.), plus `columns` and `units` attribute arrays. End-metadata is appended to `attrs` at finalize. Chunked (1024 rows per chunk), gzip-compressed at level 6.

## Per-mode columns

### Resistance

| Column | Unit | What it is |
|---|---|---|
| `elapsed_s` | s | Time since Start |
| `V_meas` | V | Voltage measured at the DUT (across the probes if 4-wire) |
| `I_meas` | A | Current sourced (instrument-reported, matches the test-current setpoint) |
| `R_ohm` | Ω | Instrument-reported R — preserves Enhanced R / offset-comp / source-readback features (NOT `V_meas/I_meas` recomputed here) |
| `R_unc_ohm` | Ω | Per-reading σ_R. When Enhanced R is on, this comes from the datasheet's Enhanced column; otherwise from V/I RSS propagation. |
| `compliance` | | Empty or a flag string when the row hit compliance (V at limit, or magic 9.91e37) |
| `event` | | Empty or the label of the most recent `M` keypress event marker |

Metadata `params`: `test_current_A`, `voltage_compliance_V`, `measurement_type`, `auto_range`, `auto_zero`, `offset_compensated_ohms`.

### Voltage Source

| Column | Unit | What it is |
|---|---|---|
| `elapsed_s` | s | Time since Start |
| `V_set` | V | Sourced voltage (the setpoint — actual output is `V_set ± σ_V_source`) |
| `I_meas` | A | Measured current |
| `R_calc` | Ω | `V_set / I_meas` |
| `I_unc_A` | A | σ_I from the per-range current-measurement spec |
| `R_calc_unc_ohm` | Ω | σ_R via RSS through `R = V_set/I_meas` (combining V-source spec + I-measure spec) |
| `compliance` | | Compliance flag for the current side |
| `event` | | Event-marker label |

Metadata `params`: `source_voltage_V`, `current_compliance_A`, `current_auto_range`, `duration_hours`, `auto_zero`.

### Current Source

Mirror of Voltage Source: `V_meas`/`I_set` instead of `V_set`/`I_meas`, `V_unc_V` instead of `I_unc_A`.

Metadata `params`: `source_current_A`, `voltage_compliance_V`, `voltage_auto_range`, `duration_hours`, `auto_zero`.

### Four-Point Probe

| Column | Unit | What it is |
|---|---|---|
| `elapsed_s` | s | Time since Start |
| `V` | V | Voltage measured between inner two probes |
| `I` | A | Current sourced through outer two probes |
| `V_over_I` | Ω | `V/I` — the raw ratio before geometric corrections |
| `Rs_ohm_sq` | Ω/□ | Sheet resistance = `K · α · V/I` (K modified by ASTM F84 corrections when applicable) |
| `rho_ohm_cm` | Ω·cm | Resistivity = `Rs · thickness` |
| `sigma_S_cm` | S/cm | Conductivity = `1 / rho` |
| `V_unc_V` | V | σ_V from per-range voltage-measurement spec |
| `I_unc_A` | A | σ_I from per-range current-measurement spec |
| `compliance` | | Compliance flag |
| `event` | | Event-marker label |

Per-reading uncertainty on Rs / ρ / σ is recoverable downstream: `σ_X/X = √((V_unc/V)² + (I_unc/I)²)`. The aggregated per-spot stats use a different formula combining statistical and instrument uncertainty — see [Concepts → Uncertainty](concepts.md#uncertainty-instrument-vs-statistical-vs-combined).

**Delta mode** splices four more columns before `compliance`:

| Column | Unit | What it is |
|---|---|---|
| `V_plus` | V | Voltage at `+I` polarity |
| `V_minus` | V | Voltage at `−I` polarity |
| `R_f` | Ω | Forward `V_plus / +I` |
| `R_r` | Ω | Reverse `V_minus / −I` |

Per the F84 §11.2.2.2 forward/reverse diagnostic — `R_f / R_r` should approach 1; large deviation flags thermoelectric or rectification effects.

Metadata `params`: `source_current_A`, `voltage_compliance_V`, `probe_spacing_cm`, `thickness_um`, `k_factor`, `alpha`, `model`, `target_samples`, plus the F84 inputs (`fpp_diameter_cm`, `fpp_geometry`, `fpp_temperature_c`, `fpp_dopant_type`) and delta-mode settings when active.

### Van der Pauw

One row per geometry (4 rows total per spot), both polarities captured in the same row:

| Column | Unit | What it is |
|---|---|---|
| `elapsed_s` | s | Time the row was captured |
| `geometry` | | One of 4 F76 geometry labels |
| `group` | | Geometry pair (A/B) for the §11.1 homogeneity check |
| `source_high` / `source_low` / `sense_high` / `sense_low` | | Contact numbers (1–4) for this geometry — derived from F76 Fig. 2 |
| `label_pos` | | Operator-facing label for the positive-polarity measurement |
| `V_pos` | V | Voltage at `+I` |
| `label_neg` | | Operator-facing label for the negative-polarity measurement |
| `V_neg` | V | Voltage at `−I` |
| `current_A` | A | Sourced current magnitude (same `|I|` at both polarities) |

The final sheet resistance, resistivity, f(Q), Q, the §11.1 homogeneity check result, and the combined uncertainty land in the **metadata** at finalize — not as data rows. Look in the end-of-file `# --- run completed ---` block for:

- `sheet_resistance_ohm_sq`, `sheet_resistance_uncertainty`
- `rho_avg_ohm_cm`, `rho_avg_uncertainty`
- `f_q_factor`, `q_factor`
- `homogeneity_check`, `rho_a_vs_rho_b_pct`
- `homogeneity_gate_passed` (boolean per F76 §11.1)

Metadata `params`: `source_current_A`, `voltage_compliance_V`, `thickness_cm`, `settling_s`, `readings_per_polarity`, `standard: ASTM F76-08 Method A`.

### I-V Sweep

| Column | Unit | What it is |
|---|---|---|
| `point` | | Sweep index (0…N) |
| `V_source` | V | Sourced voltage at this step (for V-source sweeps) — for I-source sweeps this column is named `I_source` and the next is `V_meas` |
| `I_meas` | A | Measured current (for V-source sweeps) |
| `compliance` | | Compliance flag |

Metadata `params`: `source_function`, `start`, `stop`, `step`, `compliance`, `delay_s`, `direction`.

## Reading the data back

A small example to load a v2.0 CSV with metadata into pandas:

```python
import pandas as pd
from resistamet_gui.data_export import parse_metadata

path = "measurement_data/alice/cu-foil-spot-1_20260525_143321.csv"

# Metadata as a flat dict
meta = parse_metadata(path)
print(meta["mode"], meta["nplc"], meta["params.source_current_A"])

# Data rows — pandas skips the `#`-prefixed header automatically
df = pd.read_csv(path, comment="#")
print(df.head())
```

For HDF5:

```python
import h5py

with h5py.File(path.replace(".csv", ".h5"), "r") as f:
    print(dict(f.attrs))                # metadata
    data = f["data"][:]                 # structured numpy array
    print(data.dtype.names)             # column names
```

For per-spot 4PP summaries, the **Export Summary** button on the 4PP tab writes a separate CSV with one row per saved spot, columns: `spot_name, n_readings, Rs_mean, Rs_std, Rs_rsd_pct, rho_mean, rho_std, sigma_mean, sigma_std, u_total, u_stat, u_inst`. Inter-spot uniformity is reported in the trailing metadata block.

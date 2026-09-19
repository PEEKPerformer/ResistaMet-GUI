# Data outputs

Every run writes one file (a pair in legacy mode) into `<data_directory>/<sanitized-username>/`. The files are the record of a measurement: the PySide6 window, the desktop app and a script driving the [API](api.md) all write them through the same code, so everything on this page holds whichever client started the run.

## File names

```
{unix_timestamp}_{sanitized_sample_name}_{mode_tag}_{source_value_str}{extension}
```

- `unix_timestamp` is whole seconds, UTC, taken when the file is opened.
- `mode_tag` is `R`, `VSRC`, `ISRC`, `4PP` or `vdP`. An I-V sweep has no tag of its own and is written as `DATA`.
- `source_value_str` is the source level: `1.00mA`, `0.500V`, `1.00mA_delta` (four-point delta mode), `sweep_0.0to1.0`.
- Username and sample name are sanitized before they touch the filesystem: path separators and `..` are removed, and every character that is not alphanumeric, `-` or `_` becomes `_`.

Examples: `1789858503_api-demo_R_1.00mA.csv`, `1789858524_wafer-07_4PP_1.00mA.csv`, `1789860000_diode_DATA_sweep_0.0to1.0.csv`.

Two properties a parser can rely on:

- **The source value stays in the name.** The extension is appended to the base name, so the dot in `1.00mA` or `0.500V` is kept. Releases up to 1.12.3 replaced everything after the last dot, and the same run came out as `..._4PP_1.csv`.
- **A run never writes over an existing file.** The timestamp has one-second resolution, so two runs of one sample inside the same second ask for the same name. The second gets `-2` appended to the base name (`..._4PP_1.00mA-2.csv`), the third `-3`, and so on. The files are created in exclusive mode; if another process takes the name between the check and the open, the second run fails to open its file instead of replacing the first run's data.

A four-point run that belongs to a map also refreshes `<map_id>_map.json` in the same directory; see [Map summary](#map-summary).

## Format choices

| Backend | Setting value | Files produced | Best for |
|---|---|---|---|
| **CSV** (default) | `csv` | `<base>.csv`, or `<base>.csv.gz` when compression applies | Excel / Origin / pandas. Human-readable. |
| **HDF5** | `hdf5` | `<base>.h5` | Compact archival, attribute-encoded metadata. Always internally gzip-compressed. Requires optional `h5py`. |
| **CSV + Legacy JSON** | `csv+legacy_json` | `<base>.csv` + `<base>.json` | Pipelines written against pre-2.0 ResistaMet. The CSV has only a column header row (no `#` metadata block); the JSON contains the metadata and a full data copy. |

`<base>` is the name built above. Format version is `2.0` for the CSV and HDF5 backends, `1.0` for the legacy pair, and is recorded in the metadata so a reader can branch on it.

The legacy pair carries the header metadata in its JSON, but four-point maps are assembled only from the v2.0 CSV and HDF5 files.

## CSV layout

A four-point run on a simulated instrument, started through a headless session with a spot and a client name. Seven of the ten data rows are elided:

```
# resistamet_format_version: 2.0
# user: alice
# sample: wafer-07
# mode: four_point
# started_at: 2026-09-19T18:55:24.642028
# software_version: 1.12.3
# instrument: KEITHLEY INSTRUMENTS INC.,MODEL 2420,9999999,C30   Mar 17 2006 09:29:29/A02  /SIM
# gpib_address: GPIB0::24::INSTR
# sampling_rate_hz: 10.0
# nplc: 1
# settling_time_s: 0.2
# params.source_current_A: 0.001
# params.voltage_compliance_V: 5.0
# params.voltage_auto_range: true
# params.probe_spacing_cm: 0.1016
# params.thickness_um: 525.0
# params.k_factor: 4.532
# params.alpha: 1.0
# params.model: thin_film
# params.target_samples: 10
# params.auto_zero: on
# spot.map_id: wafer-07-map
# spot.index: 2
# spot.label: east edge
# spot.x_mm: 18.0
# spot.y_mm: 0.0
# spot.angle_deg: 0.0
# spot.sample.shape: circle
# spot.sample.diameter_mm: 50.8
# spot.sample.width_mm: 
# spot.sample.length_mm: 
# spot.position_correction: warn
# spot.edge_warn_pct: 1.0
# spot.factor_here: 4.469398933831199
# spot.factor_centre: 4.516721131494501
# spot.relative_error: 0.010588045140722535
# spot.factor_rows: 4.517
# spot.relative_error_rows: 0.01065044022105166
# spot.edge_clearance_s: 5.783464566929132
# client.name: docs-example
# client.version: 1.0
# units: s,V,A,Ω,Ω/□,Ω·cm,S/cm,V,A,,
elapsed_s,V,I,V_over_I,Rs_ohm_sq,rho_ohm_cm,sigma_S_cm,V_unc_V,I_unc_A,compliance,event
0.216768,0.0125335,0.001,12.5335,56.4343,2.9628,0.337518,0.000301504,3.3e-07,OK,
0.322602,0.0124911,0.001,12.491,56.2429,2.95275,0.338667,0.000301499,3.3e-07,OK,
0.425026,0.0125367,0.001,12.5367,56.4486,2.96355,0.337433,0.000301504,3.3e-07,OK,
...
# --- run completed ---
# ended_at: 2026-09-19T18:55:25.809913
# total_samples: 10
# duration_s: 1.1678998470306396
# spot_stats.n: 10
# spot_stats.n_excluded: 0
# spot_stats.rs.n: 10
# spot_stats.rs.mean: 56.310353134307036
# spot_stats.rs.sd: 0.11656252096144196
# spot_stats.rs.rsd_pct: 0.20700015978132152
# spot_stats.rs.u_stat: 0.03686030560492763
# spot_stats.rs.u_inst: 1.3576873414941693
# spot_stats.rs.u_total: 1.358187615678627
# spot_stats.rho.n: 10
# spot_stats.rho.mean: 2.95629353955112
# spot_stats.rho.sd: 0.006119532350475712
# spot_stats.rho.rsd_pct: 0.20700015978132183
# spot_stats.rho.u_stat: 0.0019351660442587038
# spot_stats.rho.u_inst: 0.0712785854284439
# spot_stats.rho.u_total: 0.07130484982312792
# spot_stats.sigma.n: 10
# spot_stats.sigma.mean: 0.3382627077192411
# spot_stats.sigma.sd: 0.0007003999472757331
# spot_stats.sigma.rsd_pct: 0.20705798519683902
# spot_stats.sigma.u_stat: 0.00022148591064531615
# spot_stats.sigma.u_inst: 0.008155782565855637
# spot_stats.sigma.u_total: 0.008158789448817094
# spot_stats.end_reason: target_samples
```

How this file was made: the in-package simulator as a 2420 with a 12.5 Ω DUT and 0.2 % Gaussian noise, a 50.8 mm circular sample 525 µm thick, a spot 18 mm from the center. It shows the format, not a measurement.

Two metadata blocks: one at the top (everything known when the file is opened) and one at the bottom (written at finalize), delimited by `# --- run completed ---`. Nested values are flattened with dots. Comments use the `# ` prefix; the column header row is the first non-comment line. The `units:` line is parallel-indexed to the column header.

Value encoding in both blocks: booleans are `true` / `false`, a missing value is an empty string (`spot.sample.width_mm` above: a circle has no width), non-finite floats are `NaN`, `Infinity`, `-Infinity`. Floats in data rows are written with six significant figures in CSV.

**Crash safety.** Rows are written as they arrive and the file is flushed and `fsync`'d every `auto_save_interval` seconds (Settings → File, default 60 s) and at finalize. After a crash or a power cut the partial CSV is the recovery artifact, without its trailing block and possibly without the rows since the last flush. The legacy backend additionally writes a `.json.tmp` checkpoint at each flush. A file with no trailing block is a run that did not reach finalize.

`parse_metadata(path)` in [`resistamet_gui/data_export.py`](https://github.com/PEEKPerformer/ResistaMet-GUI/blob/main/resistamet_gui/data_export.py) reads both blocks of a `.csv` or `.csv.gz` and returns one flat dict keyed by the dotted names. It coerces values back to Python types, so an identifier that looks like a literal changes (`client.version: 1.0` comes back as the float `1.0`, a map id of `12_3` as the integer `123`). Pass `text_keys=("spot.map_id", "spot.label", ...)` for keys you compare as text.

## Header keys

### Every run

| Key | Meaning |
|---|---|
| `resistamet_format_version` | `2.0` |
| `user`, `sample` | As entered (not sanitized; the sanitized forms are in the path) |
| `mode` | `resistance`, `source_v`, `source_i`, `four_point`, `sweep`, `vdp` |
| `started_at` | Local time, ISO 8601, no UTC offset |
| `software_version` | Version of the ResistaMet backend that wrote the file |
| `instrument` | The `*IDN?` reply |
| `gpib_address` | VISA resource the run opened |
| `sampling_rate_hz`, `nplc`, `settling_time_s` | The timing the run used, after the per-mode overrides (four-point and van der Pauw force their own) |
| `params.*` | The requested per-mode settings; listed per mode below |

`params.aux_sensor_driver`, `params.aux_sensor_address` and `params.aux_channels` are added when auxiliary-sensor co-logging is on.

### `effective.*` — what the instrument reported back

`params.*` is what was asked for. `effective.*` is what the instrument reported after it was configured, recorded so that a reader comparing the two sees what the instrument overrode.

It exists in resistance mode only, as one of two keys. Both hold the reply to `:SENS:VOLT:PROT?` read once, after configuration and before the output comes on (the requested value if the query fails):

| Key | Unit | Written when | Meaning |
|---|---|---|---|
| `effective.voltage_compliance_V` | V | Manual range (`params.auto_range: false`) | The voltage limit in force for the whole run. Compliance is judged against it; see [Resistance](#resistance). |
| `effective.voltage_compliance_V_at_configure` | V | **Auto range** (`params.auto_range: true`, the default) | What the limit was at configure time, and no more than that. In Auto range the instrument's auto-ohms function chooses its own test current and voltage limit per ohms range and moves them as it changes range, so no row was necessarily measured under this number, and `params.voltage_compliance_V` and `params.test_current_A` are not what applied either. The `I_meas` column is the record of the current that flowed. |

### `client.*` — which program asked for the run

| Key | Meaning |
|---|---|
| `client.name`, `client.version` | Self-reported by the client that called `POST /session/start` (or passed `client=` to `MeasurementSession.start`). The desktop app sends `resistamet-desktop` and its package version. |

The block is absent when no client was named, which is the case for every run the PySide6 window starts. It is provenance, not authentication. `software_version` is always the backend's own version.

### `spot.*` — four-point runs that carry a spot

Written when the run was started with a spot: every four-point run from the PySide6 window (label and index, no position), and any API run whose request has a `spot`. Absent otherwise. See [Concepts → Spots and maps](concepts.md#spots-and-maps).

| Key | Unit | Meaning |
|---|---|---|
| `spot.map_id` | | The map this run belongs to. 1–64 characters from `A–Z a–z 0–9 _ -`. |
| `spot.index` | | Which spot of the map, 0–9999. A later run with the same index supersedes an earlier one in the map. |
| `spot.label` | | Operator's name for the spot, 1–80 characters, no control characters. |
| `spot.x_mm`, `spot.y_mm` | mm | Center of the probe array, measured from the center of the sample. Empty when the spot has no position. |
| `spot.angle_deg` | degrees | Direction of the probe array, counterclockwise from +x. The spot's own angle, or the `fpp_array_angle_deg` setting when the spot gave none. |
| `spot.sample.shape` | | `unbounded`, `circle` or `rectangle`: the outline the position was checked against. |
| `spot.sample.diameter_mm`, `.width_mm`, `.length_mm` | mm | The outline's dimensions; empty where the shape has none. A rectangle's length lies along x and its width along y. |
| `spot.position_correction` | | Always `warn`: the position effect is reported and never applied to the rows. |
| `spot.edge_warn_pct` | % | The warning threshold in force (`fpp_edge_warn_pct`). |

Six more keys are written only when the spot has a position **and** the outline is a circle or a rectangle:

| Key | Unit | Meaning |
|---|---|---|
| `spot.edge_clearance_s` | probe spacings | Distance from the nearest probe tip to the nearest edge, in units of the probe spacing `s`. A run whose clearance is not positive is refused before the instrument is opened and writes no file. |
| `spot.factor_here` | | Closed-form lateral geometry factor `Rs / (V/I)` of a thin sheet with insulating edges, for the probe at this position and angle. |
| `spot.factor_centre` | | The same closed form at the center of the same outline. |
| `spot.relative_error` | fraction | `factor_centre / factor_here − 1`. What the position costs if the centered factor of this outline is used. |
| `spot.factor_rows` | | The lateral factor the data rows actually applied: the F84 / Smits table look-up driven by `fpp_geometry` and `fpp_diameter_cm`, or `K·α` on the legacy path, with the thickness term F(w/S) divided out. Empty when the rows have no finite Rs. |
| `spot.relative_error_rows` | fraction | `factor_rows / factor_here − 1`: the error in this file's own `Rs_ohm_sq` due to position. Positive means the file's Rs is too high. |

Both errors are fractions, not percent. They agree to the tables' printed digits when the `fpp_sample_*` outline and the legacy `fpp_geometry` / `fpp_diameter_cm` keys describe the same sample (above: 0.01059 and 0.01065). They differ widely when they do not, for example a 20 mm square outline with the rows still assuming an infinite sheet. The near-edge warning is judged on `relative_error_rows` when it exists and on `relative_error` otherwise.

The closed forms are in `resistamet_gui/calculations_geometry.py`. At the center the circle form reproduces ASTM F84 Table 3 (a unit test holds it there); away from the center the factors are arithmetic only and have not been checked against measurements on a real sample.

## Footer keys

### Every run that reaches finalize

| Key | Unit | Meaning |
|---|---|---|
| `ended_at` | | Local time, ISO 8601 |
| `total_samples` | | Data rows written |
| `duration_s` | s | From file open to finalize |

A van der Pauw file has `vdp_result.*` instead; see [Van der Pauw](#van-der-pauw).

### `spot_stats.*` — every four-point run

Written for every four-point run whose file is finalized, with or without a `spot.*` header. The same numbers go out in the `spot_complete` event and into the map summary, so no client has to recompute them.

| Key | Meaning |
|---|---|
| `spot_stats.n` | Rows that entered the statistics |
| `spot_stats.n_excluded` | Rows left out because `compliance` was not `OK`. A row in compliance records a bound, not a measurement. |
| `spot_stats.end_reason` | Why the run ended, as in the `run_ended` event: `target_samples` for a spot that ran its course, `user_stop`, `duration`, `compliance_stop`, `overpower`, `read_error`, `write_error`, `worker_error`, … Lets a reader tell a short spot from a whole one. Absent in files written before it was recorded. |

Then one group per derived quantity, `spot_stats.rs.*` (Ω/□), `spot_stats.rho.*` (Ω·cm) and `spot_stats.sigma.*` (S/cm), computed by `quantity_statistics` in `resistamet_gui/session/spot_stats.py`:

| Field | Meaning |
|---|---|
| `n` | Finite values of this quantity. Can be smaller than `spot_stats.n`: ρ and σ are NaN when no thickness was entered. |
| `mean` | Arithmetic mean |
| `sd` | Sample standard deviation (n − 1). `NaN` for a single value. |
| `rsd_pct` | `sd / |mean| × 100` |
| `u_stat` | Standard uncertainty of the mean, `sd / √n`; 0 for a single value. Falls as samples are added. |
| `u_inst` | Instrument floor: `|mean|` times the mean over the rows of σ_R/R, where σ_R is `accuracy.resistance_uncertainty(V, I)` for the detected model and the run's NPLC (RSS of the per-range voltage- and current-measurement specs). The same relative floor serves all three quantities. Does not fall with more samples. |
| `u_total` | `√(u_stat² + u_inst²)`, GUM §5.1.2 |

`u_stat`, `u_inst` and `u_total` are the values `calculations.four_point_combined_uncertainty` returns, the function the PySide6 result panel has always used, evaluated over the rows that are not in compliance. All are in the unit of their quantity. With no finite value every field but `n` is `NaN`.

In the example, `u_inst` (1.36 Ω/□) dwarfs `u_stat` (0.037 Ω/□) because 12.5 mV on the 200 mV range is dominated by the range's offset specification. That is the simulated setup, not a property of the format.

## Map summary

A map is the set of four-point runs in one user directory whose headers carry the same `spot.map_id`. Nothing else stores it. After each such run is finalized and the instrument released, the backend reassembles the map from the run files and writes `<map_id>_map.json` beside them (written to a temporary file, then moved into place). The summary is derived data: it is replaced whole each time, can be deleted, and is rebuilt on the next run or read live from `GET /maps/{map_id}`. The run files are only ever read.

```json
{
  "map_id": "wafer-07-map",
  "spots": [
    {
      "index": 1, "label": "centre", "x_mm": 0.0, "y_mm": 0.0, "angle_deg": 0.0,
      "relative_error": 0.0, "relative_error_rows": 0.0000617, "edge_clearance_s": 23.5,
      "sample": "wafer-07", "started_at": "2026-09-19T18:55:21.742032",
      "file": "1789858521_wafer-07_4PP_1.00mA.csv", "superseded": [],
      "stats": { "n": 10, "n_excluded": 0, "end_reason": "target_samples",
                 "rs": {"n": 10, "mean": 56.31, "sd": 0.1166, "rsd_pct": 0.207,
                        "u_stat": 0.0369, "u_inst": 1.358, "u_total": 1.358},
                 "rho": {"...": "..."}, "sigma": {"...": "..."} }
    },
    { "index": 2, "label": "east edge", "...": "..." }
  ],
  "rs":    {"n": 2, "mean": 56.31, "sd": 0.0, "rsd_pct": 0.0},
  "rho":   {"n": 2, "mean": 2.956, "sd": 0.0, "rsd_pct": 0.0},
  "sigma": {"n": 2, "mean": 0.3383, "sd": 0.0, "rsd_pct": 0.0},
  "skipped": []
}
```

(Numbers shortened here; the file holds full precision. The inter-spot spread is zero because the simulator repeats the same noise sequence in every run.)

| Field | Meaning |
|---|---|
| `spots[]` | One entry per spot index, in index order: the header's `spot.*` values, `sample`, `started_at`, the run's `file` name and its `spot_stats` block as `stats`. |
| `spots[].superseded` | Older runs with the same index, newest first. They stay on disk and are not used. "Newest" is decided by the Unix stamp in the file name, then `started_at`, then the `-N` suffix. |
| `rs`, `rho`, `sigma` | Spread between spots: `n`, `mean`, sample `sd` and `rsd_pct` over the spots' means, each spot counting once. `sd` needs two spots. |
| `skipped[]` | `{file, reason}` for runs that name the map but cannot stand for a spot: `no footer: the run did not finish`, `no valid sample`, `unreadable spot block: …`, and files that could not be read at all (`h5py not installed`, `unreadable: …`). A skipped run does not displace a good earlier run of the same index. |

Only files in the user's own directory (not subdirectories) whose names contain `_4PP_` are opened.

## HDF5 layout

Single dataset named `data` of compound dtype (every column as a variable-length UTF-8 string for type-mixing safety). Metadata lives in the file's `attrs` under the same dotted names the CSV uses (`params.source_current_A`, `effective.voltage_compliance_V_at_configure`, `spot.map_id`, `spot_stats.rs.mean`, …), plus `columns` and `units` attribute arrays and `resistamet_format_version`. End metadata is added to `attrs` at finalize. Chunked (1024 rows per chunk), gzip level 6.

### CSV and HDF5 parity

The two backends receive the same metadata dict, the same columns and the same rows, so every key on this page exists in both. The differences:

| | CSV | HDF5 |
|---|---|---|
| Metadata values | Text, re-typed by `parse_metadata` | Typed attributes (bool, int, float, str, list); `None` is stored as an empty string |
| Floats in data rows | 6 significant figures | 10 significant figures, stored as strings |
| Header / footer separation | Two `#` blocks | One flat `attrs` namespace; a run that did not finish lacks `ended_at` |
| Compression | Optional gzip of the whole file at finalize | Always, internal |
| Map assembly | Read | Read, if `h5py` is installed on the machine doing the reading; otherwise the run is listed under `skipped` |
| Desktop app preview | `.csv` only; a `.csv.gz` is listed, not previewed | No (listed, not previewed) |

## Per-mode columns

### Resistance

| Column | Unit | What it is |
|---|---|---|
| `elapsed_s` | s | Time since Start |
| `V_meas` | V | Voltage measured at the DUT (across the probes if 4-wire) |
| `I_meas` | A | Current sourced (instrument-reported, matches the test-current setpoint) |
| `R_ohm` | Ω | Instrument-reported R — preserves Enhanced R / offset-comp / source-readback features (NOT `V_meas/I_meas` recomputed here). When **Cable Null** is active the `res_cable_null` offset is subtracted from this column only; `V_meas` and `I_meas` are written as the instrument measured them. |
| `R_unc_ohm` | Ω | Per-reading σ_R. When Enhanced R is on, this comes from the datasheet's Enhanced column; otherwise from V/I RSS propagation. |
| `compliance` | | `OK` on a normal reading, `V_COMP` when the voltage side hit compliance (see below) |
| `event` | | Empty, or the labels of the event marks made since the previous row, joined with `; ` |

Metadata `params`: `test_current_A`, `voltage_compliance_V`, `measurement_type`, `auto_range`, `auto_zero`, `offset_compensated_ohms`; plus one [`effective.*`](#effective-what-the-instrument-reported-back) key.

**How compliance is flagged in resistance mode.** It depends on the range mode, because the voltage limit does.

- **Manual range.** A row is `V_COMP` when `|V_meas| ≥ 0.99 ×` the effective voltage limit (`effective.voltage_compliance_V`), or when the status word's compliance bit is set. The 99 % rule is what does the work: on the 2400 and the 2420 the ohms function was seen on the bench (2026-09-18) never to set the compliance bit, and in manual range it reports the programmed current, so `R_ohm` under compliance is a plausible-looking wrong number. The bench showed 0.4998 V under a 0.5 V limit.
- **Auto range** (the default). There is no fixed limit to compare with: auto-ohms sets its own per ohms range (2.1 V was read back at configure time on a 2420 asked for 0.5 V, while the top ranges measure up to 20 V), so a healthy 1 MΩ DUT at 10 V is not in compliance. A row is `V_COMP` only when the status bit is set or when `V_meas` or `R_ohm` is the instrument's overflow value (`9.9e37`), which it returns past its top range.

The other continuous modes apply the 99 % test against their requested limit (`vsource_current_compliance`, `isource_voltage_compliance`, `fpp_voltage_compliance`) alongside the status bit.

The simulator does not model auto-ohms; the Auto range behavior rests on the bench observations above and on the instrument manual's range table, not on a simulated run.

### Voltage Source

| Column | Unit | What it is |
|---|---|---|
| `elapsed_s` | s | Time since Start |
| `V_set` | V | Sourced voltage (the setpoint — actual output is `V_set ± σ_V_source`) |
| `I_meas` | A | Measured current |
| `R_calc` | Ω | `V_set / I_meas` |
| `I_unc_A` | A | σ_I from the per-range current-measurement spec |
| `R_calc_unc_ohm` | Ω | σ_R via RSS through `R = V_set/I_meas` (combining V-source spec + I-measure spec) |
| `compliance` | | `OK` on a normal reading, `I_COMP` when the current side hit compliance |
| `event` | | Event-marker label written by the `M` keyboard shortcut |

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
| `compliance` | | `OK` on a normal reading, `V_COMP` when the voltage side hit compliance |
| `event` | | Event-marker label written by the `M` keyboard shortcut |

Per-reading uncertainty on Rs / ρ / σ is recoverable downstream: `σ_X/X = √((V_unc/V)² + (I_unc/I)²)`. The aggregated per-spot stats use a different formula combining statistical and instrument uncertainty — see [Concepts → Uncertainty](concepts.md#uncertainty-instrument-vs-statistical-vs-combined).

**Delta mode** splices four more columns before `compliance`:

| Column | Unit | What it is |
|---|---|---|
| `V_plus` | V | Voltage at `+I` polarity |
| `V_minus` | V | Voltage at `−I` polarity |
| `R_f` | Ω | Forward `V_plus / +I` |
| `R_r` | Ω | Reverse `V_minus / −I` |

Per the F84 §11.2.2.2 forward/reverse diagnostic — `R_f / R_r` should approach 1; large deviation flags thermoelectric or rectification effects.

Metadata `params`: `source_current_A`, `voltage_compliance_V`, `voltage_auto_range`, `probe_spacing_cm`, `thickness_um`, `k_factor`, `alpha`, `model`, `target_samples`, `auto_zero`. The F84 inputs (`fpp_diameter_cm`, `fpp_geometry`, `fpp_temperature_c`, `fpp_dopant_type`) and the delta-mode settings are **not** written to the header; delta mode shows in the file name (`_delta`) and in the extra columns. A run with a spot adds the [`spot.*`](#spot-four-point-runs-that-carry-a-spot) block, and every four-point run ends with [`spot_stats.*`](#spot_stats-every-four-point-run).

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

The final sheet resistance, resistivity, f-factors, Q ratios, and the §11.1 homogeneity result land in the **metadata** at finalize, not as data rows. They appear in the trailing `# --- run completed ---` block with dotted keys:

- `vdp_result.sheet_resistance` (Ω/□), `vdp_result.sheet_resistance_uncertainty`
- `vdp_result.rho_avg` (Ω·cm), `vdp_result.rho_avg_uncertainty`
- `vdp_result.rho_a`, `vdp_result.rho_b` (the two F76 group resistivities)
- `vdp_result.q_a`, `vdp_result.q_b`, `vdp_result.f_a`, `vdp_result.f_b`
- `vdp_result.homogeneous` (boolean — `true` when |ρ_A − ρ_B|/ρ_avg ≤ F76 §11.1 threshold)
- `vdp_result.asymmetry_pct` (the |ρ_A − ρ_B|/ρ_avg × 100 number)
- `vdp_result.current_a`, `vdp_result.thickness_cm`
- `vdp_result.voltages.<label>` — every raw V measurement, keyed by F76 geometry label

Metadata `params`: `source_current_A`, `voltage_compliance_V`, `voltage_auto_range`, `thickness_cm`, `settling_s`, `readings_per_polarity`, `auto_zero`, `standard: ASTM F76-08 Method A`.

### I-V Sweep

| Column | Unit | What it is |
|---|---|---|
| `point` | | Sweep index (0…N) |
| `V_source` | V | Sourced voltage at this step (for V-source sweeps) — for I-source sweeps this column is named `I_source` and the next is `V_meas` |
| `I_meas` | A | Measured current (for V-source sweeps) |
| `compliance` | | `OK` on a normal point, `COMP` when that point hit compliance |

Metadata `params`: `source_function`, `start`, `stop`, `step`, `compliance`, `delay_s`, `direction`.

## Reading the data back

A small example to load a v2.0 CSV with metadata into pandas:

```python
import pandas as pd
from resistamet_gui.data_export import parse_metadata

path = "measurement_data/alice/1789858524_wafer-07_4PP_1.00mA.csv"

# Metadata as a flat dict
meta = parse_metadata(path)
print(meta["mode"], meta["nplc"], meta["params.source_current_A"])
print(meta.get("spot_stats.rs.mean"), meta.get("spot_stats.rs.u_total"))

# Data rows — pandas skips the `#`-prefixed header automatically
df = pd.read_csv(path, comment="#")
print(df.head())
```

For HDF5:

```python
import h5py

with h5py.File(path[:-len(".csv")] + ".h5", "r") as f:
    print(dict(f.attrs))                # metadata
    data = f["data"][:]                 # structured numpy array
    print(data.dtype.names)             # column names
```

For per-spot 4PP summaries, the **Export Summary…** button on the 4PP tab writes a separate (non-v2.0) CSV with a header block and two sections:

```
4-Point Probe Summary
Sample,<sample-name>
User,<username>
Model,<thin_film | semi_infinite | finite_thin | finite_alpha>
Spacing s (cm),<value>
Thickness t (cm),<value>
Alpha,<value>

Metric,Mean,StdDev
Sheet Resistance (Ω/□),<mean>,<std>
Resistivity (Ω·cm),<mean>,<std>
Conductivity (S/cm),<mean>,<std>

Per-Spot Results
Spot,N,Rs Mean (Ω/□),Rs Std,Rs RSD%,ρ Mean (Ω·cm),ρ Std,σ Mean (S/cm),σ Std
<spot-name>,<n>,...
```

The "Per-Spot Results" section is only written when ≥1 spot has been saved with the **Save Spot** button. When ≥2 spots are saved, an **Inter-spot Uniformity** block is appended:

```
Inter-spot Uniformity
Rs Mean-of-Means (Ω/□),<value>
Rs Std-of-Means (Ω/□),<value>
Inter-spot RSD%,<value>
```

Mean/Std use `np.nanmean` / `np.nanstd(ddof=1)`; NaN and infinite values are written as the literal string `N/A`.

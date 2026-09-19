# Four-point probe: spots, sample geometry, and maps

**Status:** design, 2026-09-19. Steps 1–4 of §5 have landed: the geometry
math, the schema, the spot in the file and the events, and the map with its
API. Step 5 (PySide6) is next and needs no answers; steps 6–7 wait on the
questions in §7. The position-aware correction is reported, never applied:
the schema accepts only `warn` until question 1 is answered.
**Depends on:** `tauri_backend_split.md` (run layer, events, contract)

## 1. What is wrong today

A four-point-probe characterisation is rarely one number. The operator puts
the probe down in several places, and the paper reports the sheet resistance
per place and the spread between them. The software models none of that:

- A 4PP run is one placement: N samples, then auto-stop, one data file.
- "Save spot" exists in both UIs, but a spot is a row in a widget. The
  PySide6 app keeps spots in a Python list on the tab and writes them only if
  the operator remembers the separate *Save Summary* export. The Tauri UI
  keeps them in a browser-side store that is gone on reload.
- The data files of the individual runs do not say that they belong
  together, what the spot was called, or where on the sample it was. The
  per-spot mean, its uncertainty and the inter-spot spread cannot be
  reproduced from the archive without the operator's memory.
- The finite-size correction assumes the probe is at the centre of the
  sample. Nothing knows whether it was, so nothing can warn when a spot is
  close enough to an edge for that assumption to cost several percent.

The rule that fixed the rest of the application applies here too: the run
layer and the data files are the description of what was measured; a UI is
one client of it.

## 2. Three layers

Each layer is useful without the ones above it.

```
3. Map        optional picture of the sample; spots placed on it; a figure
2. Spots      a spot is part of the run's record: label, position, statistics
1. Geometry   sample shape + dimensions -> correction factor at any position
```

### Layer 1 — sample geometry (pure math, no UI, no instrument)

The settings already hold a shape (`fpp_geometry`: circle, square, rectangle
with L/W of 2, 3 or 4), a lateral dimension (`fpp_diameter_cm`), a thickness
and a probe spacing. They feed table look-ups that are valid for a probe at
the centre. The physics behind those tables has a closed form for a thin
sheet, and the closed form works at *any* position and orientation:

- **Circle.** The potential of a point current source in a disc with an
  insulating rim is the free-space logarithm plus one image at the inverse
  point. For probes at complex positions `A, P2, P3, B` (current in at `A`,
  out at `B`) on a disc of radius `R`:

  ```
  g(P, Q) = ln( |P - Q| * |R^2 - conj(Q) P| )
  Rs = 2*pi * (V/I) / [ g(P2,B) - g(P2,A) - g(P3,B) + g(P3,A) ]
  ```

  Evaluated at the centre this agrees with **all 21 rows of ASTM F84
  Table 3** (the `_F84_TABLE3_F2` tuple in `calculations.py`) to within
  0.0006; twenty rows round to the printed value, and the row at
  S/D = 0.085 gives 4.2656 against a printed 4.265.

- **Rectangle.** Images across all four insulating edges form a doubly
  periodic lattice. Summed in closed form along one axis
  (`ln|sin(pi (z - a) / 2W)|`, taken along the shorter side) the other
  axis converges exponentially for any aspect ratio. Evaluated at the centre
  this agrees with the Smits table in `calculations.py` to within 0.05 % —
  with three exceptions, which look like transcription errors in the table
  rather than physics:

  | table entry | table | series | difference |
  |---|---|---|---|
  | D/s = 32, all columns | 4.4878 / 4.4899 | 4.4997 / 4.5011 | 0.26 % (the table values are what the series gives at D/s ≈ 27.5) |
  | D/s = 1.25, L/W = 4 | 1.2248 | 1.2468 | 1.8 % (L/W = 3 in the same row is 1.2467) |
  | D/s = 2.0, L/W = 2 | 1.9475 | 1.9454 | 0.11 % |

  The table is left alone until the source has been checked against the
  original publication; the new functions do not use it.

What this buys:

1. **One source for the F84 inputs.** A `SampleGeometry` (shape, dimensions,
   thickness) replaces typing `fpp_diameter_cm` and picking `rectangle_3`:
   any aspect ratio, not three; the settings form can show the resulting
   correction factor before a run.
2. **Edge warnings that mean something.** For a spot at a known position the
   series gives the factor there; its ratio to the centred factor *is* the
   error made by assuming the centre. On a 20 s × 20 s square, with the array
   parallel to the nearest edge:

   | distance from edge | error if the centred factor is used |
   |---|---|
   | 5 s | 1.6 % |
   | 4 s | 2.9 % |
   | 3 s | 5.3 % |
   | 2 s | 11 % |
   | 1 s | 33 % |

   (The error is `factor_centre / factor_here - 1`, the fraction by which the
   reported Rs is too high — the `relative_error` the code records.) With
   the array pointing at the edge the same distances cost 0.3 %, 0.7 %,
   1.4 %, 3.1 % and 7.8 %. Orientation matters, so a spot carries one.
3. **An optional position-aware correction.** ASTM F84 measures at the
   centre; applying the factor for the actual position is outside the
   standard. It is offered as an explicit, labelled choice
   (`fpp_position_correction`: `warn` — default — or `apply`), and the file
   records which was used. It never happens silently.

Limits, stated in the UI where they apply: thin-sheet geometry (the
thickness factor F(w/S) stays the separate multiplicative term it is today,
as in F84); insulating edges; shapes are circle and rectangle. An irregular
sample gets spots and a map but no geometry factor beyond what the operator
enters by hand.

New module `calculations_geometry.py`, pure functions, no Qt, no pyvisa:
`probe_tips(centre, angle, spacing)`, `circle_factor(...)`,
`rectangle_factor(...)`, `position_effect(...) -> (factor_here,
factor_centre, relative_error)`, `edge_clearance(...)` (smallest distance
from any tip to the boundary, in units of s; negative = a tip is off the
sample). Tests pin the functions to the F84 and Smits tables.

### Layer 2 — spots as part of the record

A spot is: a label, an index within its map, an optional position
(`x_mm`, `y_mm` from the sample's centre) and array orientation
(`angle_deg`), and what was measured there.

**Decision: a spot is a run; a map is a set of runs that share a `map_id`.**
The alternative — one long run that pauses for the operator between
placements, as van der Pauw does — was considered and not chosen:

| | spot = run (chosen) | one run, many spots |
|---|---|---|
| measurement loop | unchanged; the code with hardware blast radius is not touched | new control flow inside the loop that owns the source output |
| output while the probe is lifted | off, because the run has ended | must be switched off and on inside a run; a new failure mode |
| a crash or an abort at spot 7 | six complete files | one file that needs an "incomplete map" notion |
| redo spot 3 | run it again; the newer run wins | needs an in-run "go back" |
| matches what operators do today | yes (Start per placement, auto-stop at N) | no |
| one file per map | no — a map is assembled from files | yes |

The cost of the choice is that a map is assembled rather than read; the
assembly is a pure function over file headers, and the summary below is
written once per map so nobody has to do it by hand.

What changes:

- **Request.** A four-point `RunRequest` may carry
  `spot: {map_id, index, label, x_mm?, y_mm?, angle_deg?}`. Absent, the run
  is what it is today.
- **File.** The v2 header gains a `spot` block with those fields, the
  `SampleGeometry`, and — when a position is known — the geometry factor
  used, the factor at the position, the relative error, the edge clearance
  in units of s, and which of `warn` / `apply` was in force. The footer gains
  the spot's statistics: n, mean and sample standard deviation of Rs, ρ, σ,
  and the combined (statistical ⊕ instrument) uncertainty from
  `four_point_combined_uncertainty`, which today only the GUI computes.
- **Events.** `spot_complete` at the end of a four-point run, carrying the
  same statistics, so both UIs show the backend's numbers instead of
  computing their own (the rule `derived` already follows for single
  samples). `geometry_warning` before the first sample when a tip is off the
  sample (refused) or the relative error exceeds `fpp_edge_warn_pct`
  (default 1 %).
- **Map summary.** `session/spot_map.py`: given a data directory and a
  `map_id`, read the headers and footers, keep the newest run per spot
  index, and return the spots with the inter-spot mean, standard deviation
  and RSD. Served at `GET /maps/{map_id}` and written as
  `<map_id>_map.json` beside the runs whenever a spot completes, so the
  archive holds the map, not the browser.
- **PySide6.** *Save Spot* keeps working as it does; it additionally passes
  a `map_id` and the spot label into the run settings, so files written from
  the shipping app are linked too. No new widgets there.
- **Contract.** `SpotRequest`, `SampleGeometry`, `SpotCompletePayload`,
  `SpotMap` exported to `contracts/`; the Tauri store in `state/spots.ts`
  becomes a cache of `GET /maps/{id}`.

### Layer 3 — the map (optional, Tauri UI)

The canvas is the sample outline drawn from the `SampleGeometry` — a circle
or a rectangle to scale. **A photograph is optional**: the map works on the
bare outline, and a picture, when given, sits under it.

- *Registration is the calibration.* With a photo, the operator drags the
  outline to fit the sample's edge in the picture (circle: centre + radius;
  rectangle: centre + rotation + size, aspect locked to the geometry).
  Because the real dimensions are known, that one gesture gives the scale,
  the origin and the edges. No separate scale bar, no two-point calibration.
- *Placing a spot.* Click where the probe is; a marker shows the four tips
  to scale at the map's array orientation, coloured by the edge warning
  before anything is measured. Start measures there. The run carries the
  position; the marker fills with the result when `spot_complete` arrives.
- *The figure.* Markers coloured by Rs, ρ or σ on a perceptually uniform,
  colour-blind-safe scale with a labelled colour bar; value labels optional;
  exported as PNG and SVG with the numbers in a CSV beside it. No
  interpolated heat-map by default: five spots do not define a field, and a
  smooth surface through them would claim more than was measured. A
  nearest-spot (Voronoi) fill is offered for dense maps, clipped to the
  outline.
- *The image is data.* It is copied beside the runs as
  `<map_id>_sample.<ext>` with its SHA-256 in the map summary; the original
  is never modified; registration is stored as numbers, not baked into
  pixels.

## 3. Settings

| key | meaning | default |
|---|---|---|
| `fpp_sample_shape` | `unbounded`, `circle`, `rectangle` | `unbounded` (today's behaviour) |
| `fpp_sample_diameter_mm` / `fpp_sample_width_mm` / `fpp_sample_length_mm` | lateral dimensions | — |
| `fpp_position_correction` | `warn` or `apply` | `warn` |
| `fpp_edge_warn_pct` | relative error that raises the warning | 1.0 |
| `fpp_array_angle_deg` | probe-array direction on the map | 0 |

`fpp_geometry` and `fpp_diameter_cm` keep working and are mapped onto the
new fields when the new ones are absent, so existing profiles produce
identical numbers (a test pins this).

## 4. What is deliberately not here

- Motorised stages. Positions come from the operator; the model does not
  preclude a stage filling them in later.
- Interpolated conductivity fields, contour lines, statistics over an
  interpolated surface.
- Shapes beyond circle and rectangle, conducting edges, thick-sample
  position corrections.
- Hall or van der Pauw maps. The spot and map model is 4PP-only until a
  second mode needs it.

## 5. Order of work

1. `calculations_geometry.py` with tests against the F84 and Smits tables.
   *(landed)*
2. `SampleGeometry` and `SpotRequest` in `schema/`; legacy keys mapped;
   contract export. *(landed: `schema/spots.py`; the `fpp_sample_*` keys feed
   the position check only — the per-sample correction still reads
   `fpp_geometry` / `fpp_diameter_cm` and the tables, and the resolver warns
   when the two describe different samples)*
3. Spot block in the file header, spot statistics in the footer and the
   `spot_complete` event; the statistics move from the two UIs into
   `session/`. *(landed: `session/spot_record.py`, `session/spot_stats.py`;
   `geometry_warning` refuses an off-sample spot before the instrument is
   opened. The UIs still compute their own numbers until steps 5–6 switch
   them to the event.)*
4. `session/spot_map.py`, `GET /maps/{id}`, the map summary file. *(landed,
   with `GET /maps` and `contracts/maps.schema.json`)*
5. PySide6 *Save Spot* passes `map_id` and label through. The run procedure
   reads the spot from `settings['spot']`, so this is one dict in
   `gather_settings_for_mode`'s caller and no change to the worker.
6. Tauri: spots panel reads the map; settings form shows the geometry and
   the factor.
7. Tauri: the map canvas, registration, figure export.

Steps 1–5 change no measurement behaviour and can land before any of the
questions below are answered. Steps 6–7 depend on them.

## 6. Verification

- Geometry: table agreement as above; symmetry (mirror positions give equal
  factors); the factor tends to π/ln 2 as the sample grows; a tip outside
  the outline is rejected.
- Spots: a simulator run with a `spot` block round-trips through CSV and
  HDF5; the footer statistics equal those computed from the rows; a legacy
  profile yields byte-identical rows to today.
- Map: three simulated spots plus a repeat of spot 2 assemble into three
  spots with the newer spot 2; inter-spot RSD matches a hand calculation.

## 7. Questions for Brenden

1. **Position-aware correction:** is `warn` by default with `apply` as an
   explicit opt-in the right stance for the paper, or should `apply` not
   exist at all so every number in a file is F84-as-written?
2. **Array orientation:** one angle per map (the probe head is usually
   mounted one way), or per spot?
3. **Map identity:** is a map tied to the *sample name* already in the run
   settings (so a second session on the same sample continues the map), or
   is every mapping session its own map?
4. **Figure defaults:** which quantity should the figure show first — σ, Rs
   or ρ — and do you want value labels on by default?
5. **Irregular samples** (cleaved pieces): spots and the picture without
   any geometry factor — acceptable, or do you want a polygon outline with a
   numerical solution later?
6. **The three Smits table entries** in §2: do you have the original table
   to check them against? If they are typos the look-up values shift by up
   to 1.8 % for one narrow case and 0.26 % for D/s = 32.

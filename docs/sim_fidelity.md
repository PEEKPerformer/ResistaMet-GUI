# Simulator fidelity

The ResistaMet test suite uses a stateful in-memory Keithley 2400-series
simulator (`tests/fakes/fake_keithley.py`) so the full SCPI logic surface can
be exercised in CI without a real instrument. This document records:

1. Which simulator behaviors are validated against captured hardware traces.
2. Which behaviors are intentionally simplified (and what tests cover the
   gaps via real hardware).
3. How to recapture the golden traces when firmware drifts or new scenarios
   are needed.

## Reference instruments

Captures, quirk validation, and trace replay were performed against
two physical instruments from the Keithley 2400 family:

| Field | Primary (capture source) | Cross-model validation |
|---|---|---|
| Model | Keithley 2420 (3 A SourceMeter) | Keithley 2400 (1 A SourceMeter) |
| Serial | 1230523 | 1175680 |
| Firmware | C30, March 17 2006 | C30, March 17 2006 (identical) |
| Option codes | /H/L | /K/J |
| Interface | GPIB-USB-HS, address 24 | GPIB, address 3 |
| Line frequency | 60 Hz | 60 Hz |
| DUTs | 100 Ω (99.53 Ω), 10 kΩ (9914 Ω), 1 MΩ (1.029 MΩ) | 100 Ω (99.50 Ω) |

**Cross-model evidence**: every committed SCPI fixture (29 traces
across 100 Ω / 10 kΩ / 1 MΩ DUTs) and every quirk-trigger test
captured from the 2420 reproduces byte-equivalent (configuration
queries) and within 5 % / exact-on-compliance-bit (measurement
queries) on the 2400 — **29/29 fixtures and 6/6 quirks pass
cross-model** (validated 2026-04-30). Cross-model pass counts by
operating regime: 15/15 at 100 Ω (mA / 0.1 V), 8/8 at 10 kΩ (100 µA /
1 V, including the 2-wire and negative-source-V variants), 6/6 at
1 MΩ (µA / 1 V). The two instruments share firmware C30 but differ in
model, serial number, current rating, and option codes; the fixtures
and the simulator are therefore validated against the 2400 *family*
across the full operating envelope of the production code, not only
one specific unit at one operating point.

Three decades of resistance are exercised:

- **100 Ω at 1 mA / ~0.1 V** — lowest current range, source-V mode primarily
- **10 kΩ at 100 µA / ~1 V** — middle range, plus 2-wire and negative-source-V variants
- **1 MΩ at 1 µA / ~1 V** — lowest source-current range, exercises the µA sense regime

The simulator passes every trace without per-DUT or per-range code
changes — the trace-replay test only pins the STAT compliance bit, so
the per-mode STAT baselines that differ across ranges don't trigger
mismatches.

The simulator targets the broader 2400/2410/2420/2425/2430/2440/2450 family.
Bits encoded in the FORM:ELEM `STAT` element (notably the compliance bit at
position 3) are 2400-series specific; the 2450 may differ.

## What the simulator models

Validated against captured traces (`tests/fixtures/scpi_traces/`) and against
live hardware quirk triggers:

- **Default state after `*RST`**: 33 settable properties match the real
  instrument's documented and observed defaults (line frequency, sense
  function, compliance limits, source ranges, output mode, etc.). Verified
  by `baseline_reset_state.json`.
- **Five measurement modes** (resistance, source‑V, source‑I, four‑point
  probe, voltage sweep): full configuration round‑trips and `:READ?` element
  layouts match. Verified by per‑mode trace fixtures.
- **Source value echoing under compliance**: in source-V mode, the VOLT
  element of `:READ?` echoes the programmed setpoint even when the
  instrument is internally clamping current at the compliance limit. The
  CURR element reflects the actual clamped current. Symmetric for source-I
  mode. Verified by `compliance_v_in_compliance.json`.
- **STAT compliance bit**: bit 3 of the `STAT` element of `:READ?` is set
  iff the source is in real compliance. (Note: this is *not* the same bit
  as the `+114` Measurement Event Register entry at bit 14 documented in
  the manual's Table B-1 — the FORM:ELEM STAT element uses a different bit
  layout. The simulator has `_STAT_BIT_COMPLIANCE = 1 << 3` and per-mode
  baselines empirically observed on the 2420.) Verified by
  `tests/hardware/test_quirk_triggers.py::test_compliance_bit_is_bit_3`.
- **FORM:ELEM canonical re-ordering quirk**: argument *order* in
  `:FORM:ELEM` is silently re-ordered by the instrument; argument *set* is
  honored. Verified by `quirk_form_elem_canonical_order.json`.
- **Auto-ohms rejection (error 825)**: after `:SENS:FUNC 'RES'`, attempts
  to write `:SOUR:CURR`, `:SOUR:CURR:RANG`, or `:SENS:VOLT:PROT` queue
  error 825 "Invalid with auto-ohms on" until `:SENS:RES:MODE MAN` is sent.
  Verified by `quirk_auto_ohms_rejects_source.json`.
- **`:INIT:CONT` undefined header (error -113)**: the `:INIT:CONT`
  subsystem does not exist on the 2400 series; `:READ?` performs init +
  trigger + fetch atomically. Verified by `quirk_init_cont_unsupported.json`.
- **Concurrent measurements default ON**: after `*RST`,
  `:SENS:FUNC:CONC` is `1`, costing 3× measurement time unless explicitly
  disabled. Verified by `quirk_concurrent_default_on.json`.
- **Hardware averaging filter**: `:SENS:AVER`, `:SENS:AVER:TCON`,
  `:SENS:AVER:COUN` round-trip values correctly. Verified by
  `filter_repeat_x10.json`.
- **Offset-compensated ohms**: `:SENS:RES:OCOM ON/OFF` round-trip and
  configuration sequence. Verified by `offset_compensated_ohms.json`.
- **Output-off HIMP mode**: `:OUTP:SMOD HIMP/NORM` round-trip. Verified by
  `output_off_himp.json`.
- **Sweep engine**: `:SOUR:VOLT:MODE SWE` plus `:SOUR:VOLT:START/STOP/STEP`
  plus `:TRIG:COUN N` causes one `:READ?` to return N consecutive
  (V, I, STAT) triples. Verified by
  `sweep_v_0_to_0p5v_5pts_into_100ohm.json`.
- **Error queue FIFO**: `:SYST:ERR?` returns the oldest queued error in
  `<code>,"<message>"` format with no leading `+` on positive codes;
  `0,"No error"` when empty.

## What the simulator does NOT model

These gaps are documented here so reviewers know where the hardware tier
provides the only safety net.

- **Real measurement noise**: the simulator computes V/I from
  `dut_resistance_ohms` via Ohm's law. Real readings exhibit noise on the
  order of 0.05% at NPLC=1 on the 2420. Tests that compare simulator
  output to captured hardware traces use a 5% tolerance for V/I and only
  require exact agreement on the STAT compliance bit.
- **NPLC and source-delay timing**: the simulator returns `:READ?`
  responses immediately. It does not delay by `(NPLC × line_period)` or
  honor `:SOUR:DEL`. Tests that need timing fidelity must run on real
  hardware.
- **Filter time integration**: `:SENS:AVER ON` is recorded as a state flag
  but does not actually average multiple internal samples; the simulator
  returns the same single computed value either way.
- **Range-dependent precision and clamping**: real instruments quantize
  to range-dependent step sizes and reject inputs outside the active
  range. The simulator accepts arbitrary floats.
- **Thermoelectric EMF**: configurable via `dut_voltage_offset`, but
  the simulator does not introduce drift or temperature dependence.
- **`:STAT:MEAS:COND?` and other status-register subsystems**: the
  simulator returns `0` (a placeholder); tests that need the Measurement
  Event Register layout must run on real hardware.
- **Front-panel state, calibration data, save/recall slots**: ignored by
  the simulator.
- **Delta mode hardware capture**: the production worker's current-
  reversal path (`+I → :READ? → -I → :READ? → +I`) is exercised in CI by
  `test_workers.py::test_four_point_delta_mode_alternates_polarity`, which
  asserts the right SCPI command sequence is sent. We do not currently
  ship a hardware trace for delta mode; an attempt at 10kΩ exposed a
  polarity asymmetry in the lab Kelvin wiring that was wiring-specific
  (current direction reversal hit voltage-protection compliance) rather
  than instrument-specific, so the trace was not committed. A future
  capture on a known-symmetric setup would close this gap.

## Test tier overview

| Tier | Hardware required | Files | Pass count |
|---|---|---|---|
| Pure unit | No | `test_buffers.py`, `test_calculations.py`, `test_config.py`, `test_data_export.py`, `test_system_utils.py`, `test_widgets.py` | (existing) |
| GUI smoke | No (offscreen Qt) | `test_gui_smoke.py` | (existing) |
| Trace replay | No | `test_fake_matches_hardware.py` | 30/30 (29 traces + 1 sanity) |
| SCPI wrapper | No | `test_instrument.py` | 21/21 |
| Worker integration | No | `test_workers.py` | 17/17 |
| Hardware quirks | Yes | `tests/hardware/test_quirk_triggers.py` | 6/6 on 2420 + 6/6 on 2400 (validated 2026-04-30) |
| Hardware recapture | Yes | `tests/hardware/test_recapture_traces.py` | 29/29 on 2420 + 29/29 cross-model on 2400 (validated 2026-04-30); set `RESISTAMET_DUT_OHMS` to filter |

The CI pipeline (`.github/workflows/`) runs the four no-hardware tiers on
every push. The two hardware tiers run locally before each release with
the bench instrument connected:

```bash
RESISTAMET_HARDWARE_ADDR=GPIB0::24::INSTR pytest tests/hardware/ -v
```

## Community cross-model submissions

Anyone with a Keithley 2400-family instrument can run
``scripts/community_capture.py`` against a 100 Ω, 10 kΩ, or 1 MΩ
4-wire Kelvin reference DUT and submit the resulting SCPI traces via
the ``Keithley compatibility`` issue template. Accepted submissions
land under ``tests/fixtures/scpi_traces_community/<model>_<serial>/``
and are automatically replayed through the simulator by
``tests/test_community_traces.py`` on every CI run.

When a submitted trace fails the simulator, that's evidence of real
cross-model variance — the maintainers either update the simulator to
handle it (with the trace as the regression fixture) or document the
divergence here as a known gap. Either way, the testable surface
grows.

The model spec table in ``resistamet_gui/instrument.py`` (mirrored in
``scripts/community_capture.py`` so the script can run without the
project installed) is updated as new models are confirmed by
submissions.

## Recapturing golden traces

When firmware changes or new measurement scenarios need coverage:

1. Wire the reference DUT (default: 100 Ω resistor in 4-wire Kelvin).
2. Run the capture script with the instrument connected:

   ```bash
   python tests/hardware/capture_traces.py
   ```

   New JSON fixtures appear under `tests/fixtures/scpi_traces/`.

3. Re-run the simulator validation:

   ```bash
   pytest tests/test_fake_matches_hardware.py -v
   ```

4. If the simulator now diverges, update `tests/fakes/fake_keithley.py`
   to match the new captures, then iterate until both
   `test_fake_matches_hardware.py` (CI) and `tests/hardware/` (bench)
   pass.

## Why this design

`pyvisa-sim`, the standard simulation package, supports static SCPI
dialogues but cannot easily produce computed responses (`:READ?` values
based on the configured source mode and DUT model, sweep response
synthesis, the auto-ohms quirk's conditional error queueing). A custom
stateful fake bypasses the YAML format entirely while preserving the
PyVISA `ResourceManager.open_resource` interface, so the production code
under test runs unchanged in CI.

Captured hardware traces rather than hand-written expected values
means a simulator regression shows up as a divergence from a recorded
instrument dialogue, not from an opinion about correct behavior. The
trade-off: updating the simulator requires bench access to re-capture,
not just an edit to a fixture file.

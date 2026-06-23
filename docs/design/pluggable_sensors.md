# Pluggable Auxiliary Sensors — Design

**Status:** Design spec + commit-1 landed
**Target:** ResistaMet-GUI v1.13
**Date:** 2026-06-23

> **The goal is generality.** A researcher should be able to wire *any* secondary instrument —
> thermocouple, strain/stress gauge, flow meter, hygrometer, anything that produces a number — and
> have its readings timestamped into the same record as the Keithley measurement, with data columns
> and a live readout that appear *automatically*. Nothing downstream is sensor-specific.
>
> An Arduino K-type thermocouple (co-logging temperature with 4PP) is the motivating first use and the
> first shipped driver, but it has no privileged status — it is one implementation of the contract and
> the smallest worked example of how to write another.

---

## 1. The genericity mechanism

A sensor **declares its own channels**, and everything downstream is built by iterating that
declaration:

```python
@dataclass(frozen=True)
class SensorChannel:   # key="strain", label="Strain", unit="µε"
    key: str; label: str; unit: str
```

- The **per-point row** gets one `aux_<key>` column per channel (+ `aux_<key>_flag` if flagged).
- The **CSV/HDF5 headers** are derived from `aux_column_names(sensor)` — the exporter never sees a
  sensor type.
- The **live readout** renders `f"{label}: {value} {unit}"` per channel.

Add a flow meter that declares `[SensorChannel("flow", "Flow", "mL/min")]` and it flows through the
worker → exporter → UI with **zero new code**. (Proven by `DummyFlowSensor` in `tests/test_sensors.py`
— a non-thermocouple, non-serial sensor that registers and round-trips through the generic glue.)

---

## 2. The contract (`resistamet_gui/sensors.py` — landed)

```python
@dataclass(frozen=True)
class SensorReading:
    timestamp: float
    values: dict[str, float]            # keyed by channel key
    flags: dict[str, int] = {}          # 0 == OK; .ok is the AND
    @property
    def ok(self) -> bool: ...

@runtime_checkable
class AuxiliarySensor(Protocol):
    def open(self) -> "AuxiliarySensor": ...
    def channels(self) -> list[SensorChannel]: ...   # the self-description
    def read_latest(self) -> SensorReading: ...       # flush stale, return freshest
    def close(self) -> None: ...
```

Four methods. A driver need not be serial or VISA — anything that can produce a `SensorReading`
qualifies. A faulted reading is **returned and flagged**, never silently dropped.

**Reusable base for the common case** — `SerialLineSensor(VisaInstrument)`: for sensors that stream
delimited ASCII over serial (ASRL). Subclass declares `CHANNELS` + implements `parse_line()`; the base
handles connection (inherited → the `--simulate` seam works unchanged), **freshness** (`read_latest`
drains buffered input then reads the newest line, so the value reflects the read instant), and
**resync** (skips partial/garbage lines until one parses, bounded by `MAX_READ_ATTEMPTS`).

**Registry** — a plain `name → class` dict mirroring `instrument._MODELS`:
```python
register_sensor(name, cls)        # third-party packages call at import; in-tree drivers registered here
available_sensors() -> tuple
make_sensor(driver, address, **opts) -> AuxiliarySensor
```
No plugin framework, no entry points yet. A setuptools entry-point group can replace this lookup later
*without changing the contract* — deferred until a real second in-tree driver exists.

**Generic glue** (used by worker/exporter): `aux_column_names(sensor)`, `reading_to_columns(reading)`.

---

## 3. First driver: Arduino K-type thermocouple

`ArduinoThermocouple(SerialLineSensor)`, `ASRL6::INSTR`. Probed live on the rig: it streams ~2 Hz,
CRLF-terminated, **no query protocol** (passive streamer), native-USB CDC (baud is don't-care):

```
DATA,21.227,22.734,0,0\r\n
```
`DATA,<K-type tip °C>,<MAX31855 cold-junction °C, diagnostic>,<fault>,<status>`. Declares two channels
(`t_sample`, `t_coldjunction`, both °C); fault flag (field 3) marks the reading not-ok. First line
after port-open can be partial (auto-reset) — parser resyncs on `DATA,`.

**Packaging fix (landed):** `pyserial>=3.4` declared in `pyproject.toml` — pyvisa-py needs it
transitively for ASRL; without it a clean install enumerates GPIB but fails to open `ASRL6` (same bug
class as the pyqtgraph omission fixed in `bc80d7f`).

---

## 4. GUI & worker wiring (commit 2/3 — verified idioms, file:line)

All additive, behind an `fpp_log_temp`-style flag, so the no-sensor path is untouched and existing CSVs
stay byte-identical when off.

- **Checkbox + address field** on the 4PP Advanced form — mirror the **delta-mode** idiom exactly
  (`main_window.py:701-714`): a `QCheckBox` whose `.toggled` enables a dependent `QLineEdit`.
- **Settings gather** — add to the 4PP branch of `gather_settings_for_mode` (`main_window.py:1935-1966`)
  using the existing `hasattr(widget, ...)` guard pattern.
- **Worker holds the sensor** — `MeasurementWorker` stashes optional config into `self._fpp_*` at
  config time (`workers.py:320-333` precedent); construct `make_sensor(...).open()` alongside
  `self.keithley` (`workers.py:142`); in the 4PP read loop merge `reading_to_columns(sensor.read_latest())`
  into `data_dict` (`workers.py:763`) and `row_data` (`workers.py:823-935`); close it in `_cleanup`
  (`workers.py:1129-1137`). This second-instrument read is the most invasive piece — keep it guarded.
- **CSV columns** — add conditionally in `get_column_config` via the **same conditional-splice
  delta-mode uses** (`data_export.py:233-239`), names from `aux_column_names`. Record sensor driver +
  channels in `build_metadata` 4PP params (`data_export.py:308-320`).
- **Live monitor** — a `QLabel` + a `QTimer(500ms)` mirroring `_readout_timer` (`main_window.py:51-53`),
  started/stopped in the same three lifecycle spots (`main_window.py:2202`, `2219`, `2616`). Renders the
  channel values for eyeballing equilibration. No gating.
- **aux-vs-time / ρ-vs-aux plot** — the one bespoke bit: 4PP has no `PgLiveCanvas` (it uses
  `HistogramCanvas`; `update_active_plot` early-returns for four_point at `main_window.py:2401`). Mount a
  `PgLiveCanvas` in the 4PP right panel (`main_window.py:813-817`) and drive it manually from the
  four_point branch of `update_data` (`main_window.py:2275-2309`). Still additive.

---

## 5. Domain note: temperature ≠ the framework

The framework just **co-logs** declared channels; it is not temperature-aware. The 4PP physics guidance
that applies *when a temperature channel is present* — delta-mode on (thermoelectric EMF cancellation),
ASTM F84 F_T correction off (it would normalize out the very T-dependence you're measuring) — stays the
**operator's** 4PP setting. Optionally surface a one-time hint when a channel with unit `°C` is
registered. Do **not** hard-couple that enforcement to "a sensor is attached" — a flow meter implies
nothing about delta-mode.

---

## 6. Simulator & tests

- **Landed (commit 1):** `tests/test_sensors.py` — 23 tests: thermocouple parse truth table
  (valid / partial / wrong-arity / non-numeric / trailing-`\r`), `read_latest` freshness+resync+errors,
  registry, generic glue, and the `DummyFlowSensor` genericity proof. Pure, hardware-free, sub-second.
- **Next (commit 1b/2):** a `FakeSerialSensor` device returned by `FakeResourceManager.open_resource`
  for `ASRL6` (add the address to `list_resources()` **and** branch `open_resource` before the GPIB
  check); couple its streamed temperature into `FakeKeithley`'s resistance model so `--simulate`
  produces coherent co-logged data with no hardware. `--sim-aux-*` flags mirroring `--sim-resistance`.
- One hardware-marked test over `ssh resistamet`: real open + read + parse on COM6 (the only tripwire
  for the auto-reset / partial-first-line behavior `--simulate` can't reproduce).

---

## 7. Staging

- **Commit 1 — generic sensor module + tests (LANDED).** `sensors.py` (contract, `SerialLineSensor`,
  `ArduinoThermocouple`, registry, glue); `tests/test_sensors.py` (23 passing); `pyserial` declared.
  No GUI, no worker changes, zero blast radius.
- **Commit 1b — simulator support.** `FakeSerialSensor` + `FakeResourceManager` branch + `simulator.py`
  / `__main__` sim flags. Hardware-free end-to-end exercise of a sensor.
- **Commit 2 — wire into the 4PP run.** Worker holds an optional `AuxiliarySensor`; `aux_*` columns via
  conditional splice; checkbox + address widgets; settings gather; metadata; e2e test under `--simulate`.
  First real co-logged data. Bench-verify the existing 4PP delta path is unchanged on the 2420.
- **Commit 3 — live monitor + plot.** Generic per-channel readout (QTimer) + the 4PP-panel canvas.
- **Commit 4 — docs.** "How to add your own sensor" with `DummyFlowSensor` / `ArduinoThermocouple` as
  worked examples; the thermocouple wire-protocol note.
- **Deferred to v2.0:** aux logging for the other measurement modes (same sensor injection); setuptools
  entry-point discovery (trigger: a real third-party driver); the dual-bus multi-instrument publication
  claim, of which this is the concrete engine.

---

## 8. Open questions & risks

**Open questions (low-stakes):** meaning of thermocouple field 4 (`status`) — carried as a flag
regardless; whether ~2 Hz is enough (fine for manually-triggered 4PP spots); whether aux logging is
wanted on continuous-resistance / I-V modes too (easy later via the same injection).

| Risk | Severity | Mitigation |
|---|---|---|
| Second-instrument read inside `MeasurementWorker` touches connect/loop/cleanup | Med | Guarded behind the flag; non-sensor path byte-for-byte unchanged; bench-verify 4PP delta on the 2420 |
| Native-USB auto-reset → partial first line / brief gap on open | Med | `read_latest` drains + bounded resync; parser keys on the record tag |
| Faulted/unplugged sensor logs a plausible number | Med | Flags returned + `.ok`; flagged columns; faded point |
| Unconditional column addition would break existing 4PP CSVs | Med | Conditional splice (delta-mode precedent); columns only when logging is on |
| `pyserial` undeclared → ASRL fails on clean install | **Fixed** | Declared in commit 1 |
| `--simulate` can't reproduce real auto-reset timing | Low | One hardware-marked test over `ssh resistamet` |

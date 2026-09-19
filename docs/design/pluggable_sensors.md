# Pluggable Auxiliary Sensors — Design

**Status:** Implemented — sensor layer, simulator devices, run wiring for every continuous mode, and a
live readout. §7 lists what is not built.
**Target:** ResistaMet-GUI v1.13
**Date:** 2026-06-23 (updated 2026-09-19)

> **Where this sits.** This serves the broader **Characterization Bench** vision: a Keithley-centric
> operando layer in which the Keithley measurement is the spine and auxiliary data sources synchronize
> onto it. A user may have all, none, or more auxiliary equipment.
> - **Mode-agnostic co-logging** — aux logging works on every continuous mode (resistance, source-V,
>   source-I, 4PP). Sweep (atomic) and vdP (manual) are excluded by design
>   (`data_export.AUX_LOG_MODES`).
> - **Global config** — one aux-source section in the Settings dialog (enable / driver / address),
>   stored as `aux_log_enabled` / `aux_driver` / `aux_address`.
> - **`StreamSensor`** — a generic multi-channel driver whose channels are self-described by the device
>   (`HDR,key:unit,...` then positional `DATA` rows) and discovered dynamically. This is the general
>   "one serial link, many declared channels" case; the thermocouple remains the fixed-channel example.
> - The live readout is a single label on the 4PP tab. Per-tab readouts for the other modes, an aux
>   plot, actuator/source drivers and multi-rate timeline fusion are future work.

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

- The **per-point row** gets one `aux_<key>` column per channel plus a single `aux_fault` provenance
  column:
  - `"0"` when clean;
  - `"key=v;…"` listing the nonzero flags. Values are preserved and *marked* on fault, never
    replaced. A stream value that arrives as `nan` or `inf` is kept and flagged `key=1`;
  - `"read_error"` when there was no fresh reading at all. Only then are the channel values NaN.
- The **CSV/HDF5 headers** are derived from `aux_column_names(sensor)` — the exporter never sees a
  sensor type.
- The **live readout** renders `label: value unit` per channel.

Add a flow meter that declares `[SensorChannel("flow", "Flow", "mL/min")]` and it flows through the
run → exporter → UI with **zero new code**. (Proven by `DummyFlowSensor` in `tests/test_sensors.py`
— a non-thermocouple, non-serial sensor that registers and round-trips through the generic glue.)

---

## 2. The contract (`resistamet_gui/sensors.py`)

```python
@dataclass(frozen=True)
class SensorReading:
    timestamp: float
    values: dict[str, float]            # keyed by channel key
    flags: dict[str, int] = {}          # 0 == OK; .ok is the AND
    @property
    def ok(self) -> bool: ...
    def age_s(self, now: float) -> float: ...   # seconds since timestamp

@runtime_checkable
class AuxiliarySensor(Protocol):
    def open(self) -> "AuxiliarySensor": ...
    def channels(self) -> list[SensorChannel]: ...   # the self-description
    def read_latest(self) -> SensorReading: ...       # NON-BLOCKING newest-cached read
    def close(self) -> None: ...
```

Four methods. A driver need not be serial or VISA — anything that can produce a `SensorReading`
qualifies. A faulted reading is **returned and flagged**, never silently dropped.

**Reusable base for the common case** — `SerialLineSensor(VisaInstrument)`: for sensors that stream
delimited ASCII over serial (ASRL). A subclass declares `CHANNELS` and implements `parse_line()`; the
base handles:

- **connection**, inherited, so the `--simulate` seam works unchanged. Optional `baud_rate`,
  `data_bits`, `parity`, `stop_bits` and `termination` arguments are set on the VISA session in
  `open()`; any left out stay at the backend's default. They are constructor arguments only — no
  setting or dialog field carries them yet (§7);
- a **background reader thread**, started by `open()`. It consumes the stream and caches the newest
  parsed reading, so `read_latest()` is a non-blocking cache read and the acquisition loop never waits
  on the serial link;
- **staleness**: a cache older than `AUX_STALE_AFTER_S` (5 s) raises. Inside that window
  `read_latest()` keeps returning the last reading unchanged; `SensorReading.age_s(now)` tells a
  caller how old it is. The run does not record that age yet (§7);
- **resync**: the reader skips partial and non-conforming lines.

`wait_ready()` / `wait_for_reading()` give threads that *can* block (the run thread) a bounded wait.
`open()` does a VISA connect on the calling thread, and `close()` can take up to 1 s: it closes the
session, then waits that long for the reader. A backend that does not abort a pending read on close
leaves the reader (a daemon thread) alive past `close()`; it exits when its read returns, without
touching the sensor.

**Channel keys** declared by a device are checked in `parse_stream_header`: a letter followed by
letters, digits or underscores, no duplicates, and not a key whose `aux_<key>` column the record
already has (today that is `fault`; the set is computed from the exporter's column tables). A header
that fails raises `SensorHeaderError` naming the key; `StreamSensor` keeps reading, and `wait_ready()`
reports the reason if no usable header follows.

**Registry** — a plain `name → class` dict mirroring `instrument._MODELS`:
```python
register_sensor(name, cls)        # third-party packages call at import; in-tree drivers registered here
available_sensors() -> tuple
make_sensor(driver, address, **opts) -> AuxiliarySensor
```
No plugin framework, no entry points yet. A setuptools entry-point group can replace this lookup later
*without changing the contract*.

**Generic glue** (used by the run and the exporter): `aux_column_names(sensor)`,
`reading_to_columns(reading)`, `format_fault(flags)`.

---

## 3. Shipped drivers

### `arduino_thermocouple` — `ArduinoThermocouple(SerialLineSensor)`

An Arduino with a MAX31856 K-type thermocouple board, addressed as an ASRL VISA resource (the default
`aux_address` is `ASRL6::INSTR`). Observed on hardware: it streams at about 2 Hz, CRLF-terminated,
with **no query protocol** (a passive streamer), over native-USB CDC, so the baud rate is ignored:

```
DATA,21.227,22.734,0,0\r\n
```
`DATA,<K-type tip °C>,<MAX31856 cold-junction °C, diagnostic>,<fault>,<status>`. Declares two channels
(`t_sample`, `t_coldjunction`, both °C); a nonzero fault (field 3) or status (field 4) marks the
reading not-ok. Opening the port resets the board: it prints a banner line and `READY`, and takes
about 2 s before data flows. The reader resyncs on `DATA,` past those lines and past a partial first
line.

### `stream_sensor` — `StreamSensor(SerialLineSensor)`

```
HDR,pressure:psi,t_sample:degC,force:N
DATA,32.5,24.1,0.98
```
The reader thread captures the header, so `open()` does not wait for it; `channels()` is empty until
then and `wait_ready()` blocks for it. The driver never writes to the device, and it only sees a
header that arrives after the port is open. Rows that arrive before the header are skipped. The first
usable header wins; a header repeated later is ignored. Whether a given device resends its header on
open is an open question (§8).

**Packaging:** `pyserial>=3.4` is declared in `pyproject.toml` — pyvisa-py needs it for ASRL; without
it a clean install enumerates GPIB but fails to open a serial resource.

---

## 4. Run and GUI wiring

Everything is behind `aux_log_enabled`, so the no-sensor path is untouched and a CSV written with
logging off has the plain schema (`tests/test_e2e_aux_4pp.py`).

- **Settings** — `aux_log_enabled` (default off), `aux_driver`, `aux_address` in
  `DEFAULT_SETTINGS['measurement']`, typed in `schema/settings_common.py`, edited in the Settings
  dialog (the driver list comes from `available_sensors()`). The resolver reports `aux_log_enabled` as
  an issue for a mode outside `AUX_LOG_MODES`.
- **The run holds the sensor** — `ContinuousRun._open_aux_sensor` (`session/continuous_run.py`) runs
  after the instrument is configured and before the output is switched on. It opens the sensor, waits
  up to `AUX_READY_TIMEOUT_S` (5 s) for channels and a first reading, and emits `aux_connected` with
  the declared channels. If that fails the run ends with an `aux_connect_failed` error and the output
  is never enabled. `_cleanup` closes the sensor.
- **Per point** — `reading_to_columns(sensor.read_latest())` is merged into the sample. Any exception
  there writes NaN values with `aux_fault = read_error` and the run carries on. A warning is emitted
  when the fault value changes to something other than `0`, not per row.
- **Columns** — `get_column_config(..., aux_columns=, aux_units=)` splices the aux block in before
  `compliance`/`event` and after any delta-mode columns; rows use `splice_before_tail` with the same
  anchor. Names come from `aux_column_names`.
- **Metadata** — `params.aux_sensor_driver`, `params.aux_sensor_address`, `params.aux_channels`.
- **Live readout** — one label on the 4PP tab. While idle with that tab showing, a preview sensor is
  opened and repainted every `AUX_PREVIEW_INTERVAL_MS` (500 ms). The port stays open across tab
  switches, so a native-USB board is not reset each time. After `AUX_PREVIEW_GIVEUP_TICKS` (6)
  consecutive empty ticks the preview gives up. Start closes the preview so the run can open the
  port, and the preview reopens when the run thread has finished. During a run in any co-logging mode
  the same label shows the in-run values, with a fault marker when `aux_fault` is not `0`.

---

## 5. Domain note: temperature ≠ the framework

The framework just **co-logs** declared channels; it is not temperature-aware. The 4PP physics guidance
that applies *when a temperature channel is present* — delta-mode on (thermoelectric EMF cancellation),
ASTM F84 F_T correction off (it would normalize out the very T-dependence you're measuring) — stays the
**operator's** 4PP setting. Do **not** hard-couple that enforcement to "a sensor is attached" — a flow
meter implies nothing about delta-mode. No hint is shown for a `°C` channel today.

---

## 6. Simulator & tests

- **Simulator** — `FakeResourceManager` lists and opens two serial devices besides the SMU:
  `FakeSerialSensor` (the thermocouple line format, centred on `--sim-temp`) at `--sim-aux-address`
  (default `ASRL6::INSTR`), and `FakeStreamSensor` (header, then three channels) at
  `--sim-stream-address` (default `ASRL7::INSTR`). The simulated temperature is **not** coupled into
  `FakeKeithley`'s resistance, so simulated co-logged data shows no temperature dependence.
- **`tests/test_sensors.py`** — pure, hardware-free: the thermocouple parse truth table; reader
  freshness, staleness, reading age and resync; `close()` against a read the backend does and does not
  abort; the registry; the fault-column glue; the `DummyFlowSensor` genericity proof; the column
  splice for every co-logging mode; stream header validation (reserved, malformed and duplicate keys),
  non-finite values, a late or repeated header; serial line settings and open failure against the
  fake VISA.
- **`tests/test_e2e_aux_4pp.py`** — a 4PP run under the simulator: aux columns present with plausible
  values and a clean fault column, placed before `compliance`, together with delta mode, and the plain
  schema when logging is off.
- **No hardware-marked sensor test exists.** `tests/hardware/` has nothing for the aux port, so the
  behaviours the simulator cannot reproduce — the reset on open, the partial first line, closing a
  port while a read is pending, reopening it straight away — are covered only by manual bench checks.

---

## 7. Staging

**Landed**
- The sensor module and its tests; `pyserial` declared.
- Simulator support: `FakeSerialSensor`, `FakeStreamSensor`, the `--sim-aux-address`, `--sim-temp`
  and `--sim-stream-address` flags.
- Co-logging wired into the run for every continuous mode (first planned for 4PP only, then
  generalised), with columns, metadata and the simulator e2e test.
- The background reader thread and the `aux_fault` provenance column.
- `StreamSensor`.
- The live readout label and the idle preview.

**Not built**
- An aux-vs-time or ρ-vs-aux plot. The readout label is the only live view.
- A marker on plotted points taken while the sensor was faulted. The fault is in the `aux_fault`
  column, the readout label and a status warning, nowhere else.
- A column for the reading's age. `SensorReading.age_s` exists; the run does not call it, so a value
  repeated from a stalled stream is indistinguishable in the file until the 5 s staleness limit turns
  it into `read_error`.
- Settings for the serial line parameters. The drivers accept them; no caller passes them.
- A hardware-marked test for the aux port (§6).
- User documentation: "how to add your own sensor", with `DummyFlowSensor` / `ArduinoThermocouple`
  as worked examples, and the wire formats.
- Setuptools entry-point discovery (trigger: a real third-party driver).
- Coupling the simulated temperature into the simulated resistance.

---

## 8. Open questions & risks

**Open questions:**
- The meaning of thermocouple field 4 (`status`) — carried as a flag regardless.
- Whether ~2 Hz is enough. A run sampling faster than the sensor streams repeats the newest value in
  consecutive rows.
- `StreamSensor` never asks for the header. A device that prints it once at power-up and does not
  reset when the port opens would never be recognised. Either the wire contract requires the header
  to be resent periodically, or the driver needs a way to request it.
- `aux_address` and `aux_log_enabled` live in the shared profile, although a serial address is a
  property of the machine.

| Risk | Severity | Mitigation |
|---|---|---|
| Second-instrument read inside the run touches connect/loop/cleanup | Med | Guarded behind the flag; per-point read failures are caught and recorded, not raised; e2e test asserts the plain schema with logging off |
| Native-USB auto-reset → partial first line / brief gap on open | Med | Reader resyncs on the record tag; preview tolerates empty ticks; port kept open across tab switches (no re-reset) |
| Faulted/unplugged sensor logs a plausible number | Med | Flags returned + `.ok`; `aux_fault` column; nan/inf flagged; stale cache → `read_error`. Not mitigated: a stalled stream repeats its last value as clean for up to 5 s (§7) |
| Device-declared key collides with an existing column or breaks a file format | Med | Keys validated in `parse_stream_header`; reserved set computed from the exporter's columns |
| Closing a port while a read is pending may not abort the read on every VISA backend | Med | `close()` is bounded at 1 s and an outliving reader cannot touch the sensor; behaviour on a real backend is unverified (no hardware test) |
| Unconditional column addition would break existing CSVs | Med | Conditional splice (delta-mode precedent); columns only when logging is on |
| `pyserial` undeclared → ASRL fails on clean install | **Fixed** | Declared in `pyproject.toml` |
| `--simulate` can't reproduce real auto-reset timing | Low | None automated; manual bench check only |

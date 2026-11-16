"""Tests for the pluggable auxiliary-sensor layer (resistamet_gui.sensors).

These exercise the generic contract — not just the thermocouple. The
``DummyFlowSensor`` test is the load-bearing one: it proves a researcher can
plug in something that is neither a thermocouple nor serial, and have it flow
through the same registry / column machinery.

Serial drivers run a background reader thread that caches the newest parsed
line; ``read_latest()`` is a NON-BLOCKING cache read. Tests inject a fake
``dev`` and start the reader via ``_start_reader()`` (the seam ``open()``
uses after ``connect()``), then use ``wait_for_reading`` where they need to
block for the first line.
"""
import time

import pytest

from resistamet_gui.constants import AUX_STALE_AFTER_S
from resistamet_gui.sensors import (
    ArduinoThermocouple,
    AuxiliarySensor,
    SensorChannel,
    SensorError,
    SensorReading,
    SensorReadError,
    StreamSensor,
    available_sensors,
    aux_column_names,
    format_fault,
    make_sensor,
    parse_stream_data,
    parse_stream_header,
    parse_thermocouple_line,
    reading_to_columns,
    register_sensor,
)


# --- A fake VISA resource that yields canned lines -------------------------

class _FakeDev:
    """Canned-line device: yields each line once, then times out. The reader
    thread consumes the lines and caches the last valid one."""

    def __init__(self, lines):
        self._lines = list(lines)
        self.read_termination = "\n"
        self.write_termination = "\n"
        self.timeout = 0

    def read(self):
        if not self._lines:
            raise TimeoutError("no more lines")
        return self._lines.pop(0)

    @property
    def exhausted(self):
        return not self._lines

    def flush(self, *_a, **_k):
        pass

    def close(self):
        pass


def _start(sensor, lines):
    """Attach a canned device and start the reader thread (bypasses the VISA
    connect that open() would do)."""
    sensor.dev = _FakeDev(lines)
    sensor._start_reader()
    return sensor


def _arduino_with(lines, t=42.0):
    return _start(ArduinoThermocouple("ASRL6::INSTR", clock=lambda: t), lines)


def _wait(cond, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def _closer():
    sensors = []
    yield sensors.append
    for s in sensors:
        s.close()


# --- parse_thermocouple_line ----------------------------------------------

def test_parse_valid_line():
    r = parse_thermocouple_line("DATA,21.227,22.734,0,0")
    assert r.values == {"t_sample": 21.227, "t_coldjunction": 22.734}
    assert r.flags == {"t_sample": 0, "status": 0}
    assert r.ok is True


def test_parse_negative_temperature():
    r = parse_thermocouple_line("DATA,-5.0,20.5,0,0")
    assert r.values["t_sample"] == -5.0


def test_parse_fault_flag_marks_not_ok():
    r = parse_thermocouple_line("DATA,21.2,22.7,1,0")
    assert r.flags["t_sample"] == 1
    assert r.ok is False


@pytest.mark.parametrize("bad", [
    "",
    "21.258,22.734,0,0",       # partial first line: missing DATA prefix
    "DATA,21.2,22.7,0",         # too few fields
    "DATA,21.2,22.7,0,0,9",     # too many fields
    "DATA,nan,22.7,0,0",        # non-numeric
    "INFO,startup",             # other line type
    "DATA,21.2,22.7,0,0\rextra",
])
def test_parse_rejects_malformed(bad):
    assert parse_thermocouple_line(bad) is None


# --- Background reader: nonblocking read_latest, freshness, staleness ------

def test_reader_caches_and_read_latest_returns(_closer):
    s = _arduino_with(["DATA,21.227,22.734,0,0"], t=9.0)
    _closer(s)
    r = s.wait_for_reading(2.0)
    assert r.values["t_sample"] == 21.227
    assert r.timestamp == 9.0            # stamped when cached, not 0.0


def test_reader_caches_newest_line(_closer):
    s = _arduino_with(["DATA,21.0,22.0,0,0", "DATA,23.5,24.0,0,0"])
    _closer(s)
    assert _wait(lambda: s.dev.exhausted), "reader did not consume the stream"
    assert _wait(lambda: _latest_tip(s) == 23.5), "cache does not hold newest line"


def _latest_tip(s):
    try:
        return s.read_latest().values["t_sample"]
    except SensorReadError:
        return None


def test_read_latest_is_nonblocking_before_first_line(_closer):
    s = _arduino_with([])   # device only times out
    _closer(s)
    t0 = time.time()
    with pytest.raises(SensorReadError):
        s.read_latest()
    assert time.time() - t0 < 0.5, "read_latest blocked — must be a cache read"


def test_read_latest_raises_when_stale(_closer):
    s = _arduino_with(["DATA,21.0,22.0,0,0"])
    _closer(s)
    s.wait_for_reading(2.0)
    # Stop the reader so nothing re-caches, then age the cache artificially.
    s._stop_evt.set()
    real = time.monotonic
    s._monotonic = lambda: real() + AUX_STALE_AFTER_S + 1.0
    with pytest.raises(SensorReadError, match="stale"):
        s.read_latest()


def test_reader_resyncs_past_banner_lines(_closer):
    s = _arduino_with(["maxwelld foam-TC v1", "READY", "DATA,21.0,22.0,0,0"])
    _closer(s)
    r = s.wait_for_reading(2.0)
    assert r.values["t_sample"] == 21.0


def test_wait_for_reading_times_out(_closer):
    s = _arduino_with([])
    _closer(s)
    with pytest.raises(SensorReadError):
        s.wait_for_reading(0.15)


def test_close_stops_reader_thread(_closer):
    s = _arduino_with(["DATA,21.0,22.0,0,0"])
    s.wait_for_reading(2.0)
    reader = s._reader
    s.close()
    assert not reader.is_alive(), "reader thread survived close()"
    with pytest.raises(SensorReadError):
        s.read_latest()          # cache cleared on close


def test_arduino_declares_two_channels():
    s = ArduinoThermocouple("ASRL6::INSTR")
    keys = [c.key for c in s.channels()]
    assert keys == ["t_sample", "t_coldjunction"]
    assert all(c.unit == "°C" for c in s.channels())


def test_arduino_satisfies_protocol():
    assert isinstance(ArduinoThermocouple("ASRL6::INSTR"), AuxiliarySensor)


# --- Registry --------------------------------------------------------------

def test_make_sensor_known():
    s = make_sensor("arduino_thermocouple", "ASRL6::INSTR")
    assert isinstance(s, ArduinoThermocouple)


def test_make_sensor_unknown_raises():
    with pytest.raises(ValueError):
        make_sensor("does_not_exist", "ASRL6::INSTR")


def test_arduino_is_registered():
    assert "arduino_thermocouple" in available_sensors()


# --- Generic glue: aux_fault provenance schema ------------------------------

def test_aux_column_names_include_fault_column():
    s = ArduinoThermocouple("ASRL6::INSTR")
    assert aux_column_names(s) == ["aux_t_sample", "aux_t_coldjunction", "aux_fault"]


def test_reading_to_columns_clean():
    r = SensorReading(timestamp=1.0,
                      values={"t_sample": 21.0, "t_coldjunction": 22.0},
                      flags={"t_sample": 0, "status": 0})
    cols = reading_to_columns(r)
    assert cols == {"aux_t_sample": 21.0, "aux_t_coldjunction": 22.0,
                    "aux_fault": "0"}


def test_reading_to_columns_preserves_values_when_flagged():
    """The contract: a faulted reading is recorded AND marked — channel
    values must survive into the columns, with provenance in aux_fault."""
    r = SensorReading(timestamp=1.0,
                      values={"t_sample": 250.4, "t_coldjunction": 23.0},
                      flags={"t_sample": 1, "status": 2})
    cols = reading_to_columns(r)
    assert cols["aux_t_sample"] == 250.4          # NOT NaN'd
    assert cols["aux_t_coldjunction"] == 23.0
    assert cols["aux_fault"] == "t_sample=1;status=2"


def test_format_fault():
    assert format_fault({}) == "0"
    assert format_fault({"a": 0, "b": 0}) == "0"
    assert format_fault({"a": 3}) == "a=3"
    assert format_fault({"a": 0, "b": 7}) == "b=7"


# --- The point of the whole exercise: a totally different sensor -----------

class DummyFlowSensor:
    """Not a thermocouple, not serial, not VISA. Implements the contract in
    a dozen lines — exactly what a researcher would write for their own rig."""

    def __init__(self, address, sequence=(12.5, 12.7, 12.4)):
        self.address = address
        self._seq = list(sequence)
        self._i = 0
        self.opened = False

    def open(self):
        self.opened = True
        return self

    def channels(self):
        return [SensorChannel("flow", "Flow rate", "mL/min")]

    def read_latest(self):
        v = self._seq[self._i % len(self._seq)]
        self._i += 1
        return SensorReading(timestamp=0.0, values={"flow": v})

    def close(self):
        self.opened = False


def test_arbitrary_sensor_satisfies_protocol():
    assert isinstance(DummyFlowSensor("X"), AuxiliarySensor)


def test_arbitrary_sensor_registers_and_flows_through_generic_glue():
    register_sensor("dummy_flow", DummyFlowSensor)
    assert "dummy_flow" in available_sensors()

    sensor = make_sensor("dummy_flow", "COM9").open()
    assert sensor.opened

    # The exporter/UI would learn the columns purely from channels().
    assert aux_column_names(sensor) == ["aux_flow", "aux_fault"]

    reading = sensor.read_latest()
    assert reading_to_columns(reading) == {"aux_flow": 12.5, "aux_fault": "0"}


# --- FakeSerialSensor integration (the --simulate device) ------------------

def test_arduino_reads_fake_serial_sensor(_closer):
    """The simulator's FakeSerialSensor must parse cleanly through the real
    ArduinoThermocouple driver's reader thread."""
    from resistamet_gui._simulator import FakeSerialSensor

    s = ArduinoThermocouple("ASRL6::INSTR", clock=lambda: 1.0)
    s.dev = FakeSerialSensor(sim_temp_c=30.0)
    s._start_reader()
    _closer(s)
    r = s.wait_for_reading(2.0)
    assert abs(r.values["t_sample"] - 30.0) < 0.5
    assert abs(r.values["t_coldjunction"] - 31.5) < 0.5  # cold junction ~1.5 C above tip
    assert r.ok


# --- Exporter schema splice (aux columns via explicit params) ---------------

def test_get_column_config_aux_splice():
    from resistamet_gui.data_export import get_column_config

    baseline, _ = get_column_config("four_point")
    assert not any(c.startswith("aux_") for c in baseline)

    aux_cols = ["aux_t_sample", "aux_t_coldjunction", "aux_fault"]
    aux_units = ["°C", "°C", ""]
    cols, units = get_column_config("four_point", aux_columns=aux_cols,
                                    aux_units=aux_units)
    assert len(cols) == len(units)
    for c in aux_cols:
        assert cols.index(c) < cols.index("compliance")

    # With delta mode too, aux columns come AFTER the delta columns.
    cols2, _ = get_column_config("four_point", {"fpp_delta_mode": True},
                                 aux_columns=["aux_t_sample", "aux_fault"],
                                 aux_units=["°C", ""])
    assert cols2.index("R_r") < cols2.index("aux_t_sample") < cols2.index("compliance")


def test_get_column_config_aux_splice_is_mode_agnostic():
    """Aux columns splice into every continuous mode, not just 4PP."""
    from resistamet_gui.data_export import AUX_LOG_MODES, get_column_config

    assert AUX_LOG_MODES == ('resistance', 'source_v', 'source_i', 'four_point')

    for mode in AUX_LOG_MODES:
        base, _ = get_column_config(mode)
        assert not any(c.startswith("aux_") for c in base), mode
        cols, units = get_column_config(mode, aux_columns=["aux_flow", "aux_fault"],
                                        aux_units=["mL/min", ""])
        assert len(cols) == len(units), mode
        assert cols.index("aux_flow") < cols.index("compliance"), mode

    # sweep is excluded (atomic :READ?, no aux co-logging).
    sweep_cols, _ = get_column_config("sweep", aux_columns=["aux_flow"],
                                      aux_units=["mL/min"])
    assert not any(c.startswith("aux_") for c in sweep_cols)


def test_splice_before_tail_matches_header_anchor():
    """The row-side splice helper and the header-side 'compliance' anchor
    must agree: value lands under its own column for every mode."""
    from resistamet_gui.data_export import get_column_config, splice_before_tail

    aux_cols = ["aux_x", "aux_fault"]
    for mode, plain_row in [
        ("resistance", [0.1, 1.0, 0.01, 100.0, 0.5, "OK", ""]),
        ("source_v",   [0.1, 1.0, 0.01, 100.0, 1e-6, 0.5, "OK", ""]),
        ("source_i",   [0.1, 1.0, 0.01, 100.0, 1e-6, 0.5, "OK", ""]),
    ]:
        header, _ = get_column_config(mode, aux_columns=aux_cols,
                                      aux_units=["u", ""])
        row = splice_before_tail(plain_row, [42.0, "0"])
        assert len(row) == len(header), mode
        assert row[header.index("aux_x")] == 42.0, mode
        assert row[header.index("aux_fault")] == "0", mode
        assert row[header.index("compliance")] == "OK", mode


# --- StreamSensor: dynamic multi-channel discovery -------------------------

def test_parse_stream_header_valid():
    chans = parse_stream_header("HDR,pressure:psi,t_sample:degC,force:N")
    assert [c.key for c in chans] == ["pressure", "t_sample", "force"]
    assert [c.unit for c in chans] == ["psi", "degC", "N"]


@pytest.mark.parametrize("bad", ["", "DATA,1,2", "HDR,noun", "HDR,", "HDR,:psi"])
def test_parse_stream_header_rejects(bad):
    assert parse_stream_header(bad) is None


def test_parse_stream_header_rejects_duplicate_keys():
    """Duplicate keys would produce duplicate CSV columns (silent data loss)
    and abort the HDF5 exporter's compound dtype — reject at the source."""
    assert parse_stream_header("HDR,t:degC,t:degC") is None
    assert parse_stream_header("HDR,a:x,b:y,a:z") is None
    # Distinct keys must not false-positive.
    assert parse_stream_header("HDR,t1:degC,t2:degC") is not None


def test_parse_stream_data_positional():
    chans = [SensorChannel("a", "A", "x"), SensorChannel("b", "B", "y")]
    r = parse_stream_data("DATA,1.5,2.5", chans)
    assert r.values == {"a": 1.5, "b": 2.5}


@pytest.mark.parametrize("bad", ["DATA,1", "DATA,1,2,3", "HDR,a:b", "DATA,x,y"])
def test_parse_stream_data_rejects(bad):
    chans = [SensorChannel("a", "A", "x"), SensorChannel("b", "B", "y")]
    assert parse_stream_data(bad, chans) is None


def test_stream_sensor_discovers_channels_and_reads(_closer):
    s = StreamSensor("ASRL7::INSTR", clock=lambda: 5.0)
    _start(s, ["HDR,pressure:psi,t_sample:degC,force:N",
               "DATA,32.5,24.1,0.98"])
    _closer(s)
    s.wait_ready(2.0)
    assert [c.key for c in s.channels()] == ["pressure", "t_sample", "force"]
    r = s.read_latest()
    assert r.values == {"pressure": 32.5, "t_sample": 24.1, "force": 0.98}
    assert r.timestamp == 5.0


def test_stream_sensor_no_header_raises(_closer):
    s = StreamSensor("ASRL7::INSTR")
    _start(s, ["DATA,1,2", "garbage"])
    _closer(s)
    with pytest.raises(SensorError):
        s.wait_ready(0.3)


def test_stream_sensor_duplicate_header_never_ready(_closer):
    """A dup-key header is rejected by the parser, so the sensor never
    reports channels and wait_ready fails with a clear error."""
    s = StreamSensor("ASRL7::INSTR")
    _start(s, ["HDR,t:degC,t:degC", "DATA,1,2"])
    _closer(s)
    with pytest.raises(SensorError):
        s.wait_ready(0.3)


def test_stream_sensor_satisfies_protocol_and_registered():
    assert isinstance(StreamSensor("ASRL7::INSTR"), AuxiliarySensor)
    assert "stream_sensor" in available_sensors()


def test_stream_sensor_through_fake_under_sim():
    import pyvisa
    from resistamet_gui.simulator import enable_simulation
    orig = pyvisa.ResourceManager
    try:
        enable_simulation(stream_address="ASRL7::INSTR")
        s = make_sensor("stream_sensor", "ASRL7::INSTR").open()
        try:
            s.wait_ready(2.0)
            keys = [c.key for c in s.channels()]
            assert "pressure" in keys and "t_sample" in keys
            r = s.read_latest()
            assert set(r.values) == set(keys)
        finally:
            s.close()
    finally:
        pyvisa.ResourceManager = orig

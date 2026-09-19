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
import threading
import time

import pytest

from resistamet_gui.constants import AUX_STALE_AFTER_S
from resistamet_gui.sensors import (
    FLAG_NON_FINITE,
    ArduinoThermocouple,
    AuxiliarySensor,
    SensorChannel,
    SensorError,
    SensorHeaderError,
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
    reserved_channel_keys,
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


def test_reading_age_is_measured_against_its_timestamp():
    r = SensorReading(timestamp=100.0, values={"t": 21.0})
    assert r.age_s(100.0) == 0.0
    assert r.age_s(103.5) == 3.5
    assert r.age_s(99.0) == 0.0          # clock stepped back: not negative


def test_a_stalled_stream_shows_its_age_while_still_served(_closer):
    """The stream stops after one line. read_latest keeps returning that
    reading until AUX_STALE_AFTER_S, and age_s is what tells the caller the
    value is old."""
    now = [50.0]
    s = _start(ArduinoThermocouple("ASRL6::INSTR", clock=lambda: now[0]),
               ["DATA,21.0,22.0,0,0"])
    _closer(s)
    s.wait_for_reading(2.0)
    now[0] += AUX_STALE_AFTER_S - 1.0
    r = s.read_latest()                  # still inside the staleness limit
    assert r.ok, "the repeated value carries no fault of its own"
    assert r.age_s(now[0]) == AUX_STALE_AFTER_S - 1.0


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


class _BlockingDev:
    """A device whose read() blocks until a line is released or, when
    ``close_aborts_read`` is set, until the session is closed — the two
    behaviours a VISA backend can have."""

    def __init__(self, close_aborts_read):
        self._close_aborts_read = close_aborts_read
        self._wake = threading.Event()
        self.in_read = threading.Event()
        self.closed = False

    def read(self):
        self.in_read.set()
        self._wake.wait(10.0)
        if self.closed and self._close_aborts_read:
            raise OSError("session closed")
        return "DATA,21.0,22.0,0,0"

    def release(self):
        self._wake.set()

    def close(self):
        self.closed = True
        if self._close_aborts_read:
            self._wake.set()


def _blocked_in_read(close_aborts_read):
    s = ArduinoThermocouple("ASRL6::INSTR")
    dev = _BlockingDev(close_aborts_read)
    s.dev = dev
    s._start_reader()
    assert dev.in_read.wait(2.0), "reader never reached read()"
    return s, dev, s._reader


def test_close_ends_a_blocked_read_when_the_backend_aborts_it():
    s, dev, reader = _blocked_in_read(close_aborts_read=True)
    t0 = time.monotonic()
    s.close()
    assert time.monotonic() - t0 < 0.5, "close() waited on an aborted read"
    assert dev.closed
    assert not reader.is_alive()


def test_close_is_bounded_when_the_backend_does_not_abort_the_read():
    """Closing the session does not end the pending read. close() must still
    return — after its 1 s wait for the reader, not after the read's own
    timeout — and the reader thread may outlive it. That is the documented
    contract: the thread is a daemon and exits once the read returns."""
    s, dev, reader = _blocked_in_read(close_aborts_read=False)
    try:
        t0 = time.monotonic()
        s.close()
        took = time.monotonic() - t0
        assert 0.9 < took < 2.0, f"close() took {took:.2f}s"
        assert dev.closed
        assert reader.is_alive(), "the reader is expected to outlive close()"
        with pytest.raises(SensorReadError):
            s.read_latest()              # cache cleared even so
    finally:
        dev.release()
    reader.join(2.0)
    assert not reader.is_alive(), "reader did not exit once its read returned"


def test_a_reader_that_outlives_close_leaves_the_sensor_alone():
    """The line its read finally returns must not refill the cache of a
    closed sensor."""
    s, dev, reader = _blocked_in_read(close_aborts_read=False)
    s.close()
    dev.release()
    reader.join(2.0)
    assert not reader.is_alive()
    with pytest.raises(SensorReadError, match="No reading"):
        s.read_latest()


def test_reopening_does_not_revive_the_previous_reader():
    """The old reader is still inside its read when the sensor is reopened.
    It must exit when that read returns, not start reading the new device
    alongside the new reader."""
    s, old_dev, old_reader = _blocked_in_read(close_aborts_read=False)
    s.close()
    new_dev = _BlockingDev(close_aborts_read=True)
    s.dev = new_dev
    s._start_reader()
    try:
        assert new_dev.in_read.wait(2.0)
        old_dev.release()
        old_reader.join(2.0)
        assert not old_reader.is_alive(), "previous reader kept running"
        with pytest.raises(SensorReadError, match="No reading"):
            s.read_latest()              # its late line was discarded
    finally:
        old_dev.release()
        s.close()


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


@pytest.mark.parametrize("not_a_header", ["", "DATA,1,2", "READY", "hdr,a:x"])
def test_parse_stream_header_skips_lines_that_are_not_headers(not_a_header):
    assert parse_stream_header(not_a_header) is None


@pytest.mark.parametrize("bad, named", [
    ("HDR,noun", "noun"),          # no unit separator
    ("HDR,", ""),                  # empty header
    ("HDR,:psi", ""),              # empty key
    ("HDR,a/b:x", "a/b"),          # HDF5 path separator
    ("HDR,a#b:x", "a#b"),          # CSV comment marker
    ("HDR,flow rate:x", "flow rate"),
    ("HDR,1st:x", "1st"),          # leading digit
    ("HDR,t-sample:degC", "t-sample"),
    ("HDR,ok:x,tempé:degC", "tempé"),
])
def test_parse_stream_header_refuses_unusable_header_and_names_it(bad, named):
    with pytest.raises(SensorHeaderError) as exc:
        parse_stream_header(bad)
    assert repr(named) in str(exc.value)


def test_parse_stream_header_rejects_duplicate_keys():
    """Duplicate keys would produce duplicate CSV columns (silent data loss)
    and abort the HDF5 exporter's compound dtype — reject at the source."""
    with pytest.raises(SensorHeaderError, match="'t' is declared twice"):
        parse_stream_header("HDR,t:degC,t:degC")
    with pytest.raises(SensorHeaderError, match="'a' is declared twice"):
        parse_stream_header("HDR,a:x,b:y,a:z")
    # Distinct keys must not false-positive.
    assert parse_stream_header("HDR,t1:degC,t2:degC") is not None


def test_parse_stream_header_rejects_the_fault_key():
    """A device channel called ``fault`` would become a second ``aux_fault``
    column: the CSV header repeats it and the row dict keeps only the
    provenance value, so the channel's data is lost."""
    with pytest.raises(SensorHeaderError, match="'fault' is reserved"):
        parse_stream_header("HDR,fault:x,t:degC")
    # Only the exact reserved name; a key that merely contains it is fine,
    # and so is one whose unprefixed name matches a built-in column.
    chans = parse_stream_header("HDR,fault_code:x,compliance:x")
    assert [c.key for c in chans] == ["fault_code", "compliance"]


def test_reserved_keys_cover_every_column_a_run_can_already_have():
    """The reserved set is computed from the exporter's column tables. Pin
    the consequence: whatever keys pass the parser, the spliced header of
    every co-logging mode has no repeated column."""
    from resistamet_gui.data_export import AUX_LOG_MODES, get_column_config

    assert "fault" in reserved_channel_keys()
    builtin = set()
    for mode in AUX_LOG_MODES:
        for settings in (None, {"fpp_delta_mode": True}):
            builtin.update(get_column_config(mode, settings)[0])
    # Try to collide with every built-in column, prefixed or not.
    candidates = sorted(builtin | {c[len("aux_"):] for c in builtin
                                   if c.startswith("aux_")} | {"fault"})
    accepted = []
    for key in candidates:
        try:
            accepted += parse_stream_header(f"HDR,{key}:x")
        except SensorHeaderError:
            pass
    assert accepted, "every candidate was refused; the test proves nothing"

    class _Declares:
        def channels(self):
            return accepted

    aux = aux_column_names(_Declares())
    for mode in AUX_LOG_MODES:
        for settings in (None, {"fpp_delta_mode": True}):
            cols, _ = get_column_config(mode, settings, aux_columns=aux,
                                        aux_units=[""] * len(aux))
            assert len(cols) == len(set(cols)), (mode, settings, cols)


def test_parse_stream_data_positional():
    chans = [SensorChannel("a", "A", "x"), SensorChannel("b", "B", "y")]
    r = parse_stream_data("DATA,1.5,2.5", chans)
    assert r.values == {"a": 1.5, "b": 2.5}


def test_parse_stream_data_clean_row_carries_no_flags():
    chans = [SensorChannel("a", "A", "x"), SensorChannel("b", "B", "y")]
    r = parse_stream_data("DATA,1.5,2.5", chans)
    assert r.flags == {} and r.ok
    assert reading_to_columns(r)["aux_fault"] == "0"


@pytest.mark.parametrize("token", ["nan", "NaN", "-nan", "inf", "-inf",
                                   "Infinity", "1e999"])
def test_parse_stream_data_flags_non_finite_channel(token):
    """An open transducer prints nan/inf. The value is kept, its channel is
    flagged, and the good channel beside it is untouched."""
    import math

    chans = [SensorChannel("a", "A", "x"), SensorChannel("b", "B", "y")]
    r = parse_stream_data(f"DATA,{token},2.5", chans)
    assert r is not None, "the row must be recorded, not dropped"
    assert not math.isfinite(r.values["a"])
    assert r.values["b"] == 2.5
    assert r.flags == {"a": FLAG_NON_FINITE}
    assert r.ok is False
    cols = reading_to_columns(r)
    assert cols["aux_fault"] == "a=1"
    assert cols["aux_b"] == 2.5


def test_stream_sensor_delivers_flagged_non_finite_reading(_closer):
    s = StreamSensor("ASRL7::INSTR")
    _start(s, ["HDR,p:psi,t:degC", "DATA,nan,21.0"])
    _closer(s)
    s.wait_ready(2.0)
    r = s.read_latest()
    assert r.ok is False
    assert format_fault(r.flags) == "p=1"
    assert r.values["t"] == 21.0


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
    with pytest.raises(SensorError, match="'t' is declared twice"):
        s.wait_ready(0.3)


def test_stream_sensor_reports_the_refused_key(_closer):
    """The operator sees which key the device got wrong, not a bare timeout."""
    s = StreamSensor("ASRL7::INSTR")
    _start(s, ["HDR,fault:x,t:degC", "DATA,7.5,21.0"])
    _closer(s)
    with pytest.raises(SensorError, match="ASRL7::INSTR.*'fault' is reserved"):
        s.wait_ready(0.3)
    assert s.channels() == []


def test_stream_sensor_recovers_when_a_usable_header_follows(_closer):
    s = StreamSensor("ASRL7::INSTR")
    _start(s, ["HDR,fault:x", "HDR,t:degC", "DATA,21.0"])
    _closer(s)
    s.wait_ready(2.0)
    assert [c.key for c in s.channels()] == ["t"]
    assert s.read_latest().values == {"t": 21.0}


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


# --- Serial line settings ---------------------------------------------------

@pytest.fixture
def _sim_visa():
    """The package's fake VISA, with both simulated serial sensors on it."""
    import pyvisa
    from resistamet_gui.simulator import enable_simulation
    orig = pyvisa.ResourceManager
    enable_simulation(aux_address="ASRL6::INSTR", stream_address="ASRL7::INSTR")
    try:
        yield
    finally:
        pyvisa.ResourceManager = orig


@pytest.mark.parametrize("driver, address", [
    ("arduino_thermocouple", "ASRL6::INSTR"),
    ("stream_sensor", "ASRL7::INSTR"),
])
def test_serial_settings_reach_the_visa_session(_sim_visa, driver, address):
    from pyvisa.constants import Parity, StopBits

    s = make_sensor(driver, address, baud_rate=9600, data_bits=7,
                    parity="even", stop_bits=2, termination="\r").open()
    try:
        assert s.dev.baud_rate == 9600
        assert s.dev.data_bits == 7
        assert s.dev.parity is Parity.even
        assert s.dev.stop_bits is StopBits.two
        assert s.dev.read_termination == "\r"
        s.wait_ready(2.0)                # and the stream still reads
    finally:
        s.close()


def test_serial_settings_left_out_are_not_touched(_sim_visa):
    """Defaults unchanged: with no settings given, open() sets nothing beyond
    what VisaInstrument.connect() already does."""
    s = make_sensor("stream_sensor", "ASRL7::INSTR").open()
    try:
        for name in ("baud_rate", "data_bits", "parity", "stop_bits"):
            assert not hasattr(s.dev, name), name
        assert s.dev.read_termination == "\n"
    finally:
        s.close()

    s = make_sensor("stream_sensor", "ASRL7::INSTR", baud_rate=115200).open()
    try:
        assert s.dev.baud_rate == 115200
        assert not hasattr(s.dev, "parity")
    finally:
        s.close()


@pytest.mark.parametrize("opts, named", [
    ({"baud_rate": 0}, "baud_rate"),
    ({"baud_rate": "fast"}, "baud_rate"),
    ({"data_bits": 9}, "data_bits"),
    ({"parity": "sometimes"}, "parity"),
    ({"stop_bits": 3}, "stop_bits"),
    ({"termination": ""}, "termination"),
])
def test_bad_serial_setting_fails_before_any_port_is_opened(opts, named):
    with pytest.raises(ValueError, match=named):
        make_sensor("stream_sensor", "ASRL7::INSTR", **opts)


def test_stop_bits_and_parity_spellings():
    from pyvisa.constants import Parity, StopBits
    from resistamet_gui.sensors import serial_session_attributes

    assert serial_session_attributes() == {}
    assert serial_session_attributes(stop_bits=1)["stop_bits"] is StopBits.one
    assert (serial_session_attributes(stop_bits=1.5)["stop_bits"]
            is StopBits.one_and_a_half)
    assert serial_session_attributes(parity="None")["parity"] is Parity.none
    assert serial_session_attributes(parity=Parity.odd)["parity"] is Parity.odd


def test_port_is_released_when_a_setting_is_refused():
    """A backend that refuses a setting must not leave the port open."""
    from resistamet_gui._simulator import FakeSerialSensor

    class _Refuses(FakeSerialSensor):
        @property
        def baud_rate(self):
            return 9600

        @baud_rate.setter
        def baud_rate(self, _v):
            raise OSError("unsupported baud rate")

    dev = _Refuses()
    s = ArduinoThermocouple("ASRL6::INSTR", baud_rate=12345)
    s.connect = lambda: setattr(s, "dev", dev)
    with pytest.raises(OSError, match="unsupported baud rate"):
        s.open()
    assert dev._closed and s.dev is None
    assert s._reader is None, "no reader thread for a port that never opened"

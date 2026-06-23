"""Tests for the pluggable auxiliary-sensor layer (resistamet_gui.sensors).

These exercise the generic contract — not just the thermocouple. The
``DummyFlowSensor`` test is the load-bearing one: it proves a researcher can
plug in something that is neither a thermocouple nor serial, and have it flow
through the same registry / column machinery.
"""
import pytest

from resistamet_gui.sensors import (
    ArduinoThermocouple,
    AuxiliarySensor,
    SensorChannel,
    SensorReading,
    SensorReadError,
    SerialLineSensor,
    available_sensors,
    aux_column_names,
    make_sensor,
    parse_thermocouple_line,
    reading_to_columns,
    register_sensor,
)


# --- A fake VISA resource that yields canned lines -------------------------

class _FakeDev:
    def __init__(self, lines):
        self._lines = list(lines)
        self.flush_count = 0
        self.read_termination = "\n"
        self.write_termination = "\n"
        self.timeout = 0

    def read(self):
        if not self._lines:
            raise TimeoutError("no more lines")
        return self._lines.pop(0)

    def flush(self, *_a, **_k):
        self.flush_count += 1

    def close(self):
        pass


def _arduino_with(lines, t=42.0):
    s = ArduinoThermocouple("ASRL6::INSTR", clock=lambda: t)
    s.dev = _FakeDev(lines)
    return s


# --- parse_thermocouple_line ----------------------------------------------

def test_parse_valid_line():
    r = parse_thermocouple_line("DATA,21.227,22.734,0,0")
    assert r.values == {"t_sample": 21.227, "t_coldjunction": 22.734}
    assert r.flags == {"t_sample": 0, "status": 0}
    assert r.ok is True


def test_parse_strips_nothing_but_matches_clean():
    # read_latest strips \r\n; parser sees the clean line.
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


# --- SerialLineSensor.read_latest: freshness + resync ----------------------

def test_read_latest_drains_then_returns_fresh():
    s = _arduino_with(["DATA,21.227,22.734,0,0"], t=9.0)
    r = s.read_latest()
    assert r.values["t_sample"] == 21.227
    assert r.timestamp == 9.0            # stamped at read instant, not 0.0
    assert s.dev.flush_count == 1        # buffer drained for freshness


def test_read_latest_resyncs_past_partial_line():
    s = _arduino_with(["227,22.7", "DATA,21.0,22.0,0,0"])
    r = s.read_latest()
    assert r.values["t_sample"] == 21.0


def test_read_latest_raises_after_only_garbage():
    s = _arduino_with(["junk"] * ArduinoThermocouple.MAX_READ_ATTEMPTS)
    with pytest.raises(SensorReadError):
        s.read_latest()


def test_read_latest_raises_when_no_data():
    s = _arduino_with([])
    with pytest.raises(SensorReadError):
        s.read_latest()


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


# --- Generic glue ----------------------------------------------------------

def test_reading_to_columns_flattens_with_aux_prefix():
    r = SensorReading(timestamp=1.0,
                      values={"t_sample": 21.0, "t_coldjunction": 22.0},
                      flags={"t_sample": 0, "status": 0})
    cols = reading_to_columns(r)
    assert cols["aux_t_sample"] == 21.0
    assert cols["aux_t_coldjunction"] == 22.0
    assert cols["aux_t_sample_flag"] == 0


def test_aux_column_names_from_channels():
    s = ArduinoThermocouple("ASRL6::INSTR")
    assert aux_column_names(s) == ["aux_t_sample", "aux_t_coldjunction"]


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
    assert aux_column_names(sensor) == ["aux_flow"]

    reading = sensor.read_latest()
    assert reading_to_columns(reading) == {"aux_flow": 12.5}


# --- FakeSerialSensor integration (the --simulate device) ------------------

def test_arduino_reads_fake_serial_sensor():
    """The simulator's FakeSerialSensor must parse cleanly through the real
    ArduinoThermocouple driver (read_latest -> parse_line)."""
    from resistamet_gui._simulator import FakeSerialSensor

    s = ArduinoThermocouple("ASRL6::INSTR", clock=lambda: 1.0)
    s.dev = FakeSerialSensor(sim_temp_c=30.0)
    r = s.read_latest()
    assert abs(r.values["t_sample"] - 30.0) < 0.5
    assert abs(r.values["t_coldjunction"] - 31.5) < 0.5  # cold junction ~1.5 C above tip
    assert r.ok


# --- Exporter schema splice (aux columns) ----------------------------------

def test_get_column_config_aux_splice():
    from resistamet_gui.data_export import get_column_config

    baseline, _ = get_column_config("four_point")
    assert not any(c.startswith("aux_") for c in baseline)

    cols, units = get_column_config("four_point", {
        "fpp_log_temp": True,
        "_aux_columns": ["aux_t_sample", "aux_t_coldjunction"],
        "_aux_units": ["°C", "°C"],
    })
    assert len(cols) == len(units)
    assert cols.index("aux_t_sample") < cols.index("compliance")
    assert cols.index("aux_t_coldjunction") < cols.index("compliance")

    # With delta mode too, aux columns come AFTER the delta columns.
    cols2, _ = get_column_config("four_point", {
        "fpp_delta_mode": True,
        "fpp_log_temp": True,
        "_aux_columns": ["aux_t_sample"],
        "_aux_units": ["°C"],
    })
    assert cols2.index("R_r") < cols2.index("aux_t_sample") < cols2.index("compliance")

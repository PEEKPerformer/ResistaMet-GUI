"""Resistance mode, Auto range: a healthy high-resistance DUT is not in compliance.

Auto-ohms picks the test current and the voltage range from the ohms range
(2400 series user's manual, Table 4-1): a 2420 measures its 2 MOhm range at
10 uA and a 2400 its 20 MOhm range at 1 uA, both up to 20 V. The limit read
back once at configure time, with the output off, is the reset range's 2.1 V.
Comparing every sample against that number archived every DUT above about
208 kOhm on a 2420 as V_COMP, and stop_on_compliance ended those runs after
one sample.
"""
import threading
import time

import pytest

from resistamet_gui.session.configure import ResistanceState
from resistamet_gui.session.continuous_run import ContinuousRun
from resistamet_gui.session.control import RunControl
from resistamet_gui.session.emitter import EventEmitter, ListSink
from resistamet_gui.session.samples import parse_resistance

#: The status word the lab 2420 sent with each reading of a healthy 1.03 MOhm
#: DUT (tests/fixtures/scpi_traces/resistance_4wire_1ua_1mohm.json).
STAT_HEALTHY = 4236292
STAT_IN_COMPLIANCE = STAT_HEALTHY | (1 << 3)
SETTINGS = {'res_voltage_compliance': 5.0, 'res_offset_comp': False}


def _status(voltage, current, resistance, state, stat=STAT_HEALTHY):
    parts = [repr(voltage), repr(current), repr(resistance), str(stat)]
    _, status, kind = parse_resistance(
        parts, stat, bool(stat & (1 << 3)), SETTINGS, 1.0, '2420', state,
        EventEmitter(ListSink()), ','.join(parts))
    assert kind == 'Voltage'
    return status


AUTO = ResistanceState(voltage_compliance_v=2.1, auto_range=True)


class TestAutoRange:
    @pytest.mark.parametrize('voltage, current, resistance', [
        (10.298, 1e-5, 1.0298e6),    # the fixture's DUT on a 2420: 2 MOhm range, 10 uA
        (10.0, 1e-6, 10e6),          # a 2400: 20 MOhm range, 1 uA
        (2.09, 1e-4, 20.9e3),        # any model: top of the 20 kOhm range
        (20.5, 1e-6, 20.5e6),        # range headroom on the 20 V range
    ])
    def test_a_healthy_dut_above_the_configure_time_limit_is_ok(self, voltage, current,
                                                                 resistance):
        assert _status(voltage, current, resistance, AUTO) == 'OK'

    def test_the_status_bit_is_still_believed(self):
        assert _status(1.0, 1e-3, 1e3, AUTO, stat=STAT_IN_COMPLIANCE) == 'V_COMP'

    @pytest.mark.parametrize('voltage, resistance', [
        (9.9e37, 9.9e37), (20.0, 9.9e37), (9.91e37, 5e6), (-9.9e37, 1e6)])
    def test_an_overflow_reading_is_flagged(self, voltage, resistance):
        """Past the top ohms range the instrument is in compliance and says so
        with its overflow value, not with the status bit."""
        assert _status(voltage, 1e-6, resistance, AUTO) == 'V_COMP'

    def test_a_reading_that_is_not_a_number_is_not_compliance(self):
        assert _status(float('nan'), 1e-6, float('nan'), AUTO) == 'OK'


class TestManualRangeIsUnchanged:
    MANUAL = ResistanceState(voltage_compliance_v=0.5, auto_range=False)

    def test_the_bench_case(self):
        """0.4998 V under a 0.5 V limit, status bit clear (bench 2026-09-18)."""
        assert _status(0.4998, 10e-3, 49.98, self.MANUAL) == 'V_COMP'

    def test_under_the_limit(self):
        assert _status(0.3, 10e-3, 30.0, self.MANUAL) == 'OK'

    def test_a_state_that_does_not_say_is_treated_as_manual(self):
        assert _status(0.4998, 10e-3, 49.98, ResistanceState(voltage_compliance_v=0.5)) == 'V_COMP'


def _settings(tmp_path, **measurement):
    base = {
        "measurement": {
            "sampling_rate": 100.0, "nplc": 0.1, "settling_time": 0.0,
            "gpib_address": "GPIB0::24::INSTR", "stop_on_compliance": True,
            "auto_zero": "on", "filter_enabled": False,
            # What auto-ohms uses on a 2420's 2 MOhm range; the simulator does
            # not choose its own, so the test asks for it.
            "res_test_current": 1e-5, "res_voltage_compliance": 21.0,
            "res_measurement_type": "4-wire", "res_auto_range": True,
            "res_offset_comp": False, "res_cable_null": 0.0,
        },
        "display": {"enable_plot": False, "plot_update_interval": 100, "buffer_size": 100},
        "file": {"auto_save_interval": 60, "data_directory": str(tmp_path / "data")},
        "output": {"format": "csv", "compression": "never", "compression_threshold_mb": 5},
    }
    base["measurement"].update(measurement)
    return base


@pytest.fixture(autouse=True)
def _no_sleep_inhibitor(monkeypatch):
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)


@pytest.fixture
def reset_range_limit(monkeypatch):
    """The simulator answers :SENS:VOLT:PROT? as the bench 2420 did: 2.1 V.

    It does not model auto-ohms moving the limit with the range, so the
    limit it enforces stays the 21 V asked for -- which is what the real
    instrument has on the range this DUT lands on.
    """
    from resistamet_gui._simulator import FakeKeithley
    query = FakeKeithley.query

    def answer(self, cmd):
        if cmd.strip().upper() == ':SENS:VOLT:PROT?':
            self.command_log.append(('query', cmd))
            return '+2.100000E+00'
        return query(self, cmd)
    monkeypatch.setattr(FakeKeithley, 'query', answer)


def _run_for_samples(settings, count):
    control, sink = RunControl(), ListSink()
    run = ContinuousRun('resistance', 'megohm', 'alice', settings, control, EventEmitter(sink))
    thread = threading.Thread(target=run.execute, daemon=True)
    thread.start()
    deadline = time.time() + 10.0
    while time.time() < deadline and thread.is_alive() and len(sink.of_type('sample')) < count:
        time.sleep(0.01)
    run.stop_measurement()
    thread.join(10.0)
    assert not thread.is_alive()
    return sink


class TestAMegohmDutThroughTheRun:
    def test_every_row_is_ok_and_stop_on_compliance_does_not_fire(self, fake_rm, tmp_path,
                                                                   reset_range_limit):
        fake_rm._dut_r = 1.03e6
        sink = _run_for_samples(_settings(tmp_path), count=5)

        samples = sink.of_type('sample')
        assert len(samples) >= 5
        assert {s.payload['compliance'] for s in samples} == {'OK'}
        assert samples[0].payload['values']['voltage'] == pytest.approx(10.3)
        assert sink.of_type('compliance') == []
        assert sink.of_type('run_ended')[0].payload['reason'] == 'user_stop'

    def test_the_header_says_when_the_limit_was_read(self, fake_rm, tmp_path, reset_range_limit):
        fake_rm._dut_r = 1.03e6
        sink = _run_for_samples(_settings(tmp_path), count=1)
        with open(sink.of_type('run_ended')[0].payload['path']) as handle:
            comments = [line for line in handle if line.startswith('#')]
        effective = [line for line in comments if 'effective' in line]
        assert any('voltage_compliance_V_at_configure' in line and '2.1' in line
                   for line in effective), comments
        assert not any('voltage_compliance_V:' in line or 'voltage_compliance_V ' in line
                       for line in effective), effective

    def test_manual_range_still_names_the_limit_in_force(self, fake_rm, tmp_path):
        sink = _run_for_samples(_settings(tmp_path, res_auto_range=False, res_test_current=1e-3,
                                          res_voltage_compliance=2.0), count=1)
        with open(sink.of_type('run_ended')[0].payload['path']) as handle:
            effective = [line for line in handle if line.startswith('#') and 'effective' in line]
        assert any('voltage_compliance_V' in line and 'at_configure' not in line
                   and '2.0' in line for line in effective), effective

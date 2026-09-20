"""Resistance mode: what happens when the voltage limit cannot be read back.

The header's ``effective.*`` block is defined as what the instrument
reported. A read-back that failed used to be replaced, silently, by the
request, and the request was then written there as if reported; an overflow
reply (9.91e37) was taken as the limit, so nothing could ever be flagged.
"""
import threading
import time

import pytest
import pyvisa

from resistamet_gui.session.continuous_run import ContinuousRun
from resistamet_gui.session.control import RunControl
from resistamet_gui.session.emitter import EventEmitter, ListSink


def _settings(tmp_path, **measurement):
    base = {
        "measurement": {
            "sampling_rate": 100.0, "nplc": 0.1, "settling_time": 0.0,
            "gpib_address": "GPIB0::24::INSTR", "stop_on_compliance": False,
            "auto_zero": "on", "filter_enabled": False,
            # 100 Ohm at 10 mA wants 1 V; the 0.5 V limit pins it.
            "res_test_current": 10e-3, "res_voltage_compliance": 0.5,
            "res_measurement_type": "4-wire", "res_auto_range": False,
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
def limit_query(monkeypatch):
    """Decide what the instrument does with :SENS:VOLT:PROT?."""
    from resistamet_gui._simulator import FakeKeithley
    query = FakeKeithley.query
    behaviour = {}

    def answer(self, cmd):
        if cmd.strip().upper() == ':SENS:VOLT:PROT?' and behaviour:
            self.command_log.append(('query', cmd))
            if 'raises' in behaviour:
                raise behaviour['raises']
            return behaviour['reply']
        return query(self, cmd)
    monkeypatch.setattr(FakeKeithley, 'query', answer)
    return behaviour


def _run(settings, count=2):
    control, sink = RunControl(), ListSink()
    run = ContinuousRun('resistance', 'r100', 'alice', settings, control, EventEmitter(sink))
    thread = threading.Thread(target=run.execute, daemon=True)
    thread.start()
    deadline = time.time() + 10.0
    while time.time() < deadline and thread.is_alive() and len(sink.of_type('sample')) < count:
        time.sleep(0.01)
    run.stop_measurement()
    thread.join(10.0)
    assert not thread.is_alive()
    return sink


def _effective_lines(sink):
    with open(sink.of_type('run_ended')[0].payload['path']) as handle:
        return [line for line in handle if line.startswith('#') and 'effective' in line]


def _warnings(sink):
    return [e.payload['code'] for e in sink.of_type('log') if e.payload['level'] == 'warning']


TIMEOUT = pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)


class TestAReadBackThatFails:
    @pytest.mark.parametrize('behaviour', [
        {'raises': TIMEOUT}, {'reply': 'not a number'}, {'reply': ''}])
    def test_it_is_said_and_nothing_is_recorded_as_reported(self, fake_rm, tmp_path,
                                                             limit_query, behaviour):
        limit_query.update(behaviour)
        sink = _run(_settings(tmp_path))
        assert 'limit_readback_failed' in _warnings(sink)
        assert _effective_lines(sink) == []

    def test_manual_range_still_flags_against_the_request(self, fake_rm, tmp_path, limit_query):
        limit_query.update({'raises': TIMEOUT})
        sink = _run(_settings(tmp_path))
        assert {s.payload['compliance'] for s in sink.of_type('sample')} == {'V_COMP'}

    def test_an_error_that_is_not_the_instrument_s_is_not_swallowed(self, fake_rm, tmp_path,
                                                                     limit_query):
        limit_query.update({'raises': RuntimeError('a bug, not a timeout')})
        control, sink = RunControl(), ListSink()
        ContinuousRun('resistance', 'r100', 'alice', _settings(tmp_path), control,
                      EventEmitter(sink)).execute()
        assert sink.of_type('run_ended')[0].payload['reason'] == 'configure_failed'
        writes = [cmd for fake in fake_rm.opened for op, cmd in fake.command_log if op == 'write']
        assert ':OUTP ON' not in writes


class TestAReadBackThatCannotBeALimit:
    @pytest.mark.parametrize('reply', ['+9.910000E+37', '0', '-2.1', 'nan', '64'])
    def test_it_is_rejected(self, fake_rm, tmp_path, limit_query, reply):
        """The default simulated model is a 2420: 60 V, so 63 V at most."""
        limit_query.update({'reply': reply})
        sink = _run(_settings(tmp_path))
        assert 'limit_readback_rejected' in _warnings(sink)
        assert _effective_lines(sink) == []
        # The overflow value used to become the limit, and then nothing was flagged.
        assert {s.payload['compliance'] for s in sink.of_type('sample')} == {'V_COMP'}

    def test_the_ceiling_is_the_model_s(self, fake_rm, tmp_path, limit_query):
        from resistamet_gui._simulator import _idn_for_model
        fake_rm._idn = _idn_for_model('2400')       # 200 V: 64 V is a limit it can have
        limit_query.update({'reply': '64'})
        sink = _run(_settings(tmp_path))
        assert 'limit_readback_rejected' not in _warnings(sink)
        assert any('64' in line for line in _effective_lines(sink))

    def test_a_sane_reply_is_recorded_without_comment(self, fake_rm, tmp_path, limit_query):
        limit_query.update({'reply': '+5.000000E-01'})
        sink = _run(_settings(tmp_path))
        assert not {'limit_readback_rejected', 'limit_readback_failed'} & set(_warnings(sink))
        assert any('voltage_compliance_V' in line and '0.5' in line
                   for line in _effective_lines(sink))

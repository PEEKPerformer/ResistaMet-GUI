"""The four-point runtime power monitor.
"""
import copy
import csv
import time

import pytest

from resistamet_gui._simulator import FakeKeithley
from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.session import ListSink, MeasurementSession


@pytest.fixture(autouse=True)
def _no_sleep_inhibitor(monkeypatch):
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)


@pytest.fixture
def profile(tmp_path):
    settings = copy.deepcopy({k: DEFAULT_SETTINGS[k] for k in ('measurement', 'display', 'output')})
    settings['file'] = {'auto_save_interval': 60, 'data_directory': str(tmp_path / 'data')}
    settings['measurement'].update({
        'gpib_address': 'GPIB0::24::INSTR', 'sampling_rate': 100.0, 'settling_time': 0.0})
    return settings


def _run(profile, mode, until_samples=None):
    sink = ListSink()
    session = MeasurementSession(sink)
    try:
        session.start(profile, mode, 'wafer', 'alice')
        deadline = time.time() + 10.0
        while time.time() < deadline and session.state != 'idle':
            if until_samples and len(sink.of_type('sample')) >= until_samples:
                session.stop()
            time.sleep(0.01)
        assert session.state == 'idle'
    finally:
        session.close(timeout=5.0)
    return sink


def _writes(fake_rm):
    return [cmd for op, cmd in fake_rm.opened[-1].command_log if op == 'write']


def _rows(path):
    with open(path) as handle:
        rows = [r for r in csv.reader(handle) if r and not r[0].startswith('#')]
    return rows[0], rows[1:]


class TestFourPointPowerMonitor:
    def _four_point(self, profile, **settings):
        profile['measurement'].update({
            'fpp_current': 1e-2, 'fpp_voltage_compliance': 5.0, 'fpp_samples': 4,
            'fpp_power_warn_w': 1e-4, 'fpp_power_stop_w': 1.0})
        profile['measurement'].update(settings)
        return _run(profile, 'four_point')

    def test_the_power_warning_is_given_once_not_on_every_row(self, fake_rm, profile):
        sink = self._four_point(profile)
        assert len(sink.of_type('sample')) == 4
        runtime = [e for e in sink.of_type('log')
                   if e.payload.get('code') == 'power_envelope'
                   and 'above warn threshold' in e.payload.get('message', '')]
        assert len(runtime) == 1

    def test_an_overflow_reading_is_not_a_power(self, fake_rm, profile, monkeypatch):
        real = FakeKeithley._format_elements

        def overflow(self, v, i, r, stat):
            return real(self, 9.9e37, i, r, stat)
        monkeypatch.setattr(FakeKeithley, '_format_elements', overflow)
        sink = self._four_point(profile, fpp_power_warn_w=0.5)
        assert sink.of_type('overpower_trip') == []
        assert sink.of_type('run_ended')[-1].payload['reason'] != 'overpower'
        assert {e.payload['compliance'] for e in sink.of_type('sample')} == {'V_COMP'}

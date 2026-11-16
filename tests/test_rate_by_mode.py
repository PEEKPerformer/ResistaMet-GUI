"""The reading-rate estimate, for the mode that runs.
"""
import copy
import csv
import time

import pytest

from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.session import ListSink, MeasurementSession
from resistamet_gui.timing import TimingSettings


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


class TestRateEstimateByMode:
    SETTINGS = {'nplc': 1.0, 'auto_zero': 'on', 'filter_enabled': True, 'filter_type': 'repeat',
                'filter_count': 10, 'res_offset_comp': True}

    def test_offset_compensation_counts_for_resistance_only(self):
        resistance = TimingSettings.from_dict(self.SETTINGS, 'resistance').max_rate_hz()
        four_point = TimingSettings.from_dict(self.SETTINGS, 'four_point').max_rate_hz()
        assert four_point == pytest.approx(2 * resistance)
        assert TimingSettings.from_dict(self.SETTINGS).max_rate_hz() == resistance

    def test_the_preview_promises_a_four_point_run_the_full_rate(self):
        from resistamet_gui.schema.resolve import resolve_run_settings
        profile = copy.deepcopy(DEFAULT_SETTINGS)
        profile['measurement']['res_offset_comp'] = True
        resolved = resolve_run_settings(profile, 'four_point', {}, strict=False)
        expected = TimingSettings.from_dict(resolved.settings['measurement'],
                                            'four_point').max_rate_hz()
        assert resolved.derived['max_rate_hz'] == pytest.approx(expected)

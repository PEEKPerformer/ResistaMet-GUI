"""A source current in a file name.
"""
import copy
import csv
import time

import pytest

from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.session import ListSink, MeasurementSession
from resistamet_gui.session.configure import current_label


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


class TestCurrentInAFileName:
    @pytest.mark.parametrize("amps, label", [
        (1e-3, '1.00mA'), (1e-4, '0.10mA'), (1e-5, '0.01mA'), (1e-6, '1.00uA'),
        (2.5e-6, '2.50uA'), (-1e-6, '-1.00uA'), (0.0, '0.00mA'), (1.0, '1000.00mA')])
    def test_label(self, amps, label):
        assert current_label(amps) == label

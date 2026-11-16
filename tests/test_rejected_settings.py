"""A setting the instrument refuses ends the run before it starts.

From a 2420 on the bench with its output off (2026-11-16): asked for 100 V,
it queued -222 for the range and for the level, and left the level at 0 V. A
run went on from there under a header that recorded 100 V. Every configure
sequence a run sends was replayed on that instrument and left the error queue
empty, so anything in it after configuration is a setting that did not take.
"""
import copy
import csv
import time

import pytest

from resistamet_gui._simulator import FakeKeithley
from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.session import ListSink, MeasurementSession
from resistamet_gui.session.configure import rejected_settings


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


class TestARefusedSettingStopsTheRun:
    @pytest.fixture
    def refuses_the_voltage(self, monkeypatch):
        """As a 2420 asked for 100 V: both commands refused, the level left at 0 V."""
        real = FakeKeithley._dispatch_write

        def dispatch(self, cmd):
            if cmd.upper().startswith(':SOUR:VOLT') and cmd.rstrip().endswith(' 10.0'):
                self._queue_error(-222, "Parameter data out of range")
                return None
            return real(self, cmd)
        monkeypatch.setattr(FakeKeithley, '_dispatch_write', dispatch)

    def test_the_run_does_not_start(self, fake_rm, profile, refuses_the_voltage):
        profile['measurement'].update({
            'vsource_voltage': 10.0, 'vsource_current_compliance': 0.01})
        sink = _run(profile, 'source_v')
        ended = sink.of_type('run_ended')[-1].payload
        assert ended['reason'] == 'configure_failed' and ended['path'] is None
        assert ':OUTP ON' not in _writes(fake_rm)
        messages = [e.payload['message'] for e in sink.of_type('error')]
        assert any('refused a setting' in m and '-222' in m for m in messages), messages
        assert sink.of_type('sample') == []

    def test_an_empty_queue_is_no_refusal(self, fake_instrument):
        assert rejected_settings(fake_instrument) == []

    def test_every_queued_error_is_reported_and_the_queue_left_empty(self, fake_instrument):
        fake_instrument.write(':TRIG:COUN 9999')
        fake_instrument.write(':NOT:A:COMMAND 1')
        found = rejected_settings(fake_instrument)
        assert len(found) == 2 and found[0].startswith('-222')
        assert rejected_settings(fake_instrument) == []

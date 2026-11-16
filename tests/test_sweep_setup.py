"""A sweep's step and delay, as the instrument takes them.

From a 2420 on the bench with its output off (2026-11-16). A positive step
for a downward sweep is refused (-222) and the instrument is left with the
step of its reset point count, 2500: a 0.5 V to 0 V sweep in five points
would have stepped by 0.2 mV. Auto delay, sent after the sweep's step delay,
replaced it with 1 ms. A trigger count above 2500 is refused.
"""
import copy
import csv
import time

import pytest

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


class TestTheSimulatorRefusesWhatTheInstrumentDoes:
    def _errors(self, dev):
        found = []
        while True:
            reply = dev.query(':SYST:ERR?')
            if reply.startswith('0,'):
                return found
            found.append(reply)

    def test_a_step_away_from_the_stop_value(self, fake_instrument):
        dev = fake_instrument
        for cmd in (':SOUR:FUNC VOLT', ':SOUR:VOLT:START 0.5', ':SOUR:VOLT:STOP 0.0',
                    ':SOUR:VOLT:STEP 0.125'):
            dev.write(cmd)
        assert any(e.startswith('-222') for e in self._errors(dev))
        dev.write(':SOUR:VOLT:STEP -0.125')
        assert self._errors(dev) == []
        assert dev.state['sour_volt_step'] == -0.125

    def test_the_step_follows_a_swap_of_start_and_stop(self, fake_instrument):
        dev = fake_instrument
        for cmd in (':SOUR:VOLT:START 0', ':SOUR:VOLT:STOP 0.5', ':SOUR:VOLT:STEP 0.125',
                    ':SOUR:VOLT:START 0.5', ':SOUR:VOLT:STOP 0'):
            dev.write(cmd)
        assert self._errors(dev) == []
        assert dev.state['sour_volt_step'] == -0.125

    def test_a_trigger_count_past_2500(self, fake_instrument):
        dev = fake_instrument
        dev.write(':TRIG:COUN 2500')
        dev.write(':TRIG:COUN 2501')
        assert any(e.startswith('-222') for e in self._errors(dev))
        assert dev.query(':TRIG:COUN?').strip() == '2500'

    def test_auto_delay_replaces_a_set_delay(self, fake_instrument):
        dev = fake_instrument
        dev.write(':SOUR:DEL 0.5')
        assert (dev.state['sour_del'], dev.state['sour_del_auto']) == (0.5, False)
        dev.write(':SOUR:DEL:AUTO ON')
        assert (dev.state['sour_del'], dev.state['sour_del_auto']) == (0.001, True)


class TestSweep:
    def _sweep(self, profile, **settings):
        profile['measurement'].update({
            'sweep_source': 'voltage', 'sweep_start': 0.0, 'sweep_stop': 0.5,
            'sweep_step': 0.125, 'sweep_compliance': 0.1, 'sweep_delay': 0.25,
            'sweep_direction': 'up'})
        profile['measurement'].update(settings)
        return _run(profile, 'sweep')

    def test_a_downward_sweep_sends_a_negative_step_and_runs_downwards(self, fake_rm, profile):
        sink = self._sweep(profile, sweep_direction='down')
        assert ':SOUR:VOLT:STEP -0.125' in _writes(fake_rm)
        ended = sink.of_type('run_ended')[-1].payload
        assert ended['reason'] == 'completed', sink.of_type('error')
        _, rows = _rows(ended['path'])
        assert [float(r[1]) for r in rows] == pytest.approx([0.5, 0.375, 0.25, 0.125, 0.0])

    def test_an_upward_sweep_is_as_it_was(self, fake_rm, profile):
        sink = self._sweep(profile)
        assert ':SOUR:VOLT:STEP 0.125' in _writes(fake_rm)
        _, rows = _rows(sink.of_type('run_ended')[-1].payload['path'])
        assert [float(r[1]) for r in rows] == pytest.approx([0.0, 0.125, 0.25, 0.375, 0.5])

    def test_both_legs_of_an_up_then_down_sweep(self, fake_rm, profile):
        sink = self._sweep(profile, sweep_direction='up_down')
        ended = sink.of_type('run_ended')[-1].payload
        assert ended['reason'] == 'completed', sink.of_type('error')
        _, rows = _rows(ended['path'])
        assert [float(r[1]) for r in rows] == pytest.approx(
            [0.0, 0.125, 0.25, 0.375, 0.5, 0.5, 0.375, 0.25, 0.125, 0.0])

    def test_the_step_delay_is_the_one_the_header_records(self, fake_rm, profile):
        sink = self._sweep(profile)
        writes = _writes(fake_rm)
        assert ':SOUR:DEL 0.25' in writes
        assert ':SOUR:DEL:AUTO ON' not in writes[writes.index(':SOUR:DEL 0.25'):]
        from resistamet_gui.data_export import parse_metadata
        header = parse_metadata(sink.of_type('run_ended')[-1].payload['path'])
        assert header['params.delay_s'] == 0.25

    def test_the_other_modes_still_use_auto_delay(self, fake_rm, profile):
        profile['measurement'].update({'isource_current': 1e-3, 'isource_voltage_compliance': 5.0})
        _run(profile, 'source_i', until_samples=1)
        assert ':SOUR:DEL:AUTO ON' in _writes(fake_rm)

"""The headless session: state, single-run rule, commands, teardown.

No Qt anywhere — this is the path the API sidecar and the MCP layer will use.
"""
import copy
import os
import time

import pytest

from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.session.emitter import ListSink
from resistamet_gui.session.manager import MeasurementSession, SessionBusy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def profile(tmp_path):
    settings = copy.deepcopy({
        'measurement': DEFAULT_SETTINGS['measurement'],
        'display': DEFAULT_SETTINGS['display'],
        'file': DEFAULT_SETTINGS['file'],
        'output': DEFAULT_SETTINGS['output'],
    })
    settings['file']['data_directory'] = str(tmp_path / 'data')
    settings['measurement']['sampling_rate'] = 50.0
    settings['measurement']['settling_time'] = 0.0
    return settings


@pytest.fixture
def sink():
    return ListSink()


@pytest.fixture
def session(sink):
    made = MeasurementSession(sink)
    yield made
    made.close(timeout=5.0)


@pytest.fixture(autouse=True)
def _no_sleep_inhibitor(monkeypatch):
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _four_point(profile, samples=2):
    profile['measurement'].update({
        'fpp_current': 1e-4, 'fpp_voltage_compliance': 5.0, 'fpp_samples': samples,
        'fpp_power_warn_w': 1.0, 'fpp_power_stop_w': 2.0,
    })
    return profile


class TestStateMachine:
    def test_starts_idle(self, session):
        assert session.state == 'idle'
        assert session.status()['run_id'] is None

    def test_run_reports_its_id_and_ends_idle(self, session, sink, fake_rm, profile):
        run_id = session.start(_four_point(profile), 'four_point', 'wafer1', 'alice')
        assert run_id == 'run-1'
        assert _wait_for(lambda: session.state == 'idle')
        assert [e.run_id for e in sink.events][0] == 'run-1'

    def test_one_run_at_a_time(self, session, fake_rm, profile):
        session.start(_four_point(profile, samples=50), 'four_point', 'wafer1', 'alice')
        with pytest.raises(SessionBusy):
            session.start(profile, 'four_point', 'wafer2', 'alice')
        session.stop()

    def test_identify_is_refused_during_a_run(self, session, fake_rm, profile):
        session.start(_four_point(profile, samples=50), 'four_point', 'wafer1', 'alice')
        with pytest.raises(SessionBusy):
            session.identify('GPIB0::24::INSTR')
        session.stop()

    def test_identify_returns_the_model(self, session, fake_rm):
        info = session.identify('GPIB0::24::INSTR')
        assert 'KEITHLEY' in info['idn'].upper()
        assert info['model']


class TestCommands:
    def test_stop_ends_the_run(self, session, sink, fake_rm, profile):
        session.start(_four_point(profile, samples=0), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('sample'))
        session.stop()
        assert _wait_for(lambda: session.state == 'idle')
        assert [e.payload['reason'] for e in sink.of_type('run_ended')] == ['user_stop']

    def test_pause_and_resume_show_in_the_state(self, session, sink, fake_rm, profile):
        session.start(_four_point(profile, samples=0), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('sample'))
        session.pause()
        assert _wait_for(lambda: session.state == 'paused')
        session.resume()
        assert _wait_for(lambda: session.state == 'running')
        session.stop()

    def test_mark_event_reaches_a_sample(self, session, sink, fake_rm, profile):
        session.start(_four_point(profile, samples=0), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('sample'))
        session.mark_event('PROBE_MOVED')
        assert _wait_for(lambda: any(e.payload['event_marker'] == 'PROBE_MOVED'
                                      for e in sink.of_type('sample')))
        session.stop()

    def test_commands_without_a_run_are_refused(self, session):
        with pytest.raises(SessionBusy):
            session.mark_event('X')
        with pytest.raises(SessionBusy):
            session.answer_prompt('vdp_geometry-1', 'proceed')

    def test_stop_without_a_run_is_harmless(self, session):
        session.stop()
        assert session.state == 'idle'


class TestValidation:
    def test_strict_resolver_rejects_a_bad_request(self, session, profile):
        with pytest.raises(ValueError) as excinfo:
            session.start(profile, 'vdp', 'wafer1', 'alice',
                           overrides={'vdp_thickness_cm': 0.0})
        assert 'vdp_thickness_cm' in str(excinfo.value)

    def test_rejected_request_leaves_the_session_idle(self, session, profile):
        with pytest.raises(ValueError):
            session.start(profile, 'resistance', 'wafer1', 'alice',
                           overrides={'not_a_setting': 1})
        assert session.state == 'idle'


class TestStatus:
    def test_status_tracks_the_run(self, session, sink, fake_rm, profile):
        session.start(_four_point(profile, samples=0), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('sample'))
        status = session.status()
        assert status['mode'] == 'four_point'
        assert status['path'].endswith('.csv')
        assert status['last_seq'] > 0
        session.stop()

    def test_close_joins_the_thread(self, sink, fake_rm, profile):
        session = MeasurementSession(sink)
        session.start(_four_point(profile, samples=0), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('sample'))
        session.close(timeout=5.0)
        assert session.state == 'idle'

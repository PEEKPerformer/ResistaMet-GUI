"""An agent's run, as a person at the bench sees it afterwards and during.

``docs/design/mcp_layer.md`` M5: a hazardous run an agent started asks the
touch-safety question even on a profile a person silenced, because the
silence was given for the runs that person starts.
"""
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from resistamet_gui.api import create_app
from resistamet_gui.api.app import AGENT_ROLE
from resistamet_gui.config import ConfigManager
from resistamet_gui.session.emitter import ListSink
from resistamet_gui.session.manager import MeasurementSession

UI_TOKEN = 'ui-token'
AGENT_TOKEN = 'agent-token'


@pytest.fixture(autouse=True)
def _no_sleep_inhibitor(monkeypatch):
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)


@pytest.fixture
def config(tmp_path):
    manager = ConfigManager(config_file=str(tmp_path / 'config.json'))
    manager.add_user('alice')
    manager.update_user_settings('alice', {
        'file': {'data_directory': str(tmp_path / 'data'), 'auto_save_interval': 60},
        'measurement': {'sampling_rate': 50.0, 'settling_time': 0.0,
                         'safety_voltage_warn_v': 30.0,
                         'safety_voltage_warn_silenced': False,
                         'vsource_voltage': 60.0, 'vsource_current_compliance': 0.1,
                         'vsource_duration_hours': 0.0},
        # Above the 30 V default, so an agent's 60 V run reaches its prompt.
        'agent_limits': {'max_voltage_v': 100.0},
    })
    return manager


@pytest.fixture
def sink():
    return ListSink()


@pytest.fixture
def session(sink):
    made = MeasurementSession(sink)
    yield made
    made.close(timeout=5.0)


@pytest.fixture
def app(session, config):
    made = create_app(session, token=UI_TOKEN, config=config)
    made.state.api.add_token(AGENT_TOKEN, AGENT_ROLE)
    return made


@pytest.fixture
def ui(app):
    with TestClient(app) as client:
        client.headers.update({'Authorization': f'Bearer {UI_TOKEN}'})
        yield client


@pytest.fixture
def agent(app):
    with TestClient(app) as client:
        client.headers.update({'Authorization': f'Bearer {AGENT_TOKEN}'})
        yield client


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _start(client, overrides=None):
    return client.post('/session/start', json={
        'mode': 'source_v', 'username': 'alice', 'sample_name': 'wafer1',
        'overrides': overrides or {}})


def _pending(client):
    return client.get('/session').json()['pending_prompt']


def _end(ui):
    ui.post('/session/abort')
    assert _wait_for(lambda: ui.get('/session').json()['state'] == 'idle')


def _silence(ui):
    """What a person does to stop being asked: only the window may."""
    response = ui.patch('/profiles/alice',
                        json={'measurement': {'safety_voltage_warn_silenced': True}})
    assert response.status_code == 200


def _samples_without_a_prompt(sink):
    assert _wait_for(lambda: sink.of_type('sample'))
    assert sink.of_type('prompt') == []


class TestAnAgentsHazardousRunAlwaysAsks:
    def test_on_a_silenced_profile_it_waits_for_a_person(self, agent, ui, fake_rm, sink):
        _silence(ui)
        assert _start(agent).status_code == 202
        assert _wait_for(lambda: _pending(agent) is not None)

        prompt = _pending(agent)
        assert prompt['kind'] == 'safety_voltage_ack'
        assert prompt['requires_human'] is True
        assert prompt['detail']['voltage_v'] == 60.0
        assert fake_rm.opened == []
        _end(ui)

    def test_the_window_is_still_not_asked(self, ui, fake_rm, sink):
        _silence(ui)
        assert _start(ui).status_code == 202
        _samples_without_a_prompt(sink)
        _end(ui)

    def test_below_the_threshold_nobody_is_asked(self, agent, ui, fake_rm, sink):
        _silence(ui)
        assert _start(agent, {'vsource_voltage': 5.0}).status_code == 202
        _samples_without_a_prompt(sink)
        _end(ui)

    def test_a_person_silencing_it_at_an_agents_prompt_does_not_silence_the_next(
            self, agent, ui, fake_rm, sink):
        """The person's answer is theirs to give; the next agent run asks again.

        The answer's ``silence_for_profile`` is logged, as on any run; the
        profile's flag is what a person sets in the window. Neither reaches an
        agent's run.
        """
        assert _start(agent).status_code == 202
        assert _wait_for(lambda: _pending(agent) is not None)
        answered = ui.post('/session/prompt', json={
            'prompt_id': _pending(ui)['prompt_id'], 'choice': 'acknowledge',
            'fields': {'silence_for_profile': True}})
        assert answered.status_code == 200
        assert _wait_for(lambda: sink.of_type('sample'))
        assert 'safety_silenced' in [e.payload['code'] for e in sink.of_type('log')]
        _end(ui)
        _silence(ui)

        assert _start(agent).status_code == 202
        assert _wait_for(lambda: _pending(agent) is not None)
        assert _pending(agent)['kind'] == 'safety_voltage_ack'
        assert len(sink.of_type('prompt')) == 2
        _end(ui)

        # The person's own runs are silenced, as they asked.
        run_id = _start(ui).json()['run_id']
        assert _wait_for(lambda: any(e.run_id == run_id for e in sink.of_type('sample')))
        assert len(sink.of_type('prompt')) == 2
        _end(ui)

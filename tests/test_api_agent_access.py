"""The agent role: a second token, and everywhere a person is required.

The MCP design (``docs/design/mcp_layer.md`` M2) gives an AI agent a token
of its own, role ``agent``. Every role check in the API asks for ``ui``, so
these tests do not look at a check's code: they present an agent token to
each one and expect it refused, while the same token reads like any client.
"""
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from resistamet_gui.api import create_app
from resistamet_gui.api.app import AGENT_ROLE, UI_ROLE
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
                         'vsource_voltage': 60.0, 'vsource_duration_hours': 0.0},
    })
    return manager


@pytest.fixture
def session():
    made = MeasurementSession(ListSink())
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


class TestTokenRoles:
    def test_the_given_token_keeps_the_given_role(self, session):
        app = create_app(session, token='t', role='mcp', profile_provider=lambda u: {})
        assert app.state.api.role_for('t') == 'mcp'

    def test_each_token_maps_to_its_own_role(self, app):
        assert app.state.api.role_for(UI_TOKEN) == UI_ROLE
        assert app.state.api.role_for(AGENT_TOKEN) == AGENT_ROLE

    @pytest.mark.parametrize('wrong', ['', 'x', UI_TOKEN + 'x', AGENT_TOKEN[:-1], 'tést'])
    def test_anything_else_has_no_role(self, app, wrong):
        assert app.state.api.role_for(wrong) is None

    def test_the_ui_token_works_as_before(self, ui):
        assert ui.get('/session').json()['state'] == 'idle'
        assert ui.patch('/profiles/alice',
                        json={'measurement': {'safety_voltage_warn_v': 40.0}}).status_code == 200

    def test_the_agent_token_reads(self, agent):
        assert agent.get('/session').status_code == 200
        assert agent.get('/profiles/alice').status_code == 200

    def test_an_unknown_token_is_unauthorized(self, app):
        with TestClient(app) as client:
            response = client.get('/session', headers={'Authorization': 'Bearer nope'})
        assert response.status_code == 401

    def test_a_removed_token_is_unauthorized_at_once(self, app, agent):
        assert agent.get('/session').status_code == 200
        app.state.api.remove_token(AGENT_TOKEN)
        assert agent.get('/session').status_code == 401

    def test_the_websocket_takes_either_token_and_nothing_else(self, app):
        from starlette.websockets import WebSocketDisconnect

        with TestClient(app) as client:
            for token in (UI_TOKEN, AGENT_TOKEN):
                with client.websocket_connect(f'/session/events/ws?token={token}'):
                    pass
            app.state.api.remove_token(AGENT_TOKEN)
            for token in (AGENT_TOKEN, 'nope'):
                with pytest.raises(WebSocketDisconnect) as closed:
                    with client.websocket_connect(f'/session/events/ws?token={token}'):
                        pass
                assert closed.value.code == 4401


class TestTheAgentIsRefusedWhereAPersonIsRequired:
    def test_answering_a_human_prompt(self, agent, ui, fake_rm):
        started = agent.post('/session/start', json={
            'mode': 'source_v', 'sample_name': 'wafer1', 'username': 'alice'})
        assert started.status_code == 202
        assert _wait_for(lambda: agent.get('/session').json()['pending_prompt'])
        prompt = agent.get('/session').json()['pending_prompt']
        assert prompt['requires_human'] is True

        response = agent.post('/session/prompt', json={'prompt_id': prompt['prompt_id'],
                                                         'choice': 'acknowledge'})

        assert response.status_code == 403
        # Still waiting for the person; nothing was energised.
        assert agent.get('/session').json()['pending_prompt']['prompt_id'] == prompt['prompt_id']
        assert fake_rm.opened == []
        ui.post('/session/abort')
        assert _wait_for(lambda: ui.get('/session').json()['state'] == 'idle')

    @pytest.mark.parametrize('patch', [{'safety_voltage_warn_silenced': True},
                                        {'safety_voltage_warn_v': 150.0}])
    def test_changing_the_touch_safety_keys(self, agent, config, patch):
        response = agent.patch('/profiles/alice', json={'measurement': patch})
        assert response.status_code == 403
        stored = config.get_user_settings('alice')['measurement']
        assert stored['safety_voltage_warn_v'] == 30.0
        assert stored['safety_voltage_warn_silenced'] is False

    def test_setting_a_visa_library_path(self, agent, config, tmp_path):
        library = tmp_path / 'libvisa.so'
        library.touch()
        response = agent.patch('/profiles/alice',
                                json={'measurement': {'visa_library': str(library)}})
        assert response.status_code == 403
        assert config.get_visa_library() == ''

"""Agent limits: the envelope a run an AI agent starts must stay inside.

``docs/design/mcp_layer.md`` M4. The limits are a section of the profile,
``agent_limits``, that only the user interface may change; they bound the
``agent`` role and never the window.
"""
import json

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
        'measurement': {'sampling_rate': 50.0, 'settling_time': 0.0},
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


class TestTheProfileSection:
    def test_every_profile_has_it_with_the_defaults(self, ui):
        assert ui.get('/profiles/alice').json()['agent_limits'] == {
            'max_voltage_v': 30.0, 'max_current_a': None, 'max_power_w': None}

    def test_a_config_written_before_it_existed_gets_the_defaults(self, tmp_path):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps({
            'users': ['bob'],
            'user_settings': {'bob': {'measurement': {'nplc': 2.0}}},
        }))
        limits = ConfigManager(config_file=str(path)).get_user_settings('bob')['agent_limits']
        assert limits == {'max_voltage_v': 30.0, 'max_current_a': None, 'max_power_w': None}

    def test_the_ui_sets_raises_and_clears_them(self, ui, config):
        reply = ui.patch('/profiles/alice', json={'agent_limits': {
            'max_voltage_v': 60.0, 'max_current_a': 0.5, 'max_power_w': 2.0}})
        assert reply.status_code == 200
        assert reply.json()['agent_limits'] == {
            'max_voltage_v': 60.0, 'max_current_a': 0.5, 'max_power_w': 2.0}

        reply = ui.patch('/profiles/alice', json={'agent_limits': {'max_voltage_v': None}})
        assert reply.status_code == 200
        stored = config.get_user_settings('alice')['agent_limits']
        assert stored == {'max_voltage_v': None, 'max_current_a': 0.5, 'max_power_w': 2.0}

    def test_one_users_limits_are_not_anothers(self, ui, config):
        config.add_user('bob')
        ui.patch('/profiles/alice', json={'agent_limits': {'max_current_a': 0.01}})
        assert config.get_user_settings('bob')['agent_limits']['max_current_a'] is None

    @pytest.mark.parametrize('value', [0, 0.0, -1.0, '5', True])
    def test_a_cap_is_a_positive_number(self, ui, config, value):
        reply = ui.patch('/profiles/alice', json={'agent_limits': {'max_current_a': value}})
        assert reply.status_code == 422
        assert [issue['key'] for issue in reply.json()['detail']['issues']] == ['max_current_a']
        assert config.get_user_settings('alice')['agent_limits']['max_current_a'] is None


class TestOnlyTheWindowChangesThem:
    @pytest.mark.parametrize('patch', [{'max_voltage_v': 1000.0}, {'max_voltage_v': None},
                                        {'max_current_a': 1.0}, {'max_power_w': 5.0},
                                        {'max_voltage_v': 10.0}])
    def test_an_agent_changing_a_limit_is_forbidden(self, agent, config, patch):
        reply = agent.patch('/profiles/alice', json={'agent_limits': patch})
        assert reply.status_code == 403
        assert config.get_user_settings('alice')['agent_limits'] == {
            'max_voltage_v': 30.0, 'max_current_a': None, 'max_power_w': None}

    def test_nor_alongside_an_edit_it_may_make(self, agent, config):
        reply = agent.patch('/profiles/alice', json={
            'measurement': {'nplc': 2.0}, 'agent_limits': {'max_voltage_v': 200.0}})
        assert reply.status_code == 403
        stored = config.get_user_settings('alice')
        assert stored['agent_limits']['max_voltage_v'] == 30.0
        assert stored['measurement']['nplc'] != 2.0

    def test_an_agent_may_send_them_back_unchanged(self, agent, config):
        profile = agent.get('/profiles/alice').json()
        reply = agent.patch('/profiles/alice', json={
            'measurement': {'nplc': 2.0}, 'agent_limits': profile['agent_limits']})
        assert reply.status_code == 200
        assert config.get_user_settings('alice')['measurement']['nplc'] == 2.0

    @pytest.mark.parametrize('key', ['max_voltage_v', 'max_current_a', 'max_power_w'])
    def test_a_run_request_cannot_carry_them(self, agent, key):
        reply = agent.post('/settings/resolve', json={
            'mode': 'source_v', 'username': 'alice', 'overrides': {key: 1000.0}})
        assert reply.json()['ok'] is False
        issues = reply.json()['issues']
        assert [issue['key'] for issue in issues] == [key]
        assert 'agent limit' in issues[0]['message']

        started = agent.post('/session/start', json={
            'mode': 'source_v', 'username': 'alice', 'sample_name': 's',
            'overrides': {key: 1000.0}})
        assert started.status_code == 422
        assert key in started.json()['detail']

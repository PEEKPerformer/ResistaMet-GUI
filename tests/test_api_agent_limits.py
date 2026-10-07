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

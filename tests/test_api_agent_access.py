"""The agent role: a second token, and everywhere a person is required.

The MCP design (``docs/design/mcp_layer.md`` M2) gives an AI agent a token
of its own, role ``agent``. Every role check in the API asks for ``ui``, so
these tests do not look at a check's code: they present an agent token to
each one and expect it refused, while the same token reads like any client.
"""
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

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
        # Above the 30 V default, so an agent's 60 V run reaches its prompt.
        'agent_limits': {'max_voltage_v': 100.0},
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

    @pytest.mark.parametrize('value', [True, False])
    def test_changing_allow_agents(self, agent, config, value):
        config.set_machine_local('allow_agents', not value)
        response = agent.patch('/profiles/alice', json={'measurement': {'allow_agents': value}})
        assert response.status_code == 403
        assert config.get_allow_agents() is (not value)


class TestAllowAgentsSetting:
    def test_it_is_off_by_default_and_in_every_profile(self, ui):
        assert ui.get('/profiles/alice').json()['measurement']['allow_agents'] is False

    def test_the_ui_may_change_it(self, ui, config):
        response = ui.patch('/profiles/alice', json={'measurement': {'allow_agents': True}})
        assert response.status_code == 200
        assert response.json()['measurement']['allow_agents'] is True
        assert config.get_allow_agents() is True
        assert 'allow_agents' not in config.config['user_settings']['alice']['measurement']

    @pytest.mark.parametrize('value', ['true', 1, None])
    def test_it_is_true_or_false(self, ui, config, value):
        response = ui.patch('/profiles/alice', json={'measurement': {'allow_agents': value}})
        assert response.status_code == 422
        assert config.get_allow_agents() is False

    def test_an_agent_may_resend_it_unchanged(self, agent):
        section = agent.get('/profiles/alice').json()['measurement']
        section.pop('gpib_address')
        section['nplc'] = 2.0
        response = agent.patch('/profiles/alice', json={'measurement': section})
        assert response.status_code == 200

    def test_it_can_change_during_a_run(self, ui, session, monkeypatch):
        """Unlike the bus keys: a person must be able to turn agents out mid-run."""
        monkeypatch.setattr(type(session), 'state', property(lambda self: 'running'))
        assert ui.patch('/profiles/alice', json={
            'measurement': {'gpib_interface': ''}}).status_code == 409
        assert ui.patch('/profiles/alice', json={
            'measurement': {'allow_agents': True}}).status_code == 200

    def test_a_run_request_cannot_carry_it(self, agent):
        response = agent.post('/settings/resolve', json={
            'mode': 'resistance', 'username': 'alice', 'overrides': {'allow_agents': True}})
        assert response.json()['ok'] is False
        assert [issue['key'] for issue in response.json()['issues']] == ['allow_agents']
        assert response.json()['settings']['measurement']['allow_agents'] is False


# --- turning access on and off, and the connection file ----------------------

URL = 'http://127.0.0.1:50000'
POSIX = os.name == 'posix'


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    child.wait(timeout=30)
    return child.pid


def _connection(path) -> dict:
    return json.loads(Path(path).read_text())


@pytest.fixture
def connection_file(tmp_path):
    return tmp_path / 'api' / 'connection.json'


@pytest.fixture
def live_app(session, config, connection_file):
    """An app as the sidecar has it once its socket is bound."""
    made = create_app(session, token=UI_TOKEN, config=config,
                      connection_file=str(connection_file))
    made.state.api.agent_access.set_url(URL)
    yield made
    made.state.api.agent_access.disable()


@pytest.fixture
def live_ui(live_app):
    with TestClient(live_app) as client:
        client.headers.update({'Authorization': f'Bearer {UI_TOKEN}'})
        yield client


def _as(client, token):
    return {'Authorization': f'Bearer {token}'}


class TestAgentAccess:
    def test_off_by_default_and_no_file(self, live_app, connection_file):
        assert live_app.state.api.agent_access.enabled is False
        assert not connection_file.exists()

    def test_on_writes_the_file_and_the_token_in_it_works(self, live_app, live_ui,
                                                          connection_file):
        assert live_app.state.api.agent_access.enable() is True

        written = _connection(connection_file)
        assert set(written) == {'url', 'agent_token', 'pid', 'started'}
        assert written['url'] == URL
        assert written['pid'] == os.getpid()
        assert abs(written['started'] - time.time()) < 60
        assert live_app.state.api.role_for(written['agent_token']) == AGENT_ROLE
        assert live_ui.get('/session', headers=_as(live_ui, written['agent_token'])
                           ).status_code == 200

    @pytest.mark.skipif(not POSIX, reason="mode bits; on Windows the profile directory "
                                          "is what keeps other accounts out")
    def test_only_this_user_can_read_it(self, live_app, connection_file):
        live_app.state.api.agent_access.enable()
        assert stat.S_IMODE(os.stat(connection_file).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(connection_file.parent).st_mode) & 0o077 == 0

    @pytest.mark.skipif(not POSIX, reason="mode bits")
    def test_it_is_never_readable_by_others_even_briefly(self, live_app, connection_file,
                                                         monkeypatch):
        """Created 0600, not chmod'ed afterwards: a permissive umask changes nothing."""
        opened = []
        real_open = os.open

        def spy(path, flags, mode=0o777, *args, **kwargs):
            opened.append(mode)
            return real_open(path, flags, mode, *args, **kwargs)

        monkeypatch.setattr(os, 'open', spy)
        previous = os.umask(0)
        try:
            live_app.state.api.agent_access.enable()
        finally:
            os.umask(previous)
        assert opened == [0o600]
        assert stat.S_IMODE(os.stat(connection_file).st_mode) == 0o600

    def test_enabling_before_the_url_is_known_waits_for_it(self, session, config,
                                                           connection_file):
        app = create_app(session, token=UI_TOKEN, config=config,
                         connection_file=str(connection_file))
        access = app.state.api.agent_access
        try:
            assert access.enable() is True
            assert not connection_file.exists()

            access.set_url(URL)

            assert _connection(connection_file)['url'] == URL
        finally:
            access.disable()

    def test_off_removes_the_file_and_the_token_at_once(self, live_app, live_ui,
                                                        connection_file):
        access = live_app.state.api.agent_access
        access.enable()
        token = _connection(connection_file)['agent_token']

        access.disable()

        assert not connection_file.exists()
        assert access.enabled is False
        assert live_ui.get('/session', headers=_as(live_ui, token)).status_code == 401

    def test_each_enable_mints_a_new_token(self, live_app, live_ui, connection_file):
        access = live_app.state.api.agent_access
        access.enable()
        first = _connection(connection_file)['agent_token']
        access.disable()
        access.enable()
        second = _connection(connection_file)['agent_token']

        assert second != first
        assert live_ui.get('/session', headers=_as(live_ui, first)).status_code == 401
        assert live_ui.get('/session', headers=_as(live_ui, second)).status_code == 200

    def test_enabling_twice_keeps_the_token(self, live_app, connection_file):
        access = live_app.state.api.agent_access
        access.enable()
        first = _connection(connection_file)['agent_token']
        access.enable()
        assert _connection(connection_file)['agent_token'] == first

    def test_a_stale_file_is_replaced(self, live_app, connection_file):
        connection_file.parent.mkdir(parents=True)
        connection_file.write_text(json.dumps({'url': 'http://127.0.0.1:1', 'pid': _dead_pid(),
                                               'agent_token': 'old', 'started': 0}))

        assert live_app.state.api.agent_access.enable() is True

        assert _connection(connection_file)['pid'] == os.getpid()

    @pytest.mark.parametrize('content', ['not json', '[]', '{"pid": "12"}', '{}'])
    def test_an_unreadable_file_is_replaced(self, live_app, connection_file, content):
        connection_file.parent.mkdir(parents=True)
        connection_file.write_text(content)
        assert live_app.state.api.agent_access.enable() is True
        assert _connection(connection_file)['pid'] == os.getpid()

    def test_another_live_backends_file_is_left_alone(self, live_app, live_ui,
                                                      connection_file, caplog):
        theirs = {'url': 'http://127.0.0.1:1', 'pid': os.getppid(),
                  'agent_token': 'theirs', 'started': 0}
        connection_file.parent.mkdir(parents=True)
        connection_file.write_text(json.dumps(theirs))
        access = live_app.state.api.agent_access

        with caplog.at_level('ERROR'):
            assert access.enable() is False

        assert access.enabled is False
        assert _connection(connection_file) == theirs
        assert 'another backend' in caplog.text
        # No token of ours is left that nobody was told about.
        assert set(live_app.state.api._tokens.values()) == {UI_ROLE}
        # Turning it off does not delete their file either.
        access.disable()
        assert _connection(connection_file) == theirs

    def test_another_backends_file_found_late_still_wins(self, session, config,
                                                         connection_file):
        """Enabled before the bind; by the time the URL is known, another has it."""
        app = create_app(session, token=UI_TOKEN, config=config,
                         connection_file=str(connection_file))
        access = app.state.api.agent_access
        access.enable()
        connection_file.parent.mkdir(parents=True)
        connection_file.write_text(json.dumps({'pid': os.getppid()}))

        access.set_url(URL)

        assert access.enabled is False
        assert _connection(connection_file) == {'pid': os.getppid()}

    def test_a_file_that_cannot_be_written_leaves_access_off(self, session, config,
                                                             tmp_path):
        (tmp_path / 'not-a-directory').write_text('')
        app = create_app(session, token=UI_TOKEN, config=config,
                         connection_file=str(tmp_path / 'not-a-directory' / 'c.json'))
        app.state.api.agent_access.set_url(URL)
        assert app.state.api.agent_access.enable() is False
        assert set(app.state.api._tokens.values()) == {UI_ROLE}


class TestPidIsAlive:
    def test_this_process_is(self):
        from resistamet_gui.api.agent_access import pid_is_alive
        assert pid_is_alive(os.getpid()) is True
        assert pid_is_alive(os.getppid()) is True

    def test_a_finished_one_is_not(self):
        from resistamet_gui.api.agent_access import pid_is_alive
        assert pid_is_alive(_dead_pid()) is False

    @pytest.mark.parametrize('pid', [0, -1, True, '123', None])
    def test_no_process_group_or_non_number_is(self, pid):
        from resistamet_gui.api.agent_access import pid_is_alive
        assert pid_is_alive(pid) is False


class TestTheUiSwitchesAccessLive:
    def test_on_then_off(self, live_app, live_ui, config, connection_file):
        on = live_ui.patch('/profiles/alice', json={'measurement': {'allow_agents': True}})
        assert on.status_code == 200
        assert config.get_allow_agents() is True
        assert json.loads(Path(config.machine_file).read_text())['allow_agents'] is True
        assert live_ui.get('/agents').json() == {'enabled': True}
        if POSIX:
            assert stat.S_IMODE(os.stat(connection_file).st_mode) == 0o600
        token = _connection(connection_file)['agent_token']
        assert live_ui.get('/session', headers=_as(live_ui, token)).status_code == 200

        off = live_ui.patch('/profiles/alice', json={'measurement': {'allow_agents': False}})

        assert off.status_code == 200
        assert json.loads(Path(config.machine_file).read_text())['allow_agents'] is False
        assert live_ui.get('/agents').json() == {'enabled': False}
        assert not connection_file.exists()
        assert live_ui.get('/session', headers=_as(live_ui, token)).status_code == 401

    def test_turned_on_again_it_is_a_new_token(self, live_ui, connection_file):
        live_ui.patch('/profiles/alice', json={'measurement': {'allow_agents': True}})
        first = _connection(connection_file)['agent_token']
        live_ui.patch('/profiles/alice', json={'measurement': {'allow_agents': False}})
        live_ui.patch('/profiles/alice', json={'measurement': {'allow_agents': True}})
        second = _connection(connection_file)['agent_token']

        assert second != first
        assert live_ui.get('/session', headers=_as(live_ui, first)).status_code == 401

    def test_the_agent_turned_out_cannot_turn_itself_back_on(self, live_ui, connection_file):
        live_ui.patch('/profiles/alice', json={'measurement': {'allow_agents': True}})
        token = _connection(connection_file)['agent_token']
        live_ui.patch('/profiles/alice', json={'measurement': {'allow_agents': False}})

        response = live_ui.patch('/profiles/alice', headers=_as(live_ui, token),
                                 json={'measurement': {'allow_agents': True}})

        assert response.status_code == 401
        assert live_ui.get('/agents').json() == {'enabled': False}

    def test_resending_the_stored_value_changes_nothing(self, live_app, live_ui,
                                                        connection_file):
        """--allow-agents turns access on without the setting; a Save must not undo it."""
        live_app.state.api.agent_access.enable()
        token = _connection(connection_file)['agent_token']

        live_ui.patch('/profiles/alice', json={'measurement': {'allow_agents': False}})

        assert live_ui.get('/agents').json() == {'enabled': True}
        assert _connection(connection_file)['agent_token'] == token

    def test_only_the_ui_sees_whether_it_is_on(self, live_app, live_ui):
        live_app.state.api.agent_access.enable()
        token = _connection(live_app.state.api.agent_access.path)['agent_token']
        assert live_ui.get('/agents', headers=_as(live_ui, token)).status_code == 403
        assert live_ui.get('/agents', headers=_as(live_ui, 'nope')).status_code == 401

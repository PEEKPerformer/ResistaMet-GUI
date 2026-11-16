"""The session routes: auth, dispatch, and the two failure modes.

Handlers are one-liners over MeasurementSession, so these tests check the
mapping — token in, 409 when the instrument is busy, 422 when the request
cannot be resolved — rather than re-testing measurement behaviour.
"""
import copy
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from resistamet_gui.api import create_app
from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.session.emitter import ListSink
from resistamet_gui.session.manager import MeasurementSession

TOKEN = 'test-token'


@pytest.fixture
def profile(tmp_path):
    settings = copy.deepcopy({
        'measurement': DEFAULT_SETTINGS['measurement'],
        'display': DEFAULT_SETTINGS['display'],
        'file': DEFAULT_SETTINGS['file'],
        'output': DEFAULT_SETTINGS['output'],
    })
    settings['file']['data_directory'] = str(tmp_path / 'data')
    settings['measurement'].update({'sampling_rate': 50.0, 'settling_time': 0.0,
                                     'fpp_current': 1e-4, 'fpp_voltage_compliance': 5.0,
                                     'fpp_samples': 0, 'fpp_power_warn_w': 1.0,
                                     'fpp_power_stop_w': 2.0})
    return settings


@pytest.fixture
def sink():
    return ListSink()


@pytest.fixture
def session(sink):
    made = MeasurementSession(sink)
    yield made
    made.close(timeout=5.0)


@pytest.fixture
def client(session, profile):
    app = create_app(session, token=TOKEN, profile_provider=lambda username: profile)
    with TestClient(app) as test_client:
        test_client.headers.update({'Authorization': f'Bearer {TOKEN}'})
        yield test_client


@pytest.fixture
def anon_client(session, profile):
    """A client that sends no Authorization header at all."""
    app = create_app(session, token=TOKEN, profile_provider=lambda username: profile)
    with TestClient(app) as test_client:
        yield test_client


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


def _start(client, mode='four_point', **body):
    payload = {'mode': mode, 'sample_name': 'wafer1', 'username': 'alice'}
    payload.update(body)
    return client.post('/session/start', json=payload)


class TestAuth:
    def test_health_needs_no_token(self, anon_client):
        assert anon_client.get('/health').status_code == 200

    def test_missing_token_is_rejected(self, anon_client):
        assert anon_client.get('/session').status_code == 401

    def test_wrong_token_is_rejected(self, anon_client):
        response = anon_client.get('/session', headers={'Authorization': 'Bearer nope'})
        assert response.status_code == 401


class TestStatus:
    def test_idle_status(self, client):
        body = client.get('/session').json()
        assert body['state'] == 'idle'
        assert body['run_id'] is None


class TestStart:
    def test_start_returns_the_run_id(self, client, fake_rm, sink):
        response = _start(client)
        assert response.status_code == 202
        assert response.json()['run_id'] == 'run-1'
        assert _wait_for(lambda: sink.of_type('sample'))
        client.post('/session/stop')

    def test_second_start_is_a_conflict(self, client, fake_rm, sink):
        _start(client)
        assert _wait_for(lambda: sink.of_type('sample'))
        assert _start(client).status_code == 409
        client.post('/session/stop')

    def test_unresolvable_request_is_unprocessable(self, client, fake_rm):
        response = _start(client, mode='vdp', overrides={'vdp_thickness_cm': 0.0})
        assert response.status_code == 422
        assert 'vdp_thickness_cm' in response.json()['detail']

    def test_unknown_mode_is_unprocessable(self, client, fake_rm):
        assert _start(client, mode='hall').status_code == 422


class TestCommands:
    def test_stop_returns_to_idle(self, client, fake_rm, sink):
        _start(client)
        assert _wait_for(lambda: sink.of_type('sample'))
        client.post('/session/stop')
        assert _wait_for(lambda: client.get('/session').json()['state'] == 'idle')

    def test_pause_and_resume(self, client, fake_rm, sink):
        _start(client)
        assert _wait_for(lambda: sink.of_type('sample'))
        client.post('/session/pause')
        assert _wait_for(lambda: client.get('/session').json()['state'] == 'paused')
        client.post('/session/resume')
        assert _wait_for(lambda: client.get('/session').json()['state'] == 'running')
        client.post('/session/stop')

    def test_mark_reaches_a_sample(self, client, fake_rm, sink):
        _start(client)
        assert _wait_for(lambda: sink.of_type('sample'))
        assert client.post('/session/mark', json={'label': 'PROBE'}).status_code == 200
        assert _wait_for(lambda: any(e.payload['event_marker'] == 'PROBE'
                                      for e in sink.of_type('sample')))
        client.post('/session/stop')

    def test_commands_without_a_run_are_conflicts(self, client):
        assert client.post('/session/mark', json={'label': 'X'}).status_code == 409
        assert client.post('/session/pause').status_code == 409

    def test_abort_is_accepted(self, client, fake_rm, sink):
        _start(client)
        assert _wait_for(lambda: sink.of_type('sample'))
        assert client.post('/session/abort').status_code == 200
        assert _wait_for(lambda: client.get('/session').json()['state'] == 'idle')


class TestPromptAuthorization:
    def _hazardous(self, profile):
        profile['measurement'].update({
            'safety_voltage_warn_v': 30.0, 'safety_voltage_warn_silenced': False,
            'vsource_voltage': 60.0, 'vsource_duration_hours': 0.0,
        })

    def test_ui_role_may_answer_a_human_prompt(self, client, fake_rm, sink, profile):
        self._hazardous(profile)
        _start(client, mode='source_v')
        assert _wait_for(lambda: client.get('/session').json()['pending_prompt'])

        prompt = client.get('/session').json()['pending_prompt']
        response = client.post('/session/prompt', json={'prompt_id': prompt['prompt_id'],
                                                         'choice': 'acknowledge'})
        assert response.status_code == 200
        assert _wait_for(lambda: sink.of_type('sample'))
        client.post('/session/stop')

    def test_non_ui_role_is_refused(self, session, profile, fake_rm, sink):
        """D4: an MCP client cannot claim a human made a decision."""
        self._hazardous(profile)
        app = create_app(session, token=TOKEN, role='mcp',
                          profile_provider=lambda username: profile)
        with TestClient(app) as client:
            client.headers.update({'Authorization': f'Bearer {TOKEN}'})
            _start(client, mode='source_v')
            assert _wait_for(lambda: client.get('/session').json()['pending_prompt'])
            prompt = client.get('/session').json()['pending_prompt']

            response = client.post('/session/prompt',
                                    json={'prompt_id': prompt['prompt_id'],
                                          'choice': 'acknowledge'})
            assert response.status_code == 403
            client.post('/session/abort')

    def test_stale_prompt_id_is_a_conflict(self, client, fake_rm, sink, profile):
        self._hazardous(profile)
        _start(client, mode='source_v')
        assert _wait_for(lambda: client.get('/session').json()['pending_prompt'])

        response = client.post('/session/prompt', json={'prompt_id': 'nope',
                                                         'choice': 'acknowledge'})
        assert response.status_code == 409
        client.post('/session/abort')

    def test_answering_with_no_prompt_is_a_conflict(self, client):
        response = client.post('/session/prompt', json={'prompt_id': 'x', 'choice': 'y'})
        assert response.status_code == 409


class TestCors:
    """The webview is a different origin from the backend; others are refused."""

    def test_desktop_origin_is_allowed(self, client):
        response = client.options('/session', headers={
            'Origin': 'tauri://localhost',
            'Access-Control-Request-Method': 'GET',
            'Access-Control-Request-Headers': 'authorization',
        })
        assert response.status_code == 200
        assert response.headers.get('access-control-allow-origin') == 'tauri://localhost'

    def test_unknown_origin_gets_no_allowance(self, client):
        response = client.get('/session', headers={'Origin': 'https://example.com'})
        assert 'access-control-allow-origin' not in response.headers

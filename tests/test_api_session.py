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

    @pytest.mark.parametrize("mode, override", [
        ('sweep', {'sweep_step': 'abc'}),
        ('sweep', {'sweep_step': None}),  # was a TypeError, so a 500
        ('resistance', {'nplc': 'fast'}),
        ('four_point', {'fpp_current': None}),
        ('vdp', {'vdp_thickness_cm': None}),
    ])
    def test_a_value_that_cannot_be_read_is_unprocessable_by_key(self, client, fake_rm, sink,
                                                                 mode, override):
        response = _start(client, mode=mode, overrides=override)
        assert response.status_code == 422
        assert next(iter(override)) in response.json()['detail']
        assert client.get('/session').json()['state'] == 'idle'
        assert fake_rm.opened == []

    def test_unknown_mode_is_unprocessable(self, client, fake_rm):
        assert _start(client, mode='hall').status_code == 422

    @pytest.mark.parametrize("field, value", [
        ('sample_name', "wafer\n# total_samples: 999\n0.0,1,1,1,1,OK,"),
        ('sample_name', 'x' * 121),
        ('username', "alice\nroot"),
        ('username', 'x' * 65),
        ('prompt_timeout_s', 1e9),
    ])
    def test_text_that_could_forge_a_header_line_is_unprocessable(self, client, fake_rm, sink,
                                                                  field, value):
        response = _start(client, **{field: value})
        assert response.status_code == 422
        assert [error['loc'] for error in response.json()['detail']] == [['body', field]]
        assert sink.events == []
        assert fake_rm.opened == []

    def test_an_infinite_prompt_timeout_is_unprocessable(self, client, fake_rm, sink):
        """json.dumps writes Infinity and the server's parser reads it."""
        response = client.post('/session/start', content=(
            '{"mode": "four_point", "sample_name": "w", "username": "alice", '
            '"prompt_timeout_s": Infinity}'), headers={'Content-Type': 'application/json'})
        assert response.status_code == 422, "not a 500 from failing to echo the input"
        assert [error['loc'] for error in response.json()['detail']] == [
            ['body', 'prompt_timeout_s']]
        assert sink.events == []

    def test_the_body_is_the_exported_contract(self, client):
        """The desktop's types are generated from RunRequest; a second model
        here could drift from it without anything failing."""
        body = client.app.openapi()['paths']['/session/start']['post']['requestBody']
        assert body['content']['application/json']['schema']['$ref'].endswith('/RunRequest')

    @pytest.mark.parametrize("typo", ['sample', 'override', 'prompt_timeout', 'acknowledge'])
    def test_a_misspelt_field_fails_loudly(self, client, fake_rm, sink, typo):
        """It used to be dropped, and the run started without it."""
        response = _start(client, **{typo: 'x'})
        assert response.status_code == 422
        assert [error['loc'] for error in response.json()['detail']] == [['body', typo]]
        assert sink.events == []
        assert fake_rm.opened == []


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

    # Named ids: pytest puts the test id in PYTEST_CURRENT_TEST, and Windows
    # refuses an environment variable as long as the 200 kB label.
    @pytest.mark.parametrize("label", ['x' * 200_000, 'x' * 81, "two\nlines", "", "  "],
                             ids=['200kB', '81-chars', 'two-lines', 'empty', 'spaces'])
    def test_a_mark_label_is_one_short_line(self, client, fake_rm, sink, label):
        _start(client)
        assert _wait_for(lambda: sink.of_type('sample'))
        response = client.post('/session/mark', json={'label': label})
        assert response.status_code == 422
        assert [error['loc'] for error in response.json()['detail']] == [['body', 'label']]
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

    @pytest.mark.parametrize("override", [
        {'safety_voltage_warn_silenced': True},
        {'safety_voltage_warn_v': 200.0},
    ])
    def test_non_ui_role_cannot_avoid_the_question_either(self, session, profile, fake_rm,
                                                          sink, override):
        """D4 from the other side: the answer it may not give, it may not
        make unnecessary by moving the threshold or the silenced flag."""
        self._hazardous(profile)
        app = create_app(session, token=TOKEN, role='mcp',
                          profile_provider=lambda username: profile)
        with TestClient(app) as client:
            client.headers.update({'Authorization': f'Bearer {TOKEN}'})
            response = _start(client, mode='source_v', overrides=override)
            assert response.status_code == 422
            assert next(iter(override)) in str(response.json()['detail'])
            assert client.get('/session').json()['state'] == 'idle'
        assert fake_rm.opened == []
        assert sink.of_type('sample') == []

    def test_stale_prompt_id_is_a_conflict(self, client, fake_rm, sink, profile):
        self._hazardous(profile)
        _start(client, mode='source_v')
        assert _wait_for(lambda: client.get('/session').json()['pending_prompt'])

        response = client.post('/session/prompt', json={'prompt_id': 'nope',
                                                         'choice': 'acknowledge'})
        assert response.status_code == 409
        client.post('/session/abort')

    def test_a_choice_the_prompt_did_not_offer_is_unprocessable(self, client, fake_rm, sink,
                                                                profile):
        self._hazardous(profile)
        _start(client, mode='source_v')
        assert _wait_for(lambda: client.get('/session').json()['pending_prompt'])
        prompt = client.get('/session').json()['pending_prompt']

        response = client.post('/session/prompt', json={'prompt_id': prompt['prompt_id'],
                                                         'choice': 'proceed'})
        assert response.status_code == 422
        assert 'acknowledge, cancel' in response.json()['detail']
        # Still waiting for a real answer; nothing was energised.
        assert client.get('/session').json()['pending_prompt']['prompt_id'] == prompt['prompt_id']
        assert sink.of_type('instrument_connected') == []
        client.post('/session/abort')

    def test_a_wrong_choice_for_a_stale_id_is_still_a_conflict(self, client, fake_rm, sink,
                                                               profile):
        self._hazardous(profile)
        _start(client, mode='source_v')
        assert _wait_for(lambda: client.get('/session').json()['pending_prompt'])
        response = client.post('/session/prompt', json={'prompt_id': 'nope', 'choice': 'x'})
        assert response.status_code == 409
        client.post('/session/abort')

    def test_a_non_ui_role_is_refused_before_its_choice_is_looked_at(self, session, profile,
                                                                     fake_rm, sink):
        self._hazardous(profile)
        app = create_app(session, token=TOKEN, role='mcp',
                          profile_provider=lambda username: profile)
        with TestClient(app) as client:
            client.headers.update({'Authorization': f'Bearer {TOKEN}'})
            _start(client, mode='source_v')
            assert _wait_for(lambda: client.get('/session').json()['pending_prompt'])
            prompt = client.get('/session').json()['pending_prompt']
            response = client.post('/session/prompt', json={
                'prompt_id': prompt['prompt_id'], 'choice': 'not-an-option'})
            assert response.status_code == 403
            client.post('/session/abort')

    def test_an_answer_meant_for_another_run_is_a_conflict(self, client, fake_rm, sink, profile):
        """Prompt ids repeat: every run's first safety prompt is ...-1."""
        self._hazardous(profile)
        _start(client, mode='source_v')
        assert _wait_for(lambda: client.get('/session').json()['pending_prompt'])
        status_now = client.get('/session').json()
        prompt = status_now['pending_prompt']

        stale = client.post('/session/prompt', json={
            'prompt_id': prompt['prompt_id'], 'choice': 'acknowledge', 'run_id': 'run-0'})
        assert stale.status_code == 409
        assert 'run-0' in stale.json()['detail']
        assert client.get('/session').json()['pending_prompt'] is not None
        assert sink.of_type('instrument_connected') == []

        current = client.post('/session/prompt', json={
            'prompt_id': prompt['prompt_id'], 'choice': 'cancel',
            'run_id': status_now['run_id']})
        assert current.status_code == 200

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


class TestStartWithASpot:
    SPOT = {'map_id': 'wafer7', 'index': 1, 'label': 'centre'}

    def test_the_spot_reaches_the_run_settings(self, client, fake_rm, sink):
        assert _start(client, spot=self.SPOT).status_code == 202
        assert _wait_for(lambda: sink.of_type('run_started'))
        started = sink.of_type('run_started')[0].payload
        assert started['settings']['spot']['map_id'] == 'wafer7'
        assert started['settings']['spot']['label'] == 'centre'
        client.post('/session/stop')

    def test_a_map_id_that_could_build_a_path_is_unprocessable(self, client, fake_rm, sink):
        for map_id in ('../wafer7', 'a/b', '..', ''):
            response = _start(client, spot={**self.SPOT, 'map_id': map_id})
            assert response.status_code == 422, map_id
        assert sink.events == []

    def test_only_four_point_may_carry_one(self, client, fake_rm, sink):
        response = _start(client, mode='resistance', spot=self.SPOT)
        assert response.status_code == 422
        assert 'four_point' in str(response.json()['detail'])
        assert sink.events == []


class TestStartWithAClient:
    """Which program asked for the run, in the file header."""

    CLIENT = {'name': 'resistamet-desktop', 'version': '2.0.0-1'}

    def _start_as(self, http, who):
        # Not _start(): its first parameter is already called "client".
        body = {'mode': 'four_point', 'sample_name': 'wafer1', 'username': 'alice'}
        if who is not None:
            body['client'] = who
        return http.post('/session/start', json=body)

    def _header_of_a_stopped_run(self, http, sink, who):
        from resistamet_gui.data_export import parse_metadata
        assert self._start_as(http, who).status_code == 202
        assert _wait_for(lambda: sink.of_type('sample'))
        http.post('/session/stop')
        assert _wait_for(lambda: sink.of_type('run_ended'))
        path = sink.of_type('file_finalized')[0].payload['path']
        return parse_metadata(path, text_keys=('client.name', 'client.version'))

    def test_the_client_is_written_to_the_header(self, client, fake_rm, sink):
        header = self._header_of_a_stopped_run(client, sink, self.CLIENT)
        assert header['client.name'] == 'resistamet-desktop'
        assert header['client.version'] == '2.0.0-1'
        # The backend's own version is still there, and still its own.
        from resistamet_gui.constants import __version__
        assert str(header['software_version']) == __version__

    def test_no_client_means_nothing_new_in_the_header(self, client, fake_rm, sink):
        header = self._header_of_a_stopped_run(client, sink, None)
        assert [key for key in header if key.startswith('client')] == []

    @pytest.mark.parametrize("bad", [
        {'name': 'resistamet-desktop'},                         # no version
        {'name': '', 'version': '1'},
        {'name': 'x' * 65, 'version': '1'},
        {'name': 'desktop\n# user: mallory', 'version': '1'},   # would forge a header line
        {'name': 'desktop', 'version': '1', 'token': 'extra'},
        'resistamet-desktop',
    ])
    def test_a_malformed_client_is_unprocessable(self, client, fake_rm, sink, bad):
        assert self._start_as(client, bad).status_code == 422
        assert sink.events == []

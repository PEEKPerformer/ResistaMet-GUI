"""An agent's run, as a person at the bench sees it during and afterwards.

``docs/design/mcp_layer.md`` M5: a hazardous run an agent started asks the
touch-safety question even on a profile a person silenced, because the
silence was given for the runs that person starts.

M6: the server records which role started a run, from the token, where a
person will look: the ``run_started`` event, the session status and the
data file's header.
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

        The answer's ``silence_for_profile`` is logged, as on any run, and
        saved to the profile; it silences the person's own runs and never
        reaches an agent's.
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
        assert ui.get('/profiles/alice').json()['measurement']['safety_voltage_warn_silenced']

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


class TestTheServerStampsWhoStartedTheRun:
    @pytest.mark.parametrize('who', ['ui', 'agent'])
    def test_in_run_started_and_the_status(self, request, fake_rm, sink, who):
        client = request.getfixturevalue(who)
        assert _start(client, {'vsource_voltage': 5.0}).status_code == 202
        assert _wait_for(lambda: sink.of_type('sample'))

        assert sink.of_type('run_started')[0].payload['started_by'] == who
        assert client.get('/session').json()['started_by'] == who
        _end(client)

    def test_the_request_cannot_say(self, agent, fake_rm, sink):
        response = agent.post('/session/start', json={
            'mode': 'source_v', 'username': 'alice', 'sample_name': 'wafer1',
            'overrides': {'vsource_voltage': 5.0}, 'started_by': 'ui'})
        assert response.status_code == 422
        assert 'started_by' in str(response.json()['detail'])
        assert sink.events == []
        assert fake_rm.opened == []

    def test_nor_can_its_overrides(self, agent, fake_rm, sink):
        response = _start(agent, {'vsource_voltage': 5.0, 'started_by': 'ui'})
        assert response.status_code == 422
        assert sink.events == []

    @pytest.mark.parametrize('who', ['ui', 'agent'])
    def test_in_the_file_header(self, request, fake_rm, sink, who):
        from resistamet_gui.data_export import parse_metadata

        client = request.getfixturevalue(who)
        assert _start(client, {'vsource_voltage': 5.0}).status_code == 202
        assert _wait_for(lambda: sink.of_type('sample'))
        _end(client)
        path = sink.of_type('file_finalized')[0].payload['path']

        assert f"# started_by: {who}\n" in open(path, encoding='utf-8').read()
        assert parse_metadata(path)['started_by'] == who


DAY = 86400.0


class TestAnsweringTheSafetyPromptSavesTheSilence:
    """Asked for in the answer, the silence is kept on the run's profile."""

    def _asked(self, client):
        assert _start(client).status_code == 202
        assert _wait_for(lambda: _pending(client) is not None)
        return _pending(client)

    def _answer(self, client, prompt, choice='acknowledge', **fields):
        return client.post('/session/prompt', json={
            'prompt_id': prompt['prompt_id'], 'choice': choice, 'fields': fields})

    def _stored(self, config):
        # Read back from the file, so the save reached the disk.
        reloaded = ConfigManager(config_file=config.config_file)
        return reloaded.get_user_settings('alice')['measurement']

    def test_silence_for_profile_saves_the_flag(self, ui, config, fake_rm, sink):
        assert self._answer(ui, self._asked(ui), silence_for_profile=True).status_code == 200
        _end(ui)

        stored = self._stored(config)
        assert stored['safety_voltage_warn_silenced'] is True
        assert stored['safety_voltage_warn_silenced_until'] is None

    def test_silence_for_days_saves_when_it_runs_out(self, ui, config, fake_rm, sink):
        prompt = self._asked(ui)
        before = time.time()
        assert self._answer(ui, prompt, silence_for_days=7).status_code == 200
        after = time.time()
        _end(ui)

        stored = self._stored(config)
        assert before + 7 * DAY <= stored['safety_voltage_warn_silenced_until'] <= after + 7 * DAY
        assert stored['safety_voltage_warn_silenced'] is False
        log = [e.payload['message'] for e in sink.of_type('log')
               if e.payload['code'] == 'safety_silenced']
        assert log == ["Touch-safety warning silenced for this profile for 7 days."]

    def test_a_fraction_of_a_day_is_a_number_of_days_too(self, ui, config, fake_rm, sink):
        prompt = self._asked(ui)
        before = time.time()
        assert self._answer(ui, prompt, silence_for_days=0.5).status_code == 200
        _end(ui)
        assert self._stored(config)['safety_voltage_warn_silenced_until'] >= before + 0.5 * DAY

    @pytest.mark.parametrize('fields', [{'silence_for_profile': True},
                                         {'silence_for_days': 7}])
    def test_cancel_saves_nothing(self, ui, config, fake_rm, sink, fields):
        before = self._stored(config)
        assert self._answer(ui, self._asked(ui), choice='cancel', **fields).status_code == 200
        assert _wait_for(lambda: ui.get('/session').json()['state'] == 'idle')

        assert self._stored(config) == before
        assert 'safety_silenced' not in [e.payload['code'] for e in sink.of_type('log')]

    def test_asking_for_no_silence_saves_nothing(self, ui, config, fake_rm, sink):
        before = self._stored(config)
        assert self._answer(ui, self._asked(ui), silence_for_profile=False).status_code == 200
        _end(ui)
        assert self._stored(config) == before

    @pytest.mark.parametrize('fields', [
        {'silence_forever': True},
        {'silence_for_days': 0},
        {'silence_for_days': -1},
        {'silence_for_days': 366},
        {'silence_for_days': '7'},
        {'silence_for_days': True},
        {'silence_for_days': None, 'note': 'x'},
        {'silence_for_profile': 'yes'},
        {'silence_for_profile': True, 'silence_for_days': 7},
    ])
    def test_fields_it_does_not_take_are_refused_and_it_stays_pending(
            self, ui, config, fake_rm, sink, fields):
        before = self._stored(config)
        prompt = self._asked(ui)

        response = self._answer(ui, prompt, **fields)

        assert response.status_code == 422
        assert _pending(ui)['prompt_id'] == prompt['prompt_id']
        assert self._stored(config) == before
        assert fake_rm.opened == []
        # Still answerable, properly.
        assert self._answer(ui, prompt, choice='cancel').status_code == 200
        assert _wait_for(lambda: ui.get('/session').json()['state'] == 'idle')

    def test_a_silence_saved_at_the_prompt_spares_the_window_s_next_run(
            self, ui, config, fake_rm, sink):
        assert self._answer(ui, self._asked(ui), silence_for_days=7).status_code == 200
        _end(ui)

        run_id = _start(ui).json()['run_id']
        assert _wait_for(lambda: any(e.run_id == run_id for e in sink.of_type('sample')))
        assert len(sink.of_type('prompt')) == 1
        _end(ui)

    def test_but_not_an_agent_s_next_run(self, ui, agent, config, fake_rm, sink):
        """mcp_layer.md M5: the silence is for the runs the person starts."""
        assert self._answer(ui, self._asked(ui), silence_for_days=7).status_code == 200
        _end(ui)

        assert self._asked(agent)['kind'] == 'safety_voltage_ack'
        assert len(sink.of_type('prompt')) == 2
        _end(ui)

    def test_an_agent_cannot_answer_it_to_save_one(self, ui, agent, config, fake_rm, sink):
        before = self._stored(config)
        prompt = self._asked(ui)
        assert self._answer(agent, prompt, silence_for_profile=True).status_code == 403
        assert self._stored(config) == before
        _end(ui)

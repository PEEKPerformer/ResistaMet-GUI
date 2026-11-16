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


# --- the check at start ------------------------------------------------------

NO_CAPS = {'max_voltage_v': None, 'max_current_a': None, 'max_power_w': None}

#: One case per mode: the profile's limits, overrides exactly at them, the
#: key that goes just over, its value there, and the limit it breaks.
AT_AND_OVER = [
    ('resistance', {'max_current_a': 0.01},
     {'res_test_current': 0.01, 'res_voltage_compliance': 5.0, 'res_auto_range': False},
     'res_test_current', 0.0101, 'max_current_a'),
    ('source_v', {'max_voltage_v': 5.0},
     {'vsource_voltage': 5.0, 'vsource_current_compliance': 0.01},
     'vsource_voltage', 5.5, 'max_voltage_v'),
    ('source_i', {'max_voltage_v': 5.0},
     {'isource_current': 1e-3, 'isource_voltage_compliance': 5.0},
     'isource_voltage_compliance', 6.0, 'max_voltage_v'),
    # 100 uA x 10 V = 1 mW at the limit; 100 uA x 11 V = 1.1 mW over it.
    ('four_point', {'max_power_w': 1e-3},
     {'fpp_current': 1e-4, 'fpp_voltage_compliance': 10.0, 'fpp_samples': 0},
     'fpp_voltage_compliance', 11.0, 'max_power_w'),
    ('sweep', {'max_voltage_v': 5.0},
     {'sweep_source': 'voltage', 'sweep_start': 0.0, 'sweep_stop': 5.0, 'sweep_step': 1.0,
      'sweep_compliance': 0.01},
     'sweep_stop', 5.5, 'max_voltage_v'),
    ('vdp', {'max_current_a': 1e-3},
     {'vdp_current': 1e-3, 'vdp_voltage_compliance': 5.0, 'vdp_thickness_cm': 0.1},
     'vdp_current', 2e-3, 'max_current_a'),
]


def _wait_for(predicate, timeout=5.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _start(client, mode, overrides):
    return client.post('/session/start', json={
        'mode': mode, 'username': 'alice', 'sample_name': 'wafer1', 'overrides': overrides})


def _end(ui):
    ui.post('/session/abort')
    assert _wait_for(lambda: ui.get('/session').json()['state'] == 'idle')


def _limits(ui, limits):
    assert ui.patch('/profiles/alice', json={'agent_limits': limits}).status_code == 200


class TestStartAsAnAgent:
    @pytest.mark.parametrize('mode, limits, overrides, key, over, limit', AT_AND_OVER,
                             ids=[case[0] for case in AT_AND_OVER])
    def test_just_over_a_limit_is_refused_before_anything_opens(
            self, agent, ui, fake_rm, mode, limits, overrides, key, over, limit):
        _limits(ui, {**NO_CAPS, **limits})
        reply = _start(agent, mode, {**overrides, key: over})
        assert reply.status_code == 422
        detail = reply.json()['detail']
        [violation] = detail['violations']
        assert violation['limit'] == limit
        assert violation['source'] == 'agent_limits'
        assert key in violation['keys']
        assert violation['allowed'] == limits[limit]
        assert violation['value'] > limits[limit]
        assert key in detail['message'] and limit in detail['message']
        assert fake_rm.opened == []
        assert ui.get('/session').json()['run_id'] is None

    @pytest.mark.parametrize('mode, limits, overrides, key, over, limit', AT_AND_OVER,
                             ids=[case[0] for case in AT_AND_OVER])
    def test_exactly_at_a_limit_starts(self, agent, ui, fake_rm, mode, limits, overrides,
                                        key, over, limit):
        _limits(ui, {**NO_CAPS, **limits})
        assert _start(agent, mode, overrides).status_code == 202
        _end(ui)

    def test_the_default_is_30_v(self, agent, ui, fake_rm):
        reply = _start(agent, 'source_v', {'vsource_voltage': 30.5})
        assert reply.status_code == 422
        assert reply.json()['detail']['violations'][0]['allowed'] == 30.0
        assert _start(agent, 'source_v', {'vsource_voltage': 30.0}).status_code == 202
        _end(ui)

    def test_a_current_source_counts_its_compliance_as_the_voltage(self, agent, fake_rm):
        reply = _start(agent, 'source_i', {'isource_current': 1e-6,
                                           'isource_voltage_compliance': 31.0})
        assert reply.status_code == 422
        assert reply.json()['detail']['violations'][0]['keys'] == ['isource_voltage_compliance']

    def test_none_caps_nothing(self, agent, ui, fake_rm):
        _limits(ui, NO_CAPS)
        assert _start(agent, 'source_v', {'vsource_voltage': 150.0,
                                          'vsource_current_compliance': 0.1}).status_code == 202
        _end(ui)

    def test_the_window_is_never_checked(self, ui, fake_rm):
        _limits(ui, {'max_voltage_v': 1.0, 'max_current_a': 1e-6, 'max_power_w': 1e-9})
        assert _start(ui, 'source_v', {'vsource_voltage': 5.0,
                                       'vsource_current_compliance': 0.1}).status_code == 202
        _end(ui)

    def test_a_request_with_errors_is_refused_for_those_first(self, agent, fake_rm):
        reply = _start(agent, 'source_v', {'vsource_voltage': 500.0})
        assert reply.status_code == 422
        assert isinstance(reply.json()['detail'], str)
        assert 'vsource_voltage' in reply.json()['detail']


class TestTheConnectedModel:
    """The fake instrument answers *IDN? as a 2420: 63 V, 3.15 A, 66 W."""

    def test_a_known_model_refuses_what_it_cannot_do(self, agent, ui, fake_rm):
        _limits(ui, NO_CAPS)
        assert agent.post('/instruments/identify',
                          json={'address': 'GPIB0::24::INSTR'}).json()['model'] == '2420'
        reply = _start(agent, 'source_v', {'vsource_voltage': 70.0,
                                           'vsource_current_compliance': 0.01})
        assert reply.status_code == 422
        [violation] = reply.json()['detail']['violations']
        assert (violation['source'], violation['model'], violation['limit']) == \
            ('model', '2420', 'max_source_v')
        assert (violation['value'], violation['allowed']) == (70.0, 63.0)
        assert '2420' in reply.json()['detail']['message']

    def test_at_the_models_limit_it_starts(self, agent, ui, fake_rm):
        _limits(ui, NO_CAPS)
        agent.post('/instruments/identify', json={'address': 'GPIB0::24::INSTR'})
        assert _start(agent, 'source_v', {'vsource_voltage': 63.0,
                                          'vsource_current_compliance': 0.01}).status_code == 202
        _end(ui)

    def test_an_unknown_model_is_left_to_the_instrument(self, agent, ui, fake_rm):
        _limits(ui, NO_CAPS)
        assert ui.get('/session').json()['instrument'] is None
        assert _start(agent, 'source_v', {'vsource_voltage': 70.0,
                                          'vsource_current_compliance': 0.01}).status_code == 202
        _end(ui)

    def test_a_model_found_at_another_address_is_not_this_one(self, agent, ui, config,
                                                               fake_rm):
        _limits(ui, NO_CAPS)
        agent.post('/instruments/identify', json={'address': 'GPIB0::24::INSTR'})
        assert ui.get('/session').json()['instrument']['model'] == '2420'
        config.set_gpib_address('GPIB0::5::INSTR')
        assert _start(agent, 'source_v', {'vsource_voltage': 70.0,
                                          'vsource_current_compliance': 0.01}).status_code == 202
        _end(ui)


# --- the dry run ---------------------------------------------------------------

def _resolve(client, mode, overrides):
    reply = client.post('/settings/resolve', json={
        'mode': mode, 'username': 'alice', 'overrides': overrides})
    assert reply.status_code == 200
    return reply.json()


class TestResolveGivesTheVerdictStartWould:
    @pytest.mark.parametrize('mode, limits, overrides, key, over, limit', AT_AND_OVER,
                             ids=[case[0] for case in AT_AND_OVER])
    def test_over_and_at(self, agent, ui, fake_rm, mode, limits, overrides, key, over, limit):
        _limits(ui, {**NO_CAPS, **limits})
        refused = _start(agent, mode, {**overrides, key: over}).json()['detail']['violations']
        for client in (agent, ui):
            verdict = _resolve(client, mode, {**overrides, key: over})['agent_limits']
            assert verdict == {'ok': False, 'violations': refused}
            assert _resolve(client, mode, overrides)['agent_limits'] == \
                {'ok': True, 'violations': []}

    def test_the_connected_model_too(self, agent, ui, fake_rm):
        _limits(ui, NO_CAPS)
        agent.post('/instruments/identify', json={'address': 'GPIB0::24::INSTR'})
        overrides = {'vsource_voltage': 70.0, 'vsource_current_compliance': 0.01}
        refused = _start(agent, 'source_v', overrides).json()['detail']['violations']
        verdict = _resolve(agent, 'source_v', overrides)['agent_limits']
        assert verdict == {'ok': False, 'violations': refused}
        assert refused[0]['model'] == '2420'

    def test_no_verdict_on_settings_with_errors(self, agent):
        body = _resolve(agent, 'source_v', {'vsource_voltage': 500.0})
        assert body['ok'] is False
        assert body['agent_limits'] is None

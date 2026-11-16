"""Users, profiles, resolution and instrument discovery over HTTP."""
import copy
import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from resistamet_gui.api import create_app
from resistamet_gui.config import ConfigManager
from resistamet_gui.session.emitter import ListSink
from resistamet_gui.session.manager import MeasurementSession

TOKEN = 'test-token'
PRLGX = 'PRLGX-ASRL::/dev/cu.usbserial-PX12345::INTFC'


@pytest.fixture
def config(tmp_path):
    manager = ConfigManager(config_file=str(tmp_path / 'config.json'))
    manager.add_user('alice')
    manager.update_user_settings('alice', {
        'file': {'data_directory': str(tmp_path / 'data'), 'auto_save_interval': 60},
        'measurement': {'res_test_current': 2e-3, 'sampling_rate': 50.0,
                         'settling_time': 0.0, 'fpp_samples': 0,
                         'fpp_current': 1e-4, 'fpp_voltage_compliance': 5.0,
                         'fpp_power_warn_w': 1.0, 'fpp_power_stop_w': 2.0},
    })
    return manager


@pytest.fixture
def session():
    made = MeasurementSession(ListSink())
    yield made
    made.close(timeout=5.0)


@pytest.fixture
def client(session, config):
    app = create_app(session, token=TOKEN, config=config)
    with TestClient(app) as test_client:
        test_client.headers.update({'Authorization': f'Bearer {TOKEN}'})
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


class TestUsersAndProfiles:
    def test_users_are_listed(self, client):
        assert 'alice' in client.get('/users').json()['users']

    def test_profile_round_trips(self, client):
        profile = client.get('/profiles/alice').json()
        assert profile['measurement']['res_test_current'] == 2e-3

    def test_patch_updates_one_section(self, client):
        response = client.patch('/profiles/alice',
                                 json={'measurement': {'res_test_current': 5e-4}})
        assert response.status_code == 200
        assert response.json()['measurement']['res_test_current'] == 5e-4

    def test_patch_needs_something_to_change(self, client):
        assert client.patch('/profiles/alice', json={}).status_code == 422

    def test_patch_leaves_the_other_keys_of_the_section_alone(self, client, config):
        before = client.get('/profiles/alice').json()

        response = client.patch('/profiles/alice',
                                 json={'measurement': {'res_test_current': 5e-4}})

        assert response.status_code == 200
        after = response.json()
        assert after['measurement']['res_test_current'] == 5e-4
        before['measurement']['res_test_current'] = 5e-4
        assert after == before
        stored = config.config['user_settings']['alice']['measurement']
        assert stored['fpp_current'] == 1e-4
        assert stored['sampling_rate'] == 50.0

    def test_an_address_only_patch_does_not_touch_the_stored_profile(self, client, config):
        stored_before = copy.deepcopy(config.config['user_settings']['alice'])

        response = client.patch('/profiles/alice', json={'measurement': {
            'gpib_address': 'GPIB0::9::INSTR', 'visa_library': '@py', 'gpib_interface': ''}})

        assert response.status_code == 200
        assert config.config['user_settings']['alice'] == stored_before
        assert config.get_gpib_address() == 'GPIB0::9::INSTR'
        assert response.json()['measurement']['fpp_current'] == 1e-4
        assert response.json()['measurement']['gpib_address'] == 'GPIB0::9::INSTR'

    def test_first_patch_keeps_what_the_user_was_running_on(self, client, config):
        """A user with no stored overrides runs on the shared sections."""
        config.update_global_settings({'measurement': {'sampling_rate': 3.0}})
        config.add_user('bob')
        assert client.get('/profiles/bob').json()['measurement']['sampling_rate'] == 3.0

        response = client.patch('/profiles/bob', json={'measurement': {'nplc': 2.0}})

        assert response.status_code == 200
        assert response.json()['measurement']['nplc'] == 2.0
        assert response.json()['measurement']['sampling_rate'] == 3.0

    def test_patch_for_an_unknown_user_is_not_found(self, client, config):
        response = client.patch('/profiles/ghost',
                                 json={'measurement': {'res_test_current': 5e-4}})
        assert response.status_code == 404
        assert 'ghost' not in config.config.get('user_settings', {})

    def test_invalid_values_are_refused_and_nothing_is_stored(self, client, config, tmp_path):
        stored_before = copy.deepcopy(config.config['user_settings']['alice'])

        response = client.patch('/profiles/alice', json={'measurement': {
            'nplc': 'banana', 'res_test_current': 1e9, 'sampling_rate': 5.0}})

        assert response.status_code == 422
        keys = {issue['key'] for issue in response.json()['detail']['issues']}
        assert keys == {'nplc', 'res_test_current'}
        assert config.config['user_settings']['alice'] == stored_before
        reloaded = ConfigManager(config_file=str(tmp_path / 'config.json'))
        assert reloaded.config['user_settings']['alice'] == stored_before

    def test_invalid_values_in_other_sections_are_refused(self, client):
        response = client.patch('/profiles/alice', json={'output': {'format': 'xlsx'}})
        assert response.status_code == 422
        assert response.json()['detail']['issues'][0]['section'] == 'output'

    def test_an_old_out_of_range_value_does_not_block_other_edits(self, client, config):
        """A drifted profile must stay editable, one key at a time."""
        config.update_user_settings('alice', {'measurement': {'vdp_current': 5.0}})

        response = client.patch('/profiles/alice', json={'measurement': {'nplc': 2.0}})

        assert response.status_code == 200
        assert response.json()['measurement']['vdp_current'] == 5.0

    def test_resending_an_old_out_of_range_value_does_not_block_either(self, client, config):
        """The desktop dialog sends the whole section it edited."""
        config.update_user_settings('alice', {'measurement': {'vdp_current': 5.0}})
        section = client.get('/profiles/alice').json()['measurement']
        section['nplc'] = 2.0

        assert client.patch('/profiles/alice', json={'measurement': section}).status_code == 200

        section['vdp_current'] = 6.0
        assert client.patch('/profiles/alice', json={'measurement': section}).status_code == 422

    def test_a_save_that_fails_is_reported(self, client, config, monkeypatch):
        from resistamet_gui.config import ConfigSaveError

        def fail():
            raise ConfigSaveError("disk full")

        monkeypatch.setattr(config, 'save_config', fail)

        response = client.patch('/profiles/alice', json={'measurement': {'nplc': 2.0}})

        assert response.status_code == 500
        assert 'not saved' in response.json()['detail']

    def test_a_bad_interface_name_is_refused(self, client, config):
        response = client.patch('/profiles/alice',
                                 json={'measurement': {'gpib_interface': 'COM5'}})
        assert response.status_code == 422
        assert config.get_gpib_interface() == ''

    def test_address_change_is_refused_during_a_run(self, client, fake_rm):
        client.post('/session/start', json={'mode': 'four_point', 'sample_name': 'w',
                                             'username': 'alice'})
        assert _wait_for(lambda: client.get('/session').json()['state'] == 'running')

        response = client.patch('/profiles/alice',
                                 json={'measurement': {'gpib_address': 'GPIB0::9::INSTR'}})
        assert response.status_code == 409
        client.post('/session/stop')

    def test_other_changes_are_allowed_during_a_run(self, client, fake_rm):
        client.post('/session/start', json={'mode': 'four_point', 'sample_name': 'w',
                                             'username': 'alice'})
        assert _wait_for(lambda: client.get('/session').json()['state'] == 'running')

        response = client.patch('/profiles/alice', json={'display': {'enable_plot': False}})
        assert response.status_code == 200
        client.post('/session/stop')


class TestTouchSafetyKeysNeedTheUiRole:
    """Only a person at the bench may move or silence the voltage warning."""

    @pytest.fixture
    def agent(self, session, config):
        app = create_app(session, token=TOKEN, role='mcp', config=config)
        with TestClient(app) as test_client:
            test_client.headers.update({'Authorization': f'Bearer {TOKEN}'})
            yield test_client

    @pytest.mark.parametrize('patch', [{'safety_voltage_warn_silenced': True},
                                        {'safety_voltage_warn_v': 150.0}])
    def test_another_role_is_refused(self, agent, config, patch):
        before = copy.deepcopy(config.config['user_settings']['alice'])

        response = agent.patch('/profiles/alice', json={'measurement': patch})

        assert response.status_code == 403
        assert config.config['user_settings']['alice'] == before

    def test_another_role_may_resend_them_unchanged(self, agent):
        section = agent.get('/profiles/alice').json()['measurement']
        section['nplc'] = 2.0
        section.pop('gpib_address')

        response = agent.patch('/profiles/alice', json={'measurement': section})

        assert response.status_code == 200
        assert response.json()['measurement']['nplc'] == 2.0

    def test_the_ui_role_may_change_them(self, client):
        response = client.patch('/profiles/alice',
                                 json={'measurement': {'safety_voltage_warn_silenced': True}})
        assert response.status_code == 200
        assert response.json()['measurement']['safety_voltage_warn_silenced'] is True


class TestSchema:
    def test_every_mode_is_described(self, client):
        modes = client.get('/schema/settings').json()['modes']
        assert set(modes) == {'resistance', 'source_v', 'source_i', 'four_point',
                               'sweep', 'vdp'}

    def test_override_keys_exclude_profile_owned_ones(self, client):
        keys = client.get('/schema/settings').json()['modes']['resistance']['override_keys']
        assert 'res_test_current' in keys
        assert 'gpib_address' not in keys
        assert 'settling_time' not in keys


class TestResolve:
    def test_preview_returns_the_settings_a_run_would_use(self, client):
        response = client.post('/settings/resolve', json={
            'mode': 'resistance', 'username': 'alice',
            'overrides': {'res_test_current': 1e-3},
        })
        body = response.json()
        assert body['ok'] is True
        assert body['settings']['measurement']['res_test_current'] == 1e-3
        assert body['derived']['max_rate_hz'] > 0

    def test_issues_are_reported_without_starting_anything(self, client, session):
        response = client.post('/settings/resolve', json={
            'mode': 'vdp', 'username': 'alice', 'overrides': {'vdp_thickness_cm': 0.0},
        })
        body = response.json()
        assert body['ok'] is False
        assert body['issues'][0]['key'] == 'vdp_thickness_cm'
        assert session.state == 'idle'

    def test_hazard_is_reported(self, client):
        body = client.post('/settings/resolve', json={
            'mode': 'source_v', 'username': 'alice',
            'overrides': {'vsource_voltage': 60.0},
        }).json()
        assert body['hazard']['hazardous'] is True
        assert body['hazard']['voltage_v'] == 60.0

    def test_unknown_mode_is_unprocessable(self, client):
        assert client.post('/settings/resolve',
                            json={'mode': 'hall', 'username': 'alice'}).status_code == 422


class TestInstruments:
    def test_resources_are_listed_when_idle(self, client, fake_rm):
        response = client.get('/instruments/resources')
        assert response.status_code == 200
        assert isinstance(response.json()['resources'], list)

    def test_resources_are_refused_during_a_run(self, client, fake_rm):
        client.post('/session/start', json={'mode': 'four_point', 'sample_name': 'w',
                                             'username': 'alice'})
        assert _wait_for(lambda: client.get('/session').json()['state'] == 'running')
        assert client.get('/instruments/resources').status_code == 409
        client.post('/session/stop')

    def test_identify_returns_the_model(self, client, fake_rm):
        response = client.post('/instruments/identify',
                                json={'address': 'GPIB0::24::INSTR'})
        assert response.status_code == 200
        assert response.json()['model']

    def test_resources_say_which_backend_answered(self, client, fake_rm):
        body = client.get('/instruments/resources').json()
        assert body['backend']['requested'] == ''
        assert body['backend']['kind'] == 'unknown'  # the fake has no visalib

    def test_resources_can_try_a_backend_before_saving_it(self, client, fake_rm, monkeypatch):
        import pyvisa
        calls = []
        factory = pyvisa.ResourceManager

        def recording(*args, **kwargs):
            calls.append(args)
            return factory(*args, **kwargs)

        monkeypatch.setattr(pyvisa, 'ResourceManager', recording)
        body = client.get('/instruments/resources', params={'visa_library': '@py'}).json()
        assert calls == [('@py',)]
        assert body['backend']['requested'] == '@py'

    def test_resources_use_the_machine_backend_by_default(self, client, config, fake_rm, monkeypatch):
        import pyvisa
        config.set_machine_local('visa_library', '@ivi')
        calls = []
        factory = pyvisa.ResourceManager
        monkeypatch.setattr(pyvisa, 'ResourceManager',
                            lambda *a, **k: (calls.append(a), factory(*a, **k))[1])
        client.get('/instruments/resources')
        assert calls == [('@ivi',)]

    def test_identify_can_name_a_backend(self, client, fake_rm, monkeypatch):
        import pyvisa
        calls = []
        factory = pyvisa.ResourceManager
        monkeypatch.setattr(pyvisa, 'ResourceManager',
                            lambda *a, **k: (calls.append(a), factory(*a, **k))[1])
        response = client.post('/instruments/identify',
                                json={'address': 'GPIB0::24::INSTR', 'visa_library': '@py'})
        assert response.status_code == 200
        assert calls == [('@py',)]

    def test_resources_can_try_an_interface_before_saving_it(self, client, config, fake_rm):
        opened = []
        fake_rm.open_resource = lambda name, **k: (opened.append(name), object())[1]
        body = client.get('/instruments/resources',
                          params={'gpib_interface': PRLGX}).json()
        assert opened == [PRLGX]
        assert body['gpib_interface'] == PRLGX
        assert config.get_gpib_interface() == ''

    def test_resources_use_the_machine_interface_by_default(self, client, config, fake_rm):
        config.set_machine_local('gpib_interface', PRLGX)
        opened = []
        fake_rm.open_resource = lambda name, **k: (opened.append(name), object())[1]
        assert client.get('/instruments/resources').json()['gpib_interface'] == PRLGX
        assert opened == [PRLGX]

    def test_an_empty_interface_overrides_the_machine_one(self, client, config, fake_rm):
        config.set_machine_local('gpib_interface', PRLGX)
        opened = []
        fake_rm.open_resource = lambda name, **k: (opened.append(name), object())[1]
        body = client.get('/instruments/resources', params={'gpib_interface': ''}).json()
        assert opened == []
        assert body['gpib_interface'] is None

    def test_an_interface_that_does_not_open_is_named(self, client, fake_rm):
        response = client.get('/instruments/resources', params={'gpib_interface': PRLGX})
        assert response.status_code == 503
        assert response.json()['detail'].startswith(f'Could not open GPIB interface {PRLGX}')

    def test_identify_opens_the_interface_first(self, client, fake_rm):
        opened = []
        opening = fake_rm.open_resource

        def recording(name, **kwargs):
            opened.append(name)
            return object() if name == PRLGX else opening(name, **kwargs)

        fake_rm.open_resource = recording
        response = client.post('/instruments/identify',
                                json={'address': 'GPIB0::24::INSTR', 'gpib_interface': PRLGX})
        assert response.status_code == 200
        assert opened == [PRLGX, 'GPIB0::24::INSTR']

    def test_identify_names_an_interface_that_does_not_open(self, client, fake_rm):
        response = client.post('/instruments/identify',
                                json={'address': 'GPIB0::24::INSTR', 'gpib_interface': PRLGX})
        assert response.status_code == 503
        assert PRLGX in response.json()['detail']

    @pytest.mark.parametrize('library', ['/tmp/evil.dylib', 'C:\\evil.dll', '@sim', 'py'])
    def test_a_request_cannot_name_a_library_to_load(self, client, fake_rm, monkeypatch,
                                                     library):
        """The value goes to ctypes; a request may only pick a known backend."""
        import pyvisa
        calls = []
        factory = pyvisa.ResourceManager
        monkeypatch.setattr(pyvisa, 'ResourceManager',
                            lambda *a, **k: (calls.append(a), factory(*a, **k))[1])

        listed = client.get('/instruments/resources', params={'visa_library': library})
        identified = client.post('/instruments/identify', json={
            'address': 'GPIB0::24::INSTR', 'visa_library': library})

        assert listed.status_code == 422
        assert identified.status_code == 422
        assert calls == []

    def test_a_request_may_repeat_the_library_this_machine_uses(self, client, config, fake_rm,
                                                                tmp_path):
        """The desktop dialog sends back what the profile gave it."""
        library = tmp_path / 'libvisa.so'
        library.touch()
        config.set_machine_local('visa_library', str(library))

        response = client.get('/instruments/resources', params={'visa_library': str(library)})

        assert response.status_code == 200

    def test_a_library_path_is_stored_when_the_file_exists(self, client, config, tmp_path):
        library = tmp_path / 'libvisa.so'
        library.touch()

        response = client.patch('/profiles/alice',
                                 json={'measurement': {'visa_library': str(library)}})

        assert response.status_code == 200
        assert config.get_visa_library() == str(library)

    def test_a_library_path_that_is_not_a_file_is_refused(self, client, config, tmp_path):
        response = client.patch('/profiles/alice', json={'measurement': {
            'visa_library': str(tmp_path / 'no-such-libvisa.so')}})

        assert response.status_code == 422
        assert response.json()['detail']['issues'][0]['key'] == 'visa_library'
        assert config.get_visa_library() == ''

    def test_a_library_path_needs_the_ui_role(self, session, config, tmp_path):
        library = tmp_path / 'libvisa.so'
        library.touch()
        app = create_app(session, token=TOKEN, role='mcp', config=config)
        with TestClient(app) as agent:
            agent.headers.update({'Authorization': f'Bearer {TOKEN}'})
            path = agent.patch('/profiles/alice',
                               json={'measurement': {'visa_library': str(library)}})
            named = agent.patch('/profiles/alice', json={'measurement': {'visa_library': '@py'}})

        assert path.status_code == 403
        assert named.status_code == 200
        assert config.get_visa_library() == '@py'

    def test_the_interface_is_machine_local(self, client, config, fake_rm):
        client.patch('/profiles/alice', json={'measurement': {'gpib_interface': PRLGX}})
        assert config.get_gpib_interface() == PRLGX
        assert client.get('/profiles/alice').json()['measurement']['gpib_interface'] == PRLGX

    def test_the_interface_cannot_change_during_a_run(self, client, fake_rm):
        client.post('/session/start', json={'mode': 'four_point', 'sample_name': 'w',
                                             'username': 'alice'})
        assert _wait_for(lambda: client.get('/session').json()['state'] == 'running')
        response = client.patch('/profiles/alice', json={'measurement': {'gpib_interface': PRLGX}})
        assert response.status_code == 409
        client.post('/session/stop')

    def test_identify_is_refused_during_a_run(self, client, fake_rm):
        client.post('/session/start', json={'mode': 'four_point', 'sample_name': 'w',
                                             'username': 'alice'})
        assert _wait_for(lambda: client.get('/session').json()['state'] == 'running')
        assert client.post('/instruments/identify',
                            json={'address': 'GPIB0::24::INSTR'}).status_code == 409
        client.post('/session/stop')


class TestJsonSafety:
    """NaN is not JSON; the wire uses null, as the event contract does."""

    def test_unmeasured_temperature_serializes_as_null(self, client):
        body = client.post('/settings/resolve', json={
            'mode': 'four_point', 'username': 'alice',
        }).json()
        assert body['settings']['measurement']['fpp_temperature_c'] is None

    def test_raw_body_contains_no_nan_token(self, client):
        raw = client.post('/settings/resolve', json={
            'mode': 'four_point', 'username': 'alice',
        }).text
        assert 'NaN' not in raw


class TestAddUser:
    def test_new_user_is_created_and_selected(self, client):
        response = client.post('/users', json={'username': 'bob'})
        assert response.status_code == 201
        body = response.json()
        assert 'bob' in body['users']
        assert body['last_user'] == 'bob'
        assert client.get('/profiles/bob').status_code == 200

    def test_existing_user_is_just_selected(self, client):
        before = client.get('/users').json()['users']
        response = client.post('/users', json={'username': 'alice'})
        assert response.status_code == 201
        assert response.json()['users'] == before

    def test_blank_name_is_rejected(self, client):
        assert client.post('/users', json={'username': '   '}).status_code == 422

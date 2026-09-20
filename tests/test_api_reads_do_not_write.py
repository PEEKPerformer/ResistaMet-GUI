"""Reading through the API must not write the configuration.

A ConfigManager migrates an old config when it opens one, and the app factory
builds one lazily from ``./config.json`` when it is not handed any. Together
that let a read-only listing rewrite whatever config.json the process happened
to be started beside.
"""
import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from resistamet_gui.api import create_app
from resistamet_gui.session.emitter import ListSink
from resistamet_gui.session.manager import MeasurementSession

TOKEN = 'test-token'

#: A config from before 1.13: no ``migrations`` list, so opening it the
#: ordinary way runs the output reset and saves.
OLD_CONFIG = {
    'users': ['alice'], 'last_user': 'alice',
    'measurement': {'sampling_rate': 3.0},
    'user_settings': {'alice': {'output': {'format': 'hdf5'},
                                'measurement': {'nplc': 2.0}}},
}

_PATH_VALUES = {'username': 'alice', 'map_id': 'wafer7'}
_QUERY = {'user': 'alice', 'path': 'alice/nothing.csv', 'mode': 'resistance'}


@pytest.fixture
def beside_a_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'config.json').write_text(json.dumps(OLD_CONFIG))
    return tmp_path


def _get_everything(app):
    called = []
    with TestClient(app) as client:
        client.headers.update({'Authorization': f'Bearer {TOKEN}'})
        for template, operations in app.openapi()['paths'].items():
            if 'get' not in operations:
                continue
            path = template
            for name, value in _PATH_VALUES.items():
                path = path.replace('{%s}' % name, value)
            assert '{' not in path, f"no value for a parameter of {template}"
            for query in ({}, _QUERY):
                client.get(path, params=query)
            called.append(template)
    return called


@pytest.mark.parametrize('with_provider', [True, False])
def test_no_get_route_changes_the_config_it_was_started_beside(beside_a_config, fake_rm,
                                                               with_provider):
    before = (beside_a_config / 'config.json').read_bytes()
    session = MeasurementSession(ListSink())
    provider = (lambda user: {'file': {'data_directory': str(beside_a_config / 'data')}}) \
        if with_provider else None
    try:
        called = _get_everything(create_app(session, token=TOKEN, profile_provider=provider))
    finally:
        session.close(timeout=5.0)

    assert {'/results', '/results/directory', '/users', '/profiles/{username}',
            '/maps', '/session'} <= set(called)
    assert (beside_a_config / 'config.json').read_bytes() == before
    assert sorted(p.name for p in beside_a_config.iterdir()) == ['config.json']


def test_a_listing_with_a_profile_provider_never_opens_the_ambient_config(beside_a_config):
    session = MeasurementSession(ListSink())
    app = create_app(session, token=TOKEN, profile_provider=lambda user: {
        'file': {'data_directory': str(beside_a_config / 'data')}})
    try:
        with TestClient(app) as client:
            client.headers.update({'Authorization': f'Bearer {TOKEN}'})
            assert client.get('/results', params={'user': 'alice'}).status_code == 200
            assert client.get('/results').status_code == 200
            assert client.get('/results/directory').status_code == 200
    finally:
        session.close(timeout=5.0)

    assert app.state.api._config is None


def test_the_ambient_config_still_runs_on_migrated_settings_and_saves_on_a_write(
        beside_a_config):
    """Nothing is written on open, but the stale Output choice must not reach a
    run, and the first deliberate write carries the migration with it."""
    session = MeasurementSession(ListSink())
    app = create_app(session, token=TOKEN)
    try:
        with TestClient(app) as client:
            client.headers.update({'Authorization': f'Bearer {TOKEN}'})
            profile = client.get('/profiles/alice').json()
            assert profile['output']['format'] == 'csv'
            assert profile['measurement']['nplc'] == 2.0
            assert json.loads((beside_a_config / 'config.json').read_text()) == OLD_CONFIG

            assert client.patch('/profiles/alice',
                                json={'measurement': {'nplc': 5.0}}).status_code == 200
    finally:
        session.close(timeout=5.0)

    saved = json.loads((beside_a_config / 'config.json').read_text())
    assert saved['user_settings']['alice'] == {'measurement': {'nplc': 5.0}}
    assert 'output_reset_1_13' in saved['migrations']

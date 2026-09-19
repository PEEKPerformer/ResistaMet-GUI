"""The map routes: assembled from the operator's run files, read-only."""
import copy

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from resistamet_gui.api import create_app
from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.session.emitter import ListSink
from resistamet_gui.session.manager import MeasurementSession

from .test_session_spot_map import _write_run

TOKEN = 'test-token'


@pytest.fixture
def data_root(tmp_path):
    root = tmp_path / 'measurement_data'
    (root / 'alice').mkdir(parents=True)
    (root / 'bob').mkdir()
    _write_run(root / 'alice', 1, 'wafer7', 0, 100.0, label='centre')
    _write_run(root / 'alice', 2, 'wafer7', 1, 999.0)
    _write_run(root / 'alice', 3, 'wafer7', 1, 120.0, label='north again')
    _write_run(root / 'alice', 4, 'wafer8', 0, 500.0)
    _write_run(root / 'bob', 5, 'bobs-map', 0, 42.0)
    (tmp_path / 'secret_map.json').write_text('{"contents": "do-not-serve"}')
    return root


@pytest.fixture
def app(data_root):
    def profile_provider(username):
        profile = copy.deepcopy(DEFAULT_SETTINGS)
        profile['file']['data_directory'] = str(data_root)
        return profile

    session = MeasurementSession(ListSink())
    yield create_app(session, token=TOKEN, profile_provider=profile_provider)
    session.close(timeout=2.0)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        test_client.headers.update({'Authorization': f'Bearer {TOKEN}'})
        yield test_client


class TestAuth:
    def test_both_routes_need_the_token(self, app):
        with TestClient(app) as anonymous:
            assert anonymous.get('/maps?user=alice').status_code == 401
            assert anonymous.get('/maps/wafer7?user=alice').status_code == 401
            wrong = {'Authorization': 'Bearer nope'}
            assert anonymous.get('/maps/wafer7?user=alice', headers=wrong).status_code == 401


class TestListing:
    def test_lists_the_operators_map_ids(self, client):
        assert client.get('/maps?user=alice').json() == {
            'user': 'alice', 'maps': ['wafer7', 'wafer8']}

    def test_another_operators_maps_are_their_own(self, client):
        assert client.get('/maps?user=bob').json()['maps'] == ['bobs-map']

    def test_an_operator_without_runs_has_no_maps(self, client):
        assert client.get('/maps?user=carol').json()['maps'] == []

    def test_the_operator_is_required(self, client):
        assert client.get('/maps').status_code == 422

    def test_a_username_cannot_leave_the_data_directory(self, client):
        """Sanitized exactly as the run's own directory name was."""
        assert client.get('/maps?user=../alice').json()['maps'] == ['wafer7', 'wafer8']
        assert client.get('/maps?user=..').json()['maps'] == []


class TestReading:
    def test_returns_the_assembled_map(self, client):
        body = client.get('/maps/wafer7?user=alice').json()
        assert body['map_id'] == 'wafer7'
        assert [spot['index'] for spot in body['spots']] == [0, 1]
        assert body['spots'][1]['label'] == 'north again'
        assert body['spots'][1]['superseded'] == ['1002_wafer7_4PP_1mA.csv']
        assert body['rs']['mean'] == 110.0
        assert body['rs']['n'] == 2
        # NaN in a footer reaches the client as null, like every other route.
        assert body['spots'][0]['stats']['sigma']['mean'] is None

    def test_matches_the_contract_model(self, client):
        from resistamet_gui.session.spot_map import SpotMap
        SpotMap.model_validate(client.get('/maps/wafer7?user=alice').json())

    def test_an_unknown_map_is_not_found(self, client):
        assert client.get('/maps/wafer9?user=alice').status_code == 404
        assert client.get('/maps/wafer7?user=bob').status_code == 404

    @pytest.mark.parametrize("map_id", [
        'a.b', 'secret_map.json', '%2E%2E', '..%2Fsecret', '..%5Csecret',
        '%2E%2E%2Fsecret_map', 'x' * 65, 'wafer%207', 'wafer7%00',
    ])
    def test_a_map_id_that_is_not_a_plain_token_is_refused(self, client, map_id):
        response = client.get(f'/maps/{map_id}?user=alice')
        assert response.status_code in (404, 422), response.text
        assert 'do-not-serve' not in response.text

    def test_nothing_is_written_by_reading(self, client, data_root):
        before = sorted(p.name for p in (data_root / 'alice').iterdir())
        client.get('/maps?user=alice')
        client.get('/maps/wafer7?user=alice')
        assert sorted(p.name for p in (data_root / 'alice').iterdir()) == before


class TestARunShowsUpInItsMap:
    def test_a_spot_started_over_the_api_is_served_back(self, app, client, fake_rm, monkeypatch):
        import time
        from resistamet_gui import system_utils
        monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
        monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)

        response = client.post('/session/start', json={
            'mode': 'four_point', 'sample_name': 'wafer7', 'username': 'alice',
            'overrides': {'sampling_rate': 50.0, 'fpp_samples': 3, 'fpp_current': 1e-4},
            'spot': {'map_id': 'wafer9', 'index': 0, 'label': 'centre'},
        })
        assert response.status_code == 202
        deadline = time.time() + 10.0
        while client.get('/session').json()['state'] != 'idle' and time.time() < deadline:
            time.sleep(0.05)

        assert 'wafer9' in client.get('/maps?user=alice').json()['maps']
        body = client.get('/maps/wafer9?user=alice').json()
        assert [spot['label'] for spot in body['spots']] == ['centre']
        assert body['spots'][0]['stats']['n'] == 3

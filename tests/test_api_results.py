"""Listing and reading run output, without escaping the data directory."""
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from resistamet_gui.api import create_app
from resistamet_gui.config import ConfigManager
from resistamet_gui.session.emitter import ListSink
from resistamet_gui.session.manager import MeasurementSession

TOKEN = 'test-token'


@pytest.fixture
def data_dir(tmp_path):
    root = tmp_path / 'measurement_data'
    (root / 'alice').mkdir(parents=True)
    (root / 'bob').mkdir()
    (root / 'alice' / '100_wafer_R_1.csv').write_text("# resistamet_format_version: 2.0\nelapsed_s,R_ohm\n0.1,100\n")
    (root / 'bob' / '200_film_4PP_1.csv').write_text("elapsed_s\n0\n")
    (root / 'bob' / 'notes.txt').write_text("not a result")
    (tmp_path / 'config.json').write_text('{"file": {"data_directory": "%s"}}' % str(root).replace('\\', '\\\\'))
    return root


@pytest.fixture
def client(data_dir, tmp_path):
    config = ConfigManager(config_file=str(tmp_path / 'config.json'))
    session = MeasurementSession(ListSink())
    app = create_app(session, token=TOKEN, config=config)
    with TestClient(app) as test_client:
        test_client.headers.update({'Authorization': f'Bearer {TOKEN}'})
        yield test_client
    session.close(timeout=2.0)


class TestListing:
    def test_lists_result_files_only(self, client):
        files = client.get('/results').json()['files']
        names = sorted(f['name'] for f in files)
        assert names == ['100_wafer_R_1.csv', '200_film_4PP_1.csv']

    def test_map_summaries_are_not_runs(self, client, data_dir):
        """<map_id>_map.json sits beside the runs and is served by /maps."""
        (data_dir / 'bob' / 'wafer7_map.json').write_text('{"map_id": "wafer7"}')
        (data_dir / 'bob' / '300_film_R_1.json').write_text('{"format_version": "1.0"}')
        names = sorted(f['name'] for f in client.get('/results').json()['files'])
        # The legacy pair's .json is still a result; the map summary is not.
        assert names == ['100_wafer_R_1.csv', '200_film_4PP_1.csv', '300_film_R_1.json']

    def test_owner_comes_from_the_directory(self, client):
        files = {f['name']: f for f in client.get('/results').json()['files']}
        assert files['100_wafer_R_1.csv']['user'] == 'alice'
        assert files['100_wafer_R_1.csv']['path'] == 'alice/100_wafer_R_1.csv'

    def test_filter_by_user(self, client):
        files = client.get('/results?user=bob').json()['files']
        assert [f['user'] for f in files] == ['bob']

    def test_missing_directory_is_empty_not_an_error(self, tmp_path):
        (tmp_path / 'config.json').write_text('{"file": {"data_directory": "%s"}}' % str(tmp_path / 'nowhere'))
        config = ConfigManager(config_file=str(tmp_path / 'config.json'))
        session = MeasurementSession(ListSink())
        with TestClient(create_app(session, token=TOKEN, config=config)) as client:
            client.headers.update({'Authorization': f'Bearer {TOKEN}'})
            assert client.get('/results').json()['files'] == []


class TestReading:
    def test_reads_a_csv(self, client):
        response = client.get('/results/file?path=alice/100_wafer_R_1.csv')
        assert response.status_code == 200
        assert 'elapsed_s,R_ohm' in response.text

    def test_refuses_to_leave_the_data_directory(self, client):
        response = client.get('/results/file?path=../config.json')
        assert response.status_code == 400

    def test_unknown_file_is_404(self, client):
        assert client.get('/results/file?path=alice/nope.csv').status_code == 404

    def test_non_csv_is_refused(self, client, data_dir):
        (data_dir / 'alice' / 'run.h5').write_bytes(b'\x89HDF')
        assert client.get('/results/file?path=alice/run.h5').status_code == 415

    def test_directory_is_reported(self, client, data_dir):
        body = client.get('/results/directory').json()
        assert body['root'] == str(data_dir.resolve())
        assert body['exists'] is True


class TestTheOperatorsOwnDirectory:
    """Runs are written under the operator's profile directory, as /maps reads them."""

    @pytest.fixture
    def custom(self, tmp_path, data_dir):
        """Anna Lee keeps her data somewhere else; her folder is ``Anna_Lee``."""
        config = ConfigManager(config_file=str(tmp_path / 'config.json'))
        config.add_user('Anna Lee')
        config.add_user('alice')
        elsewhere = tmp_path / 'annas_data'
        (elsewhere / 'Anna_Lee').mkdir(parents=True)
        (elsewhere / 'Anna_Lee' / '300_chip_R_1.csv').write_text("elapsed_s,R_ohm\n0.1,5\n")
        config.update_user_settings('Anna Lee', {'file': {'data_directory': str(elsewhere)}})
        session = MeasurementSession(ListSink())
        app = create_app(session, token=TOKEN, config=config)
        with TestClient(app) as test_client:
            test_client.headers.update({'Authorization': f'Bearer {TOKEN}'})
            yield test_client, elsewhere
        session.close(timeout=2.0)

    def test_only_mine_works_for_a_name_with_a_space(self, custom):
        client, elsewhere = custom
        body = client.get('/results', params={'user': 'Anna Lee'}).json()

        assert [f['name'] for f in body['files']] == ['300_chip_R_1.csv']
        assert body['files'][0]['user'] == 'Anna_Lee'
        assert body['root'] == str(elsewhere.resolve())

    def test_a_file_in_a_custom_directory_can_be_read(self, custom):
        client, _ = custom
        listed = client.get('/results', params={'user': 'Anna Lee'}).json()['files'][0]

        response = client.get('/results/file', params={'path': listed['path']})

        assert response.status_code == 200
        assert 'elapsed_s,R_ohm' in response.text

    def test_everyone_lists_every_operators_directory(self, custom):
        client, _ = custom
        names = sorted(f['name'] for f in client.get('/results').json()['files'])
        assert names == ['100_wafer_R_1.csv', '200_film_4PP_1.csv', '300_chip_R_1.csv']

    def test_another_users_filter_does_not_show_her_files(self, custom):
        client, _ = custom
        files = client.get('/results', params={'user': 'alice'}).json()['files']
        assert [f['name'] for f in files] == ['100_wafer_R_1.csv']

    def test_the_directory_is_the_operators(self, custom):
        client, elsewhere = custom
        body = client.get('/results/directory', params={'user': 'Anna Lee'}).json()
        assert body['root'] == str(elsewhere.resolve())

    def test_escaping_every_directory_is_still_refused(self, custom):
        client, _ = custom
        assert client.get('/results/file', params={'path': '../config.json'}).status_code == 400

"""The map's photograph: stored beside the runs, never silently replaced."""
import copy
import hashlib
import json
import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from resistamet_gui.api import create_app
from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.session import spot_map
from resistamet_gui.session.emitter import ListSink
from resistamet_gui.session.manager import MeasurementSession
from resistamet_gui.session.spot_map import SpotMap

from .test_session_spot_map import _write_run

TOKEN = 'test-token'

PNG = b'\x89PNG\r\n\x1a\n' + b'first picture'
OTHER_PNG = b'\x89PNG\r\n\x1a\n' + b'second picture'
JPEG = b'\xff\xd8\xff\xe0' + b'a jpeg'
WEBP = b'RIFF\x10\x00\x00\x00WEBP' + b'a webp'
TIFF = b'II*\x00' + b'a tiff'


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _registration(data, **changes):
    body = {'sha256': _sha(data), 'image_width_px': 4000, 'image_height_px': 3000,
            'mm_per_px': 0.0125, 'centre_x_mm': 1.5, 'centre_y_mm': -0.25,
            'rotation_deg': 12.0, 'calibrated': True}
    body.update(changes)
    return body


@pytest.fixture
def data_root(tmp_path):
    root = tmp_path / 'measurement_data'
    (root / 'alice').mkdir(parents=True)
    _write_run(root / 'alice', 1, 'wafer7', 0, 100.0, label='centre')
    (tmp_path / 'secret.png').write_bytes(PNG + b' do-not-serve')
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


def _put(client, data=PNG, content_type='image/png', map_id='wafer7', user='alice', **query):
    return client.put(f'/maps/{map_id}/image', params={'user': user, **query}, content=data,
                      headers={'Content-Type': content_type})


class TestAuth:
    def test_every_route_needs_the_token(self, app):
        with TestClient(app) as anonymous:
            assert anonymous.put('/maps/wafer7/image?user=alice', content=PNG,
                                 headers={'Content-Type': 'image/png'}).status_code == 401
            assert anonymous.get('/maps/wafer7/image?user=alice').status_code == 401
            assert anonymous.put('/maps/wafer7/registration?user=alice',
                                 json=_registration(PNG)).status_code == 401


class TestStoringAnImage:
    def test_round_trip(self, client, data_root):
        stored = _put(client)
        assert stored.status_code == 201
        assert stored.json() == {'file': 'wafer7_sample.png', 'sha256': _sha(PNG),
                                 'bytes': len(PNG), 'replaced': None}
        assert (data_root / 'alice' / 'wafer7_sample.png').read_bytes() == PNG
        served = client.get('/maps/wafer7/image?user=alice')
        assert served.status_code == 200
        assert served.headers['content-type'] == 'image/png'
        assert served.content == PNG

    @pytest.mark.parametrize('data, content_type, name', [
        (JPEG, 'image/jpeg', 'wafer7_sample.jpg'),
        (WEBP, 'image/webp', 'wafer7_sample.webp'),
        (TIFF, 'image/tiff', 'wafer7_sample.tif'),
    ])
    def test_each_type_is_served_back_as_what_it_is(self, client, data, content_type, name):
        assert _put(client, data, content_type).json()['file'] == name
        served = client.get('/maps/wafer7/image?user=alice')
        assert served.headers['content-type'] == content_type
        assert served.content == data

    def test_the_same_image_again_changes_nothing(self, client, data_root):
        _put(client)
        path = data_root / 'alice' / 'wafer7_sample.png'
        before = path.stat().st_mtime_ns
        again = _put(client)
        assert again.status_code == 200
        assert again.json()['sha256'] == _sha(PNG)
        assert path.stat().st_mtime_ns == before
        assert not list((data_root / 'alice').glob('*replaced*'))

    def test_a_different_image_is_refused(self, client, data_root):
        _put(client)
        assert _put(client, OTHER_PNG).status_code == 409
        assert _put(client, JPEG, 'image/jpeg').status_code == 409
        assert (data_root / 'alice' / 'wafer7_sample.png').read_bytes() == PNG
        assert not (data_root / 'alice' / 'wafer7_sample.jpg').exists()

    def test_replace_keeps_the_old_file(self, client, data_root):
        _put(client)
        replaced = _put(client, OTHER_PNG, replace='true')
        assert replaced.status_code == 201
        old_name = replaced.json()['replaced']
        assert old_name.startswith('wafer7_sample.png.replaced-')
        assert (data_root / 'alice' / old_name).read_bytes() == PNG
        assert (data_root / 'alice' / 'wafer7_sample.png').read_bytes() == OTHER_PNG

    def test_replace_with_another_type_leaves_one_live_image(self, client, data_root):
        _put(client)
        replaced = _put(client, JPEG, 'image/jpeg', replace='true')
        assert replaced.json()['file'] == 'wafer7_sample.jpg'
        assert not (data_root / 'alice' / 'wafer7_sample.png').exists()
        assert (data_root / 'alice' / replaced.json()['replaced']).read_bytes() == PNG
        assert client.get('/maps/wafer7/image?user=alice').content == JPEG

    def test_two_replacements_in_one_second_keep_both_old_files(self, client, data_root):
        _put(client)
        first = _put(client, OTHER_PNG, replace='true').json()['replaced']
        second = _put(client, PNG, replace='true').json()['replaced']
        assert first != second
        assert (data_root / 'alice' / first).read_bytes() == PNG
        assert (data_root / 'alice' / second).read_bytes() == OTHER_PNG

    def test_bytes_that_are_not_the_declared_type(self, client, data_root):
        assert _put(client, JPEG, 'image/png').status_code == 415
        assert _put(client, b'<svg/>', 'image/png').status_code == 415
        assert _put(client, b'', 'image/png').status_code == 415
        assert not list((data_root / 'alice').glob('wafer7_sample*'))

    def test_a_type_that_is_not_an_image_type_we_keep(self, client):
        assert _put(client, b'<svg/>', 'image/svg+xml').status_code == 415
        assert _put(client, PNG, 'application/octet-stream').status_code == 415

    def test_oversize(self, client, data_root, monkeypatch):
        from resistamet_gui.api import routes_maps
        monkeypatch.setattr(routes_maps, 'MAX_IMAGE_BYTES', 32)
        assert _put(client, PNG + b'x' * 64).status_code == 413
        assert not list((data_root / 'alice').glob('wafer7_sample*'))

    def test_the_limit_is_25_mb(self):
        assert spot_map.MAX_IMAGE_BYTES == 25 * 1024 * 1024
        with pytest.raises(spot_map.ImageTooLarge):
            spot_map.store_map_image('/nonexistent', 'wafer7',
                                     PNG + bytes(spot_map.MAX_IMAGE_BYTES), 'image/png')

    def test_an_image_may_come_before_the_first_spot(self, client, data_root):
        assert _put(client, map_id='fresh-map', user='carol').status_code == 201
        assert (data_root / 'carol' / 'fresh-map_sample.png').read_bytes() == PNG
        found = client.get('/maps/fresh-map?user=carol')
        assert found.status_code == 200
        assert found.json()['spots'] == []
        assert found.json()['image']['sha256'] == _sha(PNG)

    def test_no_temporary_file_is_left(self, client, data_root):
        _put(client)
        client.put('/maps/wafer7/registration?user=alice', json=_registration(PNG))
        assert not [p.name for p in (data_root / 'alice').iterdir() if p.name.endswith('.tmp')]


class TestTraversal:
    @pytest.mark.parametrize('map_id', ['..', '.', 'a.b', '..%2Falice', '%2Fetc%2Fpasswd',
                                        'wafer7%5Cx', 'wafer%207', 'x' * 65])
    def test_a_map_id_that_is_not_a_plain_token(self, client, data_root, map_id):
        for response in (
            client.put(f'/maps/{map_id}/image?user=alice', content=PNG,
                       headers={'Content-Type': 'image/png'}),
            client.get(f'/maps/{map_id}/image?user=alice'),
            client.put(f'/maps/{map_id}/registration?user=alice', json=_registration(PNG)),
        ):
            assert response.status_code in (404, 405, 422)
        written = [p for p in data_root.parent.rglob('*sample*')]
        assert written == []

    def test_a_username_cannot_leave_the_data_directory(self, client, data_root):
        assert _put(client, user='../../outside').status_code == 201
        inside = [p.relative_to(data_root.parent).as_posix()
                  for p in data_root.parent.rglob('wafer7_sample.png')]
        assert len(inside) == 1 and inside[0].startswith('measurement_data/')
        assert _put(client, user='/etc').status_code == 201
        assert all(data_root in p.parents for p in data_root.parent.rglob('wafer7_sample.png'))

    @pytest.mark.skipif(not hasattr(os, 'symlink'), reason='needs symlinks')
    def test_a_link_out_of_the_directory_is_not_served_or_written_through(
            self, client, data_root, tmp_path):
        secret = tmp_path / 'secret.png'
        link = data_root / 'alice' / 'evil_sample.png'
        try:
            link.symlink_to(secret)
        except OSError:
            pytest.skip('cannot create symlinks here')
        assert client.get('/maps/evil/image?user=alice').status_code == 404
        assert client.get('/maps/evil?user=alice').status_code == 404
        before = secret.read_bytes()
        assert _put(client, OTHER_PNG, map_id='evil').status_code == 409
        assert _put(client, OTHER_PNG, map_id='evil', replace='true').status_code == 409
        assert secret.read_bytes() == before
        assert link.is_symlink()


class TestRegistration:
    def test_stored_in_the_sidecar_and_returned(self, client, data_root):
        _put(client)
        stored = client.put('/maps/wafer7/registration?user=alice', json=_registration(PNG))
        assert stored.status_code == 200
        assert stored.json()['registration']['mm_per_px'] == 0.0125
        sidecar = json.loads((data_root / 'alice' / 'wafer7_map_image.json').read_text())
        assert sidecar['file'] == 'wafer7_sample.png'
        assert sidecar['sha256'] == _sha(PNG)
        assert sidecar['registration']['rotation_deg'] == 12.0

    def test_two_point_calibration(self, client):
        _put(client)
        calibration = {'x1_px': 10, 'y1_px': 20, 'x2_px': 810, 'y2_px': 20, 'distance_mm': 10.0}
        stored = client.put('/maps/wafer7/registration?user=alice',
                            json=_registration(PNG, calibration=calibration))
        assert stored.json()['registration']['calibration']['distance_mm'] == 10.0

    def test_needs_an_image(self, client):
        assert client.put('/maps/wafer7/registration?user=alice',
                          json=_registration(PNG)).status_code == 404

    def test_for_another_image_is_refused(self, client):
        _put(client)
        assert client.put('/maps/wafer7/registration?user=alice',
                          json=_registration(OTHER_PNG)).status_code == 409

    @pytest.mark.parametrize('changes', [
        {'mm_per_px': 0}, {'mm_per_px': -1}, {'mm_per_px': 1e9},
        {'image_width_px': 0}, {'image_height_px': 10 ** 7}, {'rotation_deg': 720},
        {'centre_x_mm': 1e9}, {'sha256': 'abc'}, {'unknown': 1},
        {'calibration': {'x1_px': 1, 'y1_px': 1, 'x2_px': 1, 'y2_px': 1, 'distance_mm': 5}},
        {'calibration': {'x1_px': 1, 'y1_px': 1, 'x2_px': 9, 'y2_px': 1, 'distance_mm': 0}},
    ])
    def test_numbers_out_of_bounds(self, client, changes):
        _put(client)
        assert client.put('/maps/wafer7/registration?user=alice',
                          json=_registration(PNG, **changes)).status_code == 422

    @pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity'])
    def test_numbers_that_are_not_finite(self, client, value):
        _put(client)
        body = json.dumps(_registration(PNG)).replace('0.0125', value)
        response = client.put('/maps/wafer7/registration?user=alice', content=body,
                              headers={'Content-Type': 'application/json'})
        assert response.status_code == 422

    def test_a_new_image_starts_without_the_old_registration(self, client):
        _put(client)
        client.put('/maps/wafer7/registration?user=alice', json=_registration(PNG))
        _put(client, OTHER_PNG, replace='true')
        assert client.get('/maps/wafer7?user=alice').json()['image']['registration'] is None


class TestTheMapCarriesTheImage:
    def test_no_image_no_block(self, client):
        assert client.get('/maps/wafer7?user=alice').json()['image'] is None

    def test_image_block(self, client):
        _put(client)
        client.put('/maps/wafer7/registration?user=alice', json=_registration(PNG))
        found = client.get('/maps/wafer7?user=alice').json()
        assert found['image'] == {'file': 'wafer7_sample.png', 'sha256': _sha(PNG),
                                  'bytes': len(PNG),
                                  'registration': _registration(PNG, calibration=None)}
        assert [spot['label'] for spot in found['spots']] == ['centre']
        SpotMap.model_validate(found)

    def test_the_summary_file_names_the_image(self, client, data_root):
        _put(client)
        summary = json.loads((data_root / 'alice' / 'wafer7_map.json').read_text())
        assert summary['image']['sha256'] == _sha(PNG)
        assert [spot['index'] for spot in summary['spots']] == [0]

    def test_a_file_exchanged_by_hand_is_described_as_it_is(self, client, data_root):
        _put(client)
        client.put('/maps/wafer7/registration?user=alice', json=_registration(PNG))
        (data_root / 'alice' / 'wafer7_sample.png').write_bytes(OTHER_PNG)
        image = client.get('/maps/wafer7?user=alice').json()['image']
        assert image['sha256'] == _sha(OTHER_PNG)
        assert image['registration'] is None

    def test_another_operators_map_has_no_image(self, client):
        _put(client)
        assert client.get('/maps/wafer7/image?user=bob').status_code == 404

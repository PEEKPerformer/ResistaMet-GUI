"""POST /spots/preflight: what a run would record about a spot, with no run.

All on a 20 x 20 mm rectangle with a 1 mm probe spacing, the sample the map
UI was checked on.
"""
import copy
import math
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from resistamet_gui import calculations_geometry as geo
from resistamet_gui.api import create_app
from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.data_export import parse_metadata
from resistamet_gui.schema.resolve import resolve_run_settings
from resistamet_gui.schema.spots import SpotPreflight
from resistamet_gui.session.emitter import ListSink
from resistamet_gui.session.manager import MeasurementSession
from resistamet_gui.session.spot_record import spot_record_from_settings

TOKEN = 'test-token'

SQUARE = {'fpp_sample_shape': 'rectangle', 'fpp_sample_width_mm': 20.0,
          'fpp_sample_length_mm': 20.0, 'fpp_spacing_cm': 0.1}
CENTRE = {'map_id': 'wafer7', 'index': 0, 'label': 'centre', 'x_mm': 0.0, 'y_mm': 0.0}
NEAR_EDGE = {'map_id': 'wafer7', 'index': 1, 'label': 'north', 'x_mm': 0.0, 'y_mm': 7.0}
OFF_SAMPLE = {'map_id': 'wafer7', 'index': 2, 'label': 'gone', 'x_mm': 9.0, 'y_mm': 0.0}

#: The header keys a checked position writes, as the pre-flight names them.
POSITION_KEYS = ('edge_clearance_s', 'factor_here', 'factor_centre', 'factor_rows',
                 'relative_error', 'relative_error_rows')


@pytest.fixture
def data_root(tmp_path):
    return tmp_path / 'measurement_data'


@pytest.fixture
def profile(data_root):
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings['file']['data_directory'] = str(data_root)
    return settings


@pytest.fixture
def sink():
    return ListSink()


@pytest.fixture
def app(profile, sink):
    session = MeasurementSession(sink)
    yield create_app(session, token=TOKEN, profile_provider=lambda username: profile)
    session.close(timeout=5.0)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        test_client.headers.update({'Authorization': f'Bearer {TOKEN}'})
        yield test_client


def _preflight(client, spot=None, overrides=SQUARE, **body):
    request = {'user': 'alice', 'overrides': overrides, **body}
    if spot is not None:
        request['spot'] = spot
    return client.post('/spots/preflight', json=request)


def _record(profile, spot, overrides=SQUARE):
    """What the run computes, asked of ``session/spot_record.py`` directly."""
    resolved = resolve_run_settings(profile, 'four_point', overrides, strict=True)
    assert resolved.ok
    resolved.settings['spot'] = spot
    return spot_record_from_settings(resolved.settings)


class TestAgainstTheRunsOwnFunctions:
    @pytest.mark.parametrize('spot', [CENTRE, NEAR_EDGE, OFF_SAMPLE])
    def test_the_numbers_are_the_headers_numbers(self, client, profile, spot):
        found = _preflight(client, spot).json()
        header = _record(profile, spot).header()
        assert found['checked'] is True
        for key in POSITION_KEYS:
            assert found[key] == header[key], key
        assert found['sample'] == header['sample']
        assert found['angle_deg'] == header['angle_deg']
        assert found['edge_warn_pct'] == header['edge_warn_pct']
        SpotPreflight.model_validate(found)

    def test_centre(self, client):
        found = _preflight(client, CENTRE).json()
        assert found['off_sample'] is False
        assert found['edge_clearance_s'] == pytest.approx(8.5)
        assert found['factor_here'] == found['factor_centre'] == found['geometry_factor']
        assert found['geometry_factor'] == pytest.approx(
            geo.rectangle_factor(20.0, 20.0, 1.0, (0.0, 0.0), 0.0))
        assert found['relative_error'] == 0.0

    def test_centre_of_an_outline_the_rows_do_not_know(self, client):
        """The rows assume an unbounded sheet (no fpp_diameter_cm), so even
        the centre of a 20 s square is off by the 1.8 % the square costs."""
        found = _preflight(client, CENTRE).json()
        assert found['factor_rows'] == pytest.approx(geo.UNBOUNDED_FACTOR, rel=1e-3)
        assert found['relative_error_rows'] == pytest.approx(0.018, abs=0.002)
        assert found['compared_with'] == 'rows'
        assert found['near_edge'] is True
        assert "Spot 'centre'" in found['message']

    def test_centre_when_the_rows_use_the_same_square(self, client):
        overrides = {**SQUARE, 'fpp_geometry': 'square', 'fpp_diameter_cm': 2.0,
                     'fpp_thickness_um': 100.0}
        found = _preflight(client, CENTRE, overrides).json()
        assert abs(found['relative_error_rows']) < 0.001
        assert found['near_edge'] is False
        assert found['message'] is None

    def test_near_an_edge(self, client):
        found = _preflight(client, NEAR_EDGE).json()
        assert found['off_sample'] is False and found['near_edge'] is True
        assert found['edge_clearance_s'] == pytest.approx(3.0)
        assert found['relative_error'] == pytest.approx(0.053, abs=0.002)
        assert found['factor_here'] < found['factor_centre']
        assert '3.0 s from the edge' in found['message']

    def test_off_the_sample(self, client):
        found = _preflight(client, OFF_SAMPLE).json()
        assert found['off_sample'] is True and found['near_edge'] is False
        assert found['edge_clearance_s'] == pytest.approx(-0.5)
        assert found['factor_here'] is None and found['relative_error_rows'] is None
        assert 'off the sample' in found['message']
        # The outline still has a centred factor to show.
        assert found['geometry_factor'] == pytest.approx(
            geo.rectangle_factor(20.0, 20.0, 1.0, (0.0, 0.0), 0.0))


class TestWithoutAPosition:
    def test_a_spot_that_is_only_a_label(self, client):
        found = _preflight(client, {'map_id': 'wafer7', 'index': 0, 'label': 'somewhere'}).json()
        assert found['checked'] is False
        assert found['edge_clearance_s'] is None and found['message'] is None
        assert found['geometry_factor'] == pytest.approx(
            geo.rectangle_factor(20.0, 20.0, 1.0, (0.0, 0.0), 0.0))
        assert found['factor_rows'] == pytest.approx(geo.UNBOUNDED_FACTOR, rel=1e-3)

    def test_no_spot_at_all(self, client):
        found = _preflight(client).json()
        assert found['checked'] is False
        assert found['sample'] == {'shape': 'rectangle', 'diameter_mm': None,
                                   'width_mm': 20.0, 'length_mm': 20.0}
        assert found['spacing_mm'] == pytest.approx(1.0)
        assert found['geometry_factor'] is not None

    def test_the_factor_follows_the_array_angle(self, client):
        along = _preflight(client, overrides={**SQUARE, 'fpp_sample_length_mm': 40.0}).json()
        across = _preflight(client, overrides={**SQUARE, 'fpp_sample_length_mm': 40.0,
                                               'fpp_array_angle_deg': 90.0}).json()
        assert across['angle_deg'] == 90.0
        assert across['geometry_factor'] == pytest.approx(
            geo.rectangle_factor(20.0, 40.0, 1.0, (0.0, 0.0), math.radians(90.0)))
        assert across['geometry_factor'] != pytest.approx(along['geometry_factor'], rel=1e-6)

    def test_an_unbounded_sheet(self, client):
        found = _preflight(client, CENTRE, overrides={}).json()
        assert found['sample']['shape'] == 'unbounded'
        assert found['checked'] is False
        assert found['geometry_factor'] == pytest.approx(math.pi / math.log(2.0))

    def test_a_sample_smaller_than_the_probe(self, client):
        tiny = {**SQUARE, 'fpp_sample_width_mm': 2.0, 'fpp_sample_length_mm': 2.0}
        assert _preflight(client, overrides=tiny).json()['geometry_factor'] is None


class TestRefusals:
    def test_needs_the_token(self, app):
        with TestClient(app) as anonymous:
            assert anonymous.post('/spots/preflight', json={'user': 'alice'}).status_code == 401

    def test_username_is_accepted_as_start_spells_it(self, client):
        response = client.post('/spots/preflight', json={'username': 'alice', 'spot': CENTRE})
        assert response.status_code == 200

    @pytest.mark.parametrize('body', [
        {},
        {'user': ''},
        {'user': 'alice', 'mode': 'resistance'},
        {'user': 'alice', 'spot': {'map_id': '../x', 'index': 0, 'label': 'a'}},
        {'user': 'alice', 'spot': {'map_id': 'm', 'index': 0, 'label': 'a', 'x_mm': 1.0}},
        {'user': 'alice', 'overrides': {'no_such_setting': 1}},
        {'user': 'alice', 'overrides': {'fpp_current': 99.0}},
        {'user': 'alice', 'overrides': {'fpp_sample_shape': 'circle'}},
    ])
    def test_what_start_would_refuse(self, client, body):
        assert client.post('/spots/preflight', json=body).status_code == 422

    def test_not_a_finite_position(self, client):
        body = ('{"user": "alice", "spot": {"map_id": "m", "index": 0, "label": "a", '
                '"x_mm": NaN, "y_mm": 0}}')
        response = client.post('/spots/preflight', content=body,
                               headers={'Content-Type': 'application/json'})
        assert response.status_code == 422


class TestItIsNotARun:
    def test_nothing_is_opened_written_or_emitted(self, client, data_root, sink, monkeypatch):
        import pyvisa

        def no_bus(*args, **kwargs):
            raise AssertionError("a pre-flight must not open the bus")
        monkeypatch.setattr(pyvisa, 'ResourceManager', no_bus)
        for spot in (CENTRE, NEAR_EDGE, OFF_SAMPLE, None):
            assert _preflight(client, spot).status_code == 200
        assert not data_root.exists()
        assert sink.events == []
        assert client.get('/session').json()['state'] == 'idle'


class TestAgainstASimulatedRun:
    @pytest.fixture(autouse=True)
    def _quiet_machine(self, monkeypatch):
        from resistamet_gui import system_utils
        monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
        monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)

    def _run(self, client, sink, spot):
        overrides = {**SQUARE, 'sampling_rate': 50.0, 'fpp_samples': 3, 'fpp_current': 1e-4}
        before = _preflight(client, spot, overrides).json()
        started = client.post('/session/start', json={
            'mode': 'four_point', 'sample_name': 'wafer7', 'username': 'alice',
            'overrides': overrides, 'spot': spot})
        assert started.status_code == 202
        deadline = time.time() + 10.0
        while client.get('/session').json()['state'] != 'idle' and time.time() < deadline:
            time.sleep(0.05)
        return before, [event.payload for event in sink.of_type('geometry_warning')]

    @pytest.mark.parametrize('spot', [CENTRE, NEAR_EDGE])
    def test_the_file_header_holds_the_preflights_numbers(self, client, sink, data_root,
                                                          fake_rm, spot):
        before, warnings = self._run(client, sink, spot)
        (path,) = [p for p in (data_root / 'alice').iterdir() if p.name.endswith('.csv')]
        header = parse_metadata(path)
        for key in POSITION_KEYS:
            assert header[f'spot.{key}'] == pytest.approx(before[key], rel=1e-12), key
        (warning,) = warnings
        assert warning['refused'] is False
        assert warning['message'] == before['message']
        assert warning['compared_with'] == before['compared_with']

    def test_the_refusal_is_the_preflights_message(self, client, sink, data_root, fake_rm):
        before, warnings = self._run(client, sink, OFF_SAMPLE)
        (warning,) = warnings
        assert warning['refused'] is True
        assert warning['message'] == before['message']
        assert warning['edge_clearance_s'] == before['edge_clearance_s']
        assert warning['compared_with'] == before['compared_with']

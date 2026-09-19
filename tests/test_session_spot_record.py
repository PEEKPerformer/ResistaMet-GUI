"""What a run records about its spot: position effect, warning, header block."""
import copy
import math

import pytest

from resistamet_gui import calculations_geometry as geo
from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.schema.spots import SampleGeometry
from resistamet_gui.session.spot_record import (
    check_spot_position, spot_record_from_settings,
)

#: A 20 s x 20 s square with s = 1 mm, the example of the design document.
SQUARE = SampleGeometry(shape='rectangle', width_mm=20.0, length_mm=20.0)
WAFER = SampleGeometry(shape='circle', diameter_mm=50.8)


def _settings(spot=None, **measurement):
    settings = {'measurement': copy.deepcopy(DEFAULT_SETTINGS['measurement'])}
    settings['measurement'].update(measurement)
    if spot is not None:
        settings['spot'] = spot
    return settings


def _spot(**fields):
    return {'map_id': 'wafer7', 'index': 1, 'label': 'A', **fields}


class TestCheckSpotPosition:
    def test_unbounded_sample_has_nothing_to_check(self):
        assert check_spot_position(SampleGeometry(), 1.0, 5.0, 5.0, 0.0) is None

    def test_centre_costs_nothing(self):
        position = check_spot_position(SQUARE, 1.0, 0.0, 0.0, 0.0)
        assert position.relative_error == pytest.approx(0.0, abs=1e-12)
        assert position.factor_here == pytest.approx(position.factor_centre)
        assert position.edge_clearance_s == pytest.approx(8.5)

    def test_array_parallel_to_an_edge_three_spacings_away(self):
        """The design's table: about 5 % at 3 s, from the geometry module."""
        position = check_spot_position(SQUARE, 1.0, 0.0, 7.0, 0.0)
        assert position.edge_clearance_s == pytest.approx(3.0)
        assert position.factor_here == pytest.approx(
            geo.rectangle_factor(20.0, 20.0, 1.0, (0.0, 7.0), 0.0))
        assert position.relative_error == pytest.approx(0.0532, abs=5e-4)

    def test_angle_is_in_degrees(self):
        along_y = check_spot_position(SQUARE, 1.0, 7.0, 0.0, 90.0)
        along_x = check_spot_position(SQUARE, 1.0, 0.0, 7.0, 0.0)
        assert along_y.relative_error == pytest.approx(along_x.relative_error)

    def test_circle(self):
        position = check_spot_position(WAFER, 1.016, 15.0, 0.0, 90.0)
        assert position.factor_centre == pytest.approx(geo.circle_factor(50.8, 1.016))
        assert position.edge_clearance_s == pytest.approx(
            (25.4 - math.hypot(15.0, 1.5 * 1.016)) / 1.016)
        assert position.relative_error > 0

    @pytest.mark.parametrize("x_mm", [9.0, 8.5, 20.0])
    def test_a_tip_on_or_past_the_edge_is_off_the_sample(self, x_mm):
        position = check_spot_position(SQUARE, 1.0, x_mm, 0.0, 0.0)
        assert position.off_sample
        assert position.edge_clearance_s <= 0
        assert position.factor_here is None and position.relative_error is None

    def test_off_a_circle(self):
        assert check_spot_position(WAFER, 1.016, 25.0, 0.0, 0.0).off_sample


class TestSpotRecord:
    def test_no_spot_no_record(self):
        assert spot_record_from_settings(_settings()) is None

    def test_a_label_only_spot_on_an_unbounded_sample(self):
        record = spot_record_from_settings(_settings(_spot()))
        assert record.position is None
        assert not record.off_sample and not record.near_edge
        assert record.header() == {
            'map_id': 'wafer7', 'index': 1, 'label': 'A',
            'x_mm': None, 'y_mm': None, 'angle_deg': 0.0,
            'sample': {'shape': 'unbounded', 'diameter_mm': None,
                       'width_mm': None, 'length_mm': None},
            'position_correction': 'warn', 'edge_warn_pct': 1.0,
        }

    def test_a_position_on_an_unbounded_sample_is_recorded_without_a_factor(self):
        record = spot_record_from_settings(_settings(_spot(x_mm=3.0, y_mm=4.0)))
        assert record.position is None
        assert record.header()['x_mm'] == 3.0
        assert 'factor_here' not in record.header()

    def test_the_header_carries_the_position_effect(self):
        record = spot_record_from_settings(_settings(
            _spot(x_mm=0.0, y_mm=7.0), fpp_spacing_cm=0.1,
            fpp_sample_shape='rectangle', fpp_sample_width_mm=20.0,
            fpp_sample_length_mm=20.0))
        header = record.header()
        assert header['sample'] == {'shape': 'rectangle', 'diameter_mm': None,
                                    'width_mm': 20.0, 'length_mm': 20.0}
        assert header['edge_clearance_s'] == pytest.approx(3.0)
        assert header['relative_error'] == pytest.approx(
            header['factor_centre'] / header['factor_here'] - 1.0)
        assert record.near_edge and not record.off_sample

    def test_the_threshold_decides_the_warning(self):
        def record(warn_pct):
            return spot_record_from_settings(_settings(
                _spot(x_mm=0.0, y_mm=7.0), fpp_spacing_cm=0.1, fpp_edge_warn_pct=warn_pct,
                fpp_sample_shape='rectangle', fpp_sample_width_mm=20.0,
                fpp_sample_length_mm=20.0))
        assert record(5.0).near_edge          # 5.3 % > 5 %
        assert not record(6.0).near_edge

    def test_legacy_keys_give_the_outline(self):
        record = spot_record_from_settings(_settings(
            _spot(x_mm=0.0, y_mm=0.0), fpp_geometry='circle', fpp_diameter_cm=5.08))
        assert record.geometry.shape == 'circle'
        assert record.geometry.diameter_mm == pytest.approx(50.8)
        assert record.position.factor_centre == pytest.approx(
            geo.circle_factor(50.8, 1.016))

    def test_the_spots_angle_wins_over_the_setting(self):
        settings = _settings(_spot(angle_deg=30.0), fpp_array_angle_deg=90.0)
        assert spot_record_from_settings(settings).angle_deg == 30.0
        settings = _settings(_spot(), fpp_array_angle_deg=90.0)
        assert spot_record_from_settings(settings).angle_deg == 90.0

    def test_off_sample(self):
        record = spot_record_from_settings(_settings(
            _spot(x_mm=30.0, y_mm=0.0), fpp_geometry='circle', fpp_diameter_cm=5.08))
        assert record.off_sample
        assert not record.near_edge
        assert record.header()['factor_here'] is None
        assert record.header()['edge_clearance_s'] < 0

    def test_an_undescribable_outline_or_spot_raises(self):
        with pytest.raises(ValueError):
            spot_record_from_settings(_settings(_spot(), fpp_sample_shape='circle'))
        with pytest.raises(ValueError):
            spot_record_from_settings(_settings(_spot(map_id='../x')))

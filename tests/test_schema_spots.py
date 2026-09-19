"""The sample outline and the spot a four-point run request may carry."""
import pytest
from pydantic import ValidationError

from resistamet_gui.schema.spots import (
    SampleGeometry, SpotRequest, legacy_sample_geometry, same_outline,
    sample_geometry_from_settings,
)


class TestSampleGeometry:
    def test_default_is_the_unbounded_sheet(self):
        geometry = SampleGeometry()
        assert geometry.shape == 'unbounded'
        assert geometry.model_dump() == {
            'shape': 'unbounded', 'diameter_mm': None, 'width_mm': None, 'length_mm': None}

    def test_circle_needs_a_diameter(self):
        assert SampleGeometry(shape='circle', diameter_mm=50.8).diameter_mm == 50.8
        with pytest.raises(ValidationError, match="needs diameter_mm"):
            SampleGeometry(shape='circle')

    def test_rectangle_needs_width_and_length(self):
        geometry = SampleGeometry(shape='rectangle', width_mm=10.0, length_mm=30.0)
        assert (geometry.width_mm, geometry.length_mm) == (10.0, 30.0)
        with pytest.raises(ValidationError, match="needs length_mm"):
            SampleGeometry(shape='rectangle', width_mm=10.0)
        with pytest.raises(ValidationError, match="needs width_mm"):
            SampleGeometry(shape='rectangle', length_mm=10.0)

    @pytest.mark.parametrize("value", [0.0, -1.0, float('nan'), float('inf')])
    def test_dimensions_are_positive_and_finite(self, value):
        with pytest.raises(ValidationError):
            SampleGeometry(shape='circle', diameter_mm=value)

    def test_a_dimension_the_shape_does_not_have_is_refused(self):
        with pytest.raises(ValidationError, match="has no width_mm"):
            SampleGeometry(shape='circle', diameter_mm=50.0, width_mm=10.0)
        with pytest.raises(ValidationError, match="has no diameter_mm"):
            SampleGeometry(shape='unbounded', diameter_mm=50.0)

    def test_unknown_shape_and_unknown_key_are_refused(self):
        with pytest.raises(ValidationError):
            SampleGeometry(shape='triangle')
        with pytest.raises(ValidationError):
            SampleGeometry(shape='circle', diameter_mm=5.0, radius_mm=2.5)


class TestSpotRequest:
    def test_a_label_is_enough(self):
        spot = SpotRequest(map_id='wafer7-map1', index=0, label='centre')
        assert not spot.has_position
        assert spot.angle_deg is None

    def test_a_position_is_a_pair(self):
        assert SpotRequest(map_id='m', index=1, label='a', x_mm=1.0, y_mm=-2.0).has_position
        with pytest.raises(ValidationError, match="both x_mm and y_mm"):
            SpotRequest(map_id='m', index=1, label='a', x_mm=1.0)
        with pytest.raises(ValidationError, match="both x_mm and y_mm"):
            SpotRequest(map_id='m', index=1, label='a', y_mm=1.0)

    @pytest.mark.parametrize("map_id", ['a', 'A-b_9', 'x' * 64])
    def test_map_id_is_a_plain_token(self, map_id):
        assert SpotRequest(map_id=map_id, index=0, label='a').map_id == map_id

    @pytest.mark.parametrize("map_id", [
        '', 'x' * 65, '..', '../evil', '..\\evil', 'a/b', 'a\\b', '/etc/passwd',
        'C:evil', 'a.b', 'a b', 'map\n', 'map\x00', '~', 'café',
    ])
    def test_map_id_cannot_build_a_path(self, map_id):
        with pytest.raises(ValidationError):
            SpotRequest(map_id=map_id, index=0, label='a')

    @pytest.mark.parametrize("label", ['', 'two\nlines', 'tab\there', 'x' * 81])
    def test_label_stays_on_one_header_line(self, label):
        with pytest.raises(ValidationError):
            SpotRequest(map_id='m', index=0, label=label)

    def test_index_is_not_negative(self):
        with pytest.raises(ValidationError):
            SpotRequest(map_id='m', index=-1, label='a')

    @pytest.mark.parametrize("field", ['x_mm', 'y_mm', 'angle_deg'])
    def test_numbers_are_finite(self, field):
        values = {'x_mm': 0.0, 'y_mm': 0.0, 'angle_deg': 0.0, field: float('nan')}
        with pytest.raises(ValidationError):
            SpotRequest(map_id='m', index=0, label='a', **values)

    def test_unknown_key_is_refused(self):
        with pytest.raises(ValidationError):
            SpotRequest(map_id='m', index=0, label='a', z_mm=1.0)


class TestLegacyKeysDescribeTheSameSample:
    """``fpp_geometry`` / ``fpp_diameter_cm`` map onto an outline.

    These pin the mapping, not the numbers: the per-sample Rs still comes
    from the table look-up those two keys have always fed.
    """

    def test_nothing_set_is_unbounded(self):
        from resistamet_gui.constants import DEFAULT_SETTINGS
        geometry = sample_geometry_from_settings(DEFAULT_SETTINGS['measurement'])
        assert geometry == SampleGeometry(shape='unbounded')
        assert sample_geometry_from_settings({}) == SampleGeometry(shape='unbounded')

    def test_a_shape_without_a_diameter_is_still_unbounded(self):
        """Diameter 0 means 'treat as infinite' whatever the shape says."""
        geometry = sample_geometry_from_settings({'fpp_geometry': 'square', 'fpp_diameter_cm': 0.0})
        assert geometry.shape == 'unbounded'

    def test_circle_keeps_its_diameter(self):
        geometry = sample_geometry_from_settings({'fpp_geometry': 'circle', 'fpp_diameter_cm': 5.08})
        assert geometry.shape == 'circle'
        assert geometry.diameter_mm == pytest.approx(50.8)

    @pytest.mark.parametrize("legacy, ratio", [
        ('square', 1.0), ('rectangle_2', 2.0), ('rectangle_3', 3.0), ('rectangle_4', 4.0)])
    def test_rectangles_are_width_d_and_length_n_d(self, legacy, ratio):
        geometry = sample_geometry_from_settings({'fpp_geometry': legacy, 'fpp_diameter_cm': 1.2})
        assert geometry.shape == 'rectangle'
        assert geometry.width_mm == pytest.approx(12.0)
        assert geometry.length_mm == pytest.approx(12.0 * ratio)
        assert geometry.diameter_mm is None

    def test_unknown_legacy_shape_is_an_error(self):
        with pytest.raises(ValueError, match="unknown fpp_geometry"):
            sample_geometry_from_settings({'fpp_geometry': 'hexagon', 'fpp_diameter_cm': 1.0})


class TestNewKeysWinOnceAShapeIsChosen:
    def test_circle(self):
        geometry = sample_geometry_from_settings({
            'fpp_sample_shape': 'circle', 'fpp_sample_diameter_mm': 76.2,
            'fpp_geometry': 'square', 'fpp_diameter_cm': 1.0})
        assert geometry == SampleGeometry(shape='circle', diameter_mm=76.2)

    def test_rectangle_of_any_aspect_ratio(self):
        geometry = sample_geometry_from_settings({
            'fpp_sample_shape': 'rectangle', 'fpp_sample_width_mm': 10.0,
            'fpp_sample_length_mm': 17.5})
        assert geometry == SampleGeometry(shape='rectangle', width_mm=10.0, length_mm=17.5)

    def test_dimensions_of_another_shape_are_ignored(self):
        geometry = sample_geometry_from_settings({
            'fpp_sample_shape': 'circle', 'fpp_sample_diameter_mm': 50.0,
            'fpp_sample_width_mm': 10.0, 'fpp_sample_length_mm': 20.0})
        assert geometry == SampleGeometry(shape='circle', diameter_mm=50.0)

    def test_a_dimension_left_at_zero_is_missing(self):
        with pytest.raises(ValidationError, match="needs diameter_mm"):
            sample_geometry_from_settings({'fpp_sample_shape': 'circle'})
        with pytest.raises(ValidationError, match="needs length_mm"):
            sample_geometry_from_settings({
                'fpp_sample_shape': 'rectangle', 'fpp_sample_width_mm': 10.0})


class TestSameOutline:
    def test_survives_the_cm_to_mm_rounding(self):
        legacy = legacy_sample_geometry({'fpp_geometry': 'circle', 'fpp_diameter_cm': 5.08})
        assert same_outline(legacy, SampleGeometry(shape='circle', diameter_mm=50.8))

    def test_different_shape_or_size_is_different(self):
        circle = SampleGeometry(shape='circle', diameter_mm=50.8)
        assert not same_outline(circle, SampleGeometry(shape='circle', diameter_mm=50.9))
        assert not same_outline(circle, SampleGeometry(shape='unbounded'))

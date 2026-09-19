"""The sample outline and the spot a four-point run request may carry."""
import pytest
from pydantic import ValidationError

from resistamet_gui.schema.spots import SampleGeometry, SpotRequest


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

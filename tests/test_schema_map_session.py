"""The spot block the PySide6 window sends with a four-point run.

The window validates nothing through pydantic when a run starts, so whatever
the operator typed into *Spot name* or *Sample* has to come out of these
helpers as something ``SpotRequest`` accepts -- a label that ends a header
line, or a map id that is not a plain token, would refuse the run.
"""
import re
from datetime import datetime

import pytest

from resistamet_gui.schema import map_session
from resistamet_gui.schema.map_session import (
    MapSession, auto_label, new_map_id, spot_index, spot_label,
)
from resistamet_gui.schema.spots import LABEL_PATTERN, MAP_ID_PATTERN, SpotRequest

NOON = datetime(2026, 9, 19, 12, 0, 0)

HOSTILE = [
    '', '   ', '\n', '\t\r\n', '\x00', '\x7f', 'a\nb', 'a\x1fb', 'line\u2028sep',
    'para\u2029sep', 'next\x85line', '../../etc/passwd', 'C:\\data\\x', 'x' * 500,
    ' ' * 100 + 'edge', 'Ω/□ corner', 'centre (5 mm from flat)', '# spot.index: 7',
    'true', '12', None, 42, 3.5,
]


def test_bounds_are_the_models():
    """The constants repeat ``SpotRequest``'s bounds; this keeps them equal."""
    fields = SpotRequest.model_fields
    assert {m.max_length for m in fields['label'].metadata if hasattr(m, 'max_length')} == {
        map_session.MAX_LABEL_LENGTH}
    assert {m.le for m in fields['index'].metadata if hasattr(m, 'le')} == {map_session.MAX_INDEX}
    assert MAP_ID_PATTERN.endswith('{1,%d}$' % map_session.MAX_MAP_ID_LENGTH)


class TestSpotLabel:
    def test_a_plain_name_is_kept(self):
        assert spot_label('Centre', 3) == 'Centre'
        assert spot_label('  top left  ', 3) == 'top left'

    @pytest.mark.parametrize('text', ['', '   ', '\n', '\x00\x1f', None])
    def test_nothing_falls_back_to_the_automatic_name(self, text):
        assert spot_label(text, 4) == auto_label(4) == 'Spot 4'

    def test_a_line_break_becomes_a_space(self):
        assert spot_label('edge\r\nnorth', 1) == 'edge north'
        assert spot_label('a\u2028b\u2029c\x85d', 1) == 'a b c d'

    def test_a_long_name_is_cut_to_the_models_length(self):
        assert spot_label('x' * 500, 1) == 'x' * 80
        # The cut must not leave a trailing space the model would keep.
        assert spot_label('x' * 79 + ' tail', 1) == 'x' * 79

    @pytest.mark.parametrize('text', HOSTILE)
    def test_whatever_is_typed_the_model_accepts_the_label(self, text):
        label = spot_label(text, 2)
        assert re.match(LABEL_PATTERN, label)
        assert SpotRequest(map_id='m', index=2, label=label).label == label


class TestSpotIndex:
    def test_the_counter_is_the_index(self):
        assert spot_index(1) == 1
        assert spot_index(37) == 37

    @pytest.mark.parametrize('counter, index', [(-3, 0), (10_000, 9999), (None, 0), ('x', 0)])
    def test_out_of_range_is_clamped(self, counter, index):
        assert spot_index(counter) == index


class TestNewMapId:
    def test_form(self):
        assert new_map_id('Wafer 7', now=NOON, token='ab12') == '20260919-120000_Wafer_7_ab12'

    def test_the_sample_name_is_reduced_to_token_characters(self):
        assert new_map_id('../a b/c:d', now=NOON, token='ab12') == '20260919-120000_a_b_c_d_ab12'
        assert new_map_id('Ω□', now=NOON, token='ab12') == '20260919-120000_sample_ab12'
        assert new_map_id('', now=NOON, token='ab12') == '20260919-120000_sample_ab12'

    def test_a_long_sample_name_still_fits(self):
        map_id = new_map_id('s' * 300, now=NOON, token='ab12')
        assert len(map_id) == 64
        assert map_id.endswith('_ab12')

    def test_a_token_that_is_not_one_is_replaced(self):
        assert re.match(MAP_ID_PATTERN, new_map_id('a', now=NOON, token='../x'))

    def test_two_ids_in_the_same_second_differ(self):
        assert new_map_id('a', now=NOON) != new_map_id('a', now=NOON)

    @pytest.mark.parametrize('name', HOSTILE)
    def test_whatever_the_sample_is_called_the_id_is_a_token(self, name):
        assert re.match(MAP_ID_PATTERN, new_map_id(name))


class TestMapSession:
    def test_a_spot_is_what_the_model_would_dump(self):
        spot = MapSession(clock=lambda: NOON).spot_for_run('alice', 'Wafer 7', 1, 'Centre')
        assert spot == SpotRequest.model_validate(spot).model_dump()
        assert spot['map_id'].startswith('20260919-120000_Wafer_7_')
        assert (spot['index'], spot['label']) == (1, 'Centre')
        assert spot['x_mm'] is None and spot['y_mm'] is None and spot['angle_deg'] is None

    def test_no_map_until_a_run_needs_one(self):
        assert MapSession().map_id is None

    def test_runs_on_one_sample_share_the_map(self):
        session = MapSession()
        first = session.spot_for_run('alice', 'w7', 1, 'Spot 1')
        second = session.spot_for_run('alice', ' w7 ', 2, 'Spot 2')
        assert first['map_id'] == second['map_id'] == session.map_id
        assert (first['index'], second['index']) == (1, 2)

    def test_another_sample_is_another_map(self):
        session = MapSession()
        first = session.spot_for_run('alice', 'w7', 1, '')
        second = session.spot_for_run('alice', 'w8', 2, '')
        assert first['map_id'] != second['map_id']

    def test_coming_back_to_a_sample_does_not_continue_its_map(self):
        session = MapSession()
        first = session.spot_for_run('alice', 'w7', 1, '')
        session.spot_for_run('alice', 'w8', 2, '')
        again = session.spot_for_run('alice', 'w7', 3, '')
        assert again['map_id'] != first['map_id']

    def test_another_user_is_another_map(self):
        """The files of two users are in two directories; so are their maps."""
        session = MapSession()
        first = session.spot_for_run('alice', 'w7', 1, '')
        second = session.spot_for_run('bob', 'w7', 2, '')
        assert first['map_id'] != second['map_id']

    def test_reset_starts_a_new_map_even_within_the_second(self):
        session = MapSession(clock=lambda: NOON)
        first = session.spot_for_run('alice', 'w7', 1, '')
        session.reset()
        assert session.map_id is None
        second = session.spot_for_run('alice', 'w7', 1, '')
        assert first['map_id'] != second['map_id']

    def test_an_unnamed_spot_is_named_after_its_index(self):
        assert MapSession().spot_for_run('alice', 'w7', 5, '  ')['label'] == 'Spot 5'

    @pytest.mark.parametrize('text', HOSTILE)
    def test_nothing_typed_can_make_the_spot_invalid(self, text):
        spot = MapSession().spot_for_run(text, text, text, text)
        SpotRequest.model_validate(spot)

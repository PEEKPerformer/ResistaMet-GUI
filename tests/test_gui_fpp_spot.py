"""A four-point run started from the PySide6 window says which spot it is.

The window has no map: it has a *Spot name* field, a spot counter, *Save
Spot* and *Clear All*. ``schema.map_session`` holds the rule that turns those
into a spot; these tests check that the window applies it at the one place a
run is started, and that *Save Spot* still does what it did.

The worker is replaced by a stub that records what it was given, so no
instrument, no thread and no data file is involved.
"""
import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from resistamet_gui.schema.spots import SpotRequest

ROW = (1000.0, 1e-3, 1e-4, 10.0, 45.32, 4.532e-4, 2206.5, 'OK', '')


class _Signal:
    def connect(self, *_args):
        pass


class _RecordingWorker:
    """Stands in for MeasurementWorker: keeps its arguments, runs nothing."""

    started = []
    filename = ''

    def __init__(self, mode, sample_name, username, settings):
        self.mode = mode
        self.settings = settings
        type(self).started.append(self)

    def __getattr__(self, name):
        return _Signal()

    def start(self):
        pass


@pytest.fixture
def window(main_window, monkeypatch):
    from resistamet_gui.ui import main_window as main_window_module

    _RecordingWorker.started = []
    monkeypatch.setattr(main_window_module, 'MeasurementWorker', _RecordingWorker)
    main_window.sample_input.setText("wafer7")
    yield main_window
    # closeEvent asks before closing on a running measurement; a modal
    # question in a test hangs the session.
    main_window.measurement_running = False


def _run(window, mode='four_point'):
    """Press Start, let the (stub) run end, return the settings it was given."""
    window.start_measurement(mode)
    assert window.measurement_running, "the run did not start"
    settings = _RecordingWorker.started[-1].settings
    window.on_worker_finished()
    return settings


def _measure_and_save(window):
    """One placement as the operator does it: Start, readings, Save Spot."""
    settings = _run(window)
    window.tab_four_point._fpp_rows.append(ROW)
    window._save_fpp_spot()
    return settings['spot']


def test_a_four_point_run_carries_a_valid_spot(window):
    spot = _run(window)['spot']

    assert spot == SpotRequest.model_validate(spot).model_dump()
    assert spot['index'] == 1
    assert spot['label'] == 'Spot 1'
    assert 'wafer7' in spot['map_id']
    assert spot['x_mm'] is None and spot['y_mm'] is None


def test_the_label_is_the_spot_name_when_start_is_pressed(window):
    window.tab_four_point.fpp_spot_name.setText("  north edge ")
    assert _run(window)['spot']['label'] == 'north edge'


def test_an_empty_spot_name_is_the_automatic_one(window):
    window.tab_four_point.fpp_spot_name.setText("   ")
    assert _run(window)['spot']['label'] == 'Spot 1'


def test_a_spot_name_that_will_not_fit_is_cut_not_refused(window):
    window.tab_four_point.fpp_spot_name.setText("x" * 500)
    assert _run(window)['spot']['label'] == 'x' * 80


def test_other_modes_carry_no_spot(window):
    assert 'spot' not in _run(window, 'resistance')


def test_gathered_settings_are_unchanged(window):
    """The spot is added where the run starts; the gather step stays what the
    goldens in test_gather_golden describe."""
    assert 'spot' not in window.gather_settings_for_mode('four_point')


def test_save_spot_moves_the_next_run_to_the_next_index(window):
    tab = window.tab_four_point

    first = _measure_and_save(window)
    assert tab._fpp_spot_counter == 2
    assert tab.fpp_spot_name.text() == 'Spot 2'
    assert tab.fpp_spots_table.rowCount() == 1
    assert tab.fpp_spots_table.item(0, 0).text() == first['label'] == 'Spot 1'

    second = _measure_and_save(window)
    assert (first['index'], second['index']) == (1, 2)
    assert second['label'] == 'Spot 2'
    assert second['map_id'] == first['map_id']


def test_starting_again_without_saving_repeats_the_spot(window):
    """A redo: same index, same map; the newer file stands for the spot."""
    first = _run(window)['spot']
    again = _run(window)['spot']
    assert (again['map_id'], again['index']) == (first['map_id'], first['index'])


def test_another_sample_name_starts_another_map(window):
    first = _measure_and_save(window)
    window.sample_input.setText("wafer8")
    second = _run(window)['spot']

    assert second['map_id'] != first['map_id']
    assert 'wafer8' in second['map_id']
    # The window does not reset its counter for a new sample, so neither
    # does the index: the file and the spot table keep naming the same spot.
    assert second['index'] == 2


def test_clear_all_starts_another_map_at_spot_one(window):
    first = _measure_and_save(window)
    window._clear_all_fpp_spots()
    second = _run(window)['spot']

    assert second['map_id'] != first['map_id']
    assert (second['index'], second['label']) == (1, 'Spot 1')

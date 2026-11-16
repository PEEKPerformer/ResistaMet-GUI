"""The 4PP panel must show the numbers the CSV holds.

Before this, the panel recomputed sheet resistance and resistivity from the
raw voltage and current with the legacy K*alpha path and the widget values,
while the worker wrote ASTM F84 corrected numbers to the file. With F84 inputs
set, the screen and the file disagreed.
"""
import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def panel_window(main_window):
    """A window that believes a run is in progress, cleared before teardown.

    closeEvent prompts when a measurement is running, and a modal prompt in a
    test hangs the session.
    """
    yield main_window
    main_window.measurement_running = False


def _drive_sample(window, derived, values=None, timestamp=1000.0):
    """Feed one sample through the same path the worker's signals take."""
    window.active_mode = 'four_point'
    window.measurement_running = True
    window._on_sample_derived(timestamp, derived)
    window.update_data(timestamp, values or {'voltage': 1e-3, 'current': 1e-4},
                        'OK', '')


DERIVED = {'ratio': 10.0, 'rs': 45.32, 'rho': 4.532e-4, 'sigma': 2206.5,
           'v_unc': 1e-6, 'i_unc': 1e-9, 'method': 'f84'}


def test_panel_row_uses_the_workers_values(panel_window):
    _drive_sample(panel_window, DERIVED)

    row = panel_window.tab_four_point._fpp_rows[-1]
    _, v, i, ratio, rs, rho, sigma, compliance, event = row
    assert (ratio, rs, rho, sigma) == (10.0, 45.32, 4.532e-4, 2206.5)


def test_f84_corrections_reach_the_panel(panel_window):
    """A corrected Rs differs from the legacy K*alpha number for the same V/I.

    The legacy form would give 4.532 * (V/I) = 45.32 here; with a specimen
    diameter set, F84's F2 correction changes it, and the panel must follow
    the worker rather than recompute.
    """
    corrected = dict(DERIVED, rs=38.10, rho=3.81e-4)
    _drive_sample(panel_window, corrected)

    row = panel_window.tab_four_point._fpp_rows[-1]
    assert row[4] == 38.10
    assert row[5] == 3.81e-4


def test_sample_without_derived_adds_no_row(panel_window):
    """No derived values means no invented ones."""
    panel_window._last_fpp_derived = None
    panel_window.active_mode = 'four_point'
    panel_window.measurement_running = True
    panel_window.update_data(1000.0, {'voltage': 1e-3, 'current': 1e-4}, 'OK', '')

    assert panel_window.tab_four_point._fpp_rows == []


def test_panel_no_longer_recomputes(panel_window, monkeypatch):
    """The legacy calculation must not run on the GUI thread any more."""
    from resistamet_gui import calculations

    def fail(*args, **kwargs):
        raise AssertionError("4PP panel recomputed instead of using the worker's values")

    monkeypatch.setattr(calculations, 'calculate_four_point_probe', fail)
    monkeypatch.setattr(calculations, 'calculate_four_point_probe_bound', fail)
    _drive_sample(panel_window, DERIVED)

    assert panel_window.tab_four_point._fpp_rows

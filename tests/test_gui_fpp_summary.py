"""Export Summary and Save Spot report the run, not the tab.

The four-point rows on screen carry the worker's own Rs, rho and sigma. The
summary and the saved-spot statistics are taken from those rows and from the
settings the run was started with, by the function that writes the run's
file footer (``session.spot_stats``): rows in compliance are left out, and a
quantity nobody could compute is NaN, never 0.

The worker is a stub; the samples are handed to the window's own slots.
"""
import csv
import math
import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QFileDialog, QMessageBox

NAN = float('nan')


class _Signal:
    def connect(self, *_args):
        pass


class _StubWorker:
    filename = ''

    def __init__(self, mode, sample_name, username, settings):
        self.settings = settings

    def __getattr__(self, name):
        return _Signal()

    def start(self):
        pass


@pytest.fixture
def window(main_window, monkeypatch):
    from resistamet_gui.ui import main_window as main_window_module

    monkeypatch.setattr(main_window_module, 'MeasurementWorker', _StubWorker)
    for name in ('information', 'warning', 'critical'):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.Ok))
    main_window.sample_input.setText("wafer7")
    yield main_window
    main_window.measurement_running = False


def run(window, samples, method='legacy'):
    """A four-point run: Start, the samples as the worker reports them, finish.

    Each sample is ``(rs, rho, sigma)`` or ``(rs, rho, sigma, compliance)``.
    """
    window.start_measurement('four_point')
    assert window.measurement_running, "the run did not start"
    for index, sample in enumerate(samples):
        rs, rho, sigma = sample[:3]
        compliance = sample[3] if len(sample) > 3 else 'OK'
        stamp = 1000.0 + index
        window._on_sample_derived(stamp, {'ratio': 10.0, 'rs': rs, 'rho': rho,
                                          'sigma': sigma, 'method': method})
        window.update_data(stamp, {'voltage': 1e-3, 'current': 1e-4}, compliance, '')
    window.on_worker_finished()


def export(window, monkeypatch, tmp_path):
    """Press Export Summary; the file as {first cell: rest of the row}."""
    path = tmp_path / 'summary.csv'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName',
                        staticmethod(lambda *a, **k: (str(path), '')))
    window.export_fpp_summary()
    with open(path, newline='', encoding='utf-8-sig') as f:
        rows = [row for row in csv.reader(f) if row]
    return {row[0]: row[1:] for row in rows}


def test_the_summary_is_the_runs_values_not_a_recalculation(window, monkeypatch, tmp_path):
    """The F84 path gives an Rs no K * V/I of the tab's widgets would."""
    run(window, [(40.0, 0.8, 1.25), (42.0, 0.84, 1.19)], method='f84')

    summary = export(window, monkeypatch, tmp_path)

    assert summary["Sheet Resistance (Ω/□)"][0] == "41"
    assert summary["Resistivity (Ω·cm)"][0] == "0.82"
    assert summary["Conductivity (S/cm)"][0] == "1.22"
    assert summary["Samples"] == ["2"]


def test_the_header_is_the_runs_settings_not_the_tabs(window, monkeypatch, tmp_path):
    fpp = window.tab_four_point
    fpp.fpp_thickness_um.setValue(200.0)
    fpp.fpp_k_factor.setValue(4.4)
    run(window, [(40.0, 0.8, 1.25)])
    fpp.fpp_thickness_um.setValue(0.0)
    fpp.fpp_k_factor.setValue(3.0)
    fpp.fpp_model.setCurrentText('semi_infinite')

    summary = export(window, monkeypatch, tmp_path)

    assert float(summary["Thickness t (cm)"][0]) == pytest.approx(0.02)
    assert float(summary["K"][0]) == pytest.approx(4.4)
    assert summary["Model"] == ["thin_film"]
    assert "Method" not in summary


def test_an_f84_run_is_described_by_what_f84_read(window, monkeypatch, tmp_path):
    fpp = window.tab_four_point
    fpp.fpp_thickness_um.setValue(200.0)
    fpp.fpp_diameter_cm.setValue(5.0)
    fpp.fpp_geometry.setCurrentText('square')
    run(window, [(40.0, 0.8, 1.25)], method='f84')
    fpp.fpp_diameter_cm.setValue(0.0)

    summary = export(window, monkeypatch, tmp_path)

    assert summary["Method"] == ["ASTM F84"]
    assert float(summary["Diameter D (cm)"][0]) == 5.0
    assert summary["Geometry"] == ["square"]
    assert summary["Temperature (°C)"] == ["N/A"]
    # K and alpha are not in the F84 formula; naming them would mislead.
    assert "K" not in summary and "Alpha" not in summary


def test_compliance_rows_are_left_out_as_the_run_leaves_them_out(window, monkeypatch, tmp_path):
    run(window, [(40.0, 0.8, 1.25), (42.0, 0.84, 1.19), (5e6, 1e5, 1e-5, 'V_COMP')])

    summary = export(window, monkeypatch, tmp_path)

    assert summary["Sheet Resistance (Ω/□)"][0] == "41"
    assert summary["Samples"] == ["2"]
    assert summary["Excluded (compliance)"] == ["1"]


def test_a_quantity_with_no_value_exports_as_missing_not_zero(window, monkeypatch, tmp_path):
    """No thickness: every rho and sigma is NaN."""
    run(window, [(40.0, NAN, NAN), (42.0, NAN, NAN)])

    summary = export(window, monkeypatch, tmp_path)

    assert summary["Sheet Resistance (Ω/□)"][0] == "41"
    assert summary["Resistivity (Ω·cm)"] == ["N/A", "N/A"]
    assert summary["Conductivity (S/cm)"] == ["N/A", "N/A"]


class TestSavedSpots:
    def test_a_spot_with_no_valid_value_has_no_mean(self, window):
        run(window, [(NAN, NAN, NAN), (NAN, NAN, NAN)])
        window._save_fpp_spot()

        (spot,) = window.tab_four_point._fpp_spots
        for name in ('rs_mean', 'rs_std', 'rho_mean', 'sigma_mean'):
            assert math.isnan(spot[name]), name
        table = window.tab_four_point.fpp_spots_table
        assert [table.item(0, col).text() for col in (2, 3, 4)] == ["--", "--", "--"]

    def test_a_spot_leaves_out_its_compliance_rows(self, window):
        run(window, [(40.0, 0.8, 1.25), (42.0, 0.84, 1.19), (5e6, 1e5, 1e-5, 'V_COMP')])
        window._save_fpp_spot()

        (spot,) = window.tab_four_point._fpp_spots
        assert spot['rs_mean'] == pytest.approx(41.0)
        assert spot['rs_std'] == pytest.approx(math.sqrt(2.0))
        assert (spot['n'], spot['n_excluded']) == (2, 1)
        assert len(spot['rows']) == 3

    def test_a_spot_matches_the_runs_own_statistics(self, window):
        from resistamet_gui.session.spot_stats import SpotSamples, spot_statistics
        samples = [(40.0, 0.8, 1.25), (42.0, 0.84, 1.19), (41.5, 0.83, 1.2)]
        expected = SpotSamples()
        for rs, rho, sigma in samples:
            expected.add(1e-3, 1e-4, {'rs': rs, 'rho': rho, 'sigma': sigma})
        footer = spot_statistics(expected)

        run(window, samples)
        window._save_fpp_spot()

        (spot,) = window.tab_four_point._fpp_spots
        for name in ('rs', 'rho', 'sigma'):
            assert spot[f'{name}_mean'] == footer[name]['mean']
            assert spot[f'{name}_std'] == footer[name]['sd']

    def test_an_invalid_spot_exports_as_missing_and_stays_out_of_the_uniformity(
            self, window, monkeypatch, tmp_path):
        for samples in ([(40.0, 0.8, 1.25), (40.0, 0.8, 1.25)],
                        [(NAN, NAN, NAN)],
                        [(44.0, 0.88, 1.14), (44.0, 0.88, 1.14)]):
            run(window, samples)
            window._save_fpp_spot()

        summary = export(window, monkeypatch, tmp_path)

        # N is the samples that were not in compliance, as in the file footer.
        assert summary["Spot 2"][:4] == ["1", "N/A", "N/A", "N/A"]
        assert summary["Rs Mean-of-Means (Ω/□)"] == ["42"]
        # The chart draws the spots that have an Rs.
        names, means, stds = window._fpp_spot_bars()
        assert names == ["Spot 1", "Spot 3"] and means == [40.0, 44.0] and stds == [0.0, 0.0]

    def test_each_spot_keeps_the_settings_of_its_own_run(self, window, monkeypatch, tmp_path):
        fpp = window.tab_four_point
        fpp.fpp_thickness_um.setValue(200.0)
        run(window, [(40.0, 0.8, 1.25)])
        window._save_fpp_spot()
        fpp.fpp_thickness_um.setValue(300.0)
        run(window, [(44.0, 1.32, 0.76)])
        window._save_fpp_spot()

        first, second = fpp._fpp_spots
        assert first['run']['measurement']['fpp_thickness_um'] == 200.0
        assert second['run']['measurement']['fpp_thickness_um'] == 300.0
        # With nothing unsaved, the summary describes the last run.
        summary = export(window, monkeypatch, tmp_path)
        assert float(summary["Thickness t (cm)"][0]) == pytest.approx(0.03)
        assert summary["Sheet Resistance (Ω/□)"][0] == "44"

    def test_nothing_measured_exports_nothing(self, window, monkeypatch, tmp_path):
        path = tmp_path / 'summary.csv'
        monkeypatch.setattr(QFileDialog, 'getSaveFileName',
                            staticmethod(lambda *a, **k: (str(path), '')))
        window.export_fpp_summary()
        assert not path.exists()


class TestUncertaintyOnAFixedRange:
    """With auto range off the panel uses the range the run was pinned to,
    as the file's footer does."""

    ROWS = [(0.1 * k, 1e-3, 1e-3, 1.0, 4.532, float('nan'), float('nan'), 'OK', '')
            for k in range(1, 4)]

    def test_the_fixed_range_is_the_one_the_run_sends(self, main_window):
        w = main_window.tab_four_point
        w.fpp_voltage_compliance.setValue(20.0)
        w.fpp_voltage_range_auto.setChecked(True)
        assert main_window._fpp_fixed_voltage_range() is None
        w.fpp_voltage_range_auto.setChecked(False)
        assert main_window._fpp_fixed_voltage_range() == 20.0
        # A saved run's own settings win over the widgets.
        assert main_window._fpp_fixed_voltage_range(
            {'fpp_voltage_range_auto': True, 'fpp_voltage_compliance': 20.0}) is None

    def test_the_live_statistics_use_it(self, main_window):
        w = main_window.tab_four_point
        w.nplc.setValue(1.0)
        w.fpp_voltage_compliance.setValue(20.0)
        w._fpp_rows = list(self.ROWS)
        volts, amps, rs = ([row[i] for row in self.ROWS] for i in (1, 2, 4))
        shown = {}
        for auto, fixed in ((True, None), (False, 20.0)):
            w.fpp_voltage_range_auto.setChecked(auto)
            main_window._update_four_point_stats()
            from resistamet_gui.calculations import four_point_combined_uncertainty
            u = four_point_combined_uncertainty(rs, volts, amps, model='2400', nplc=1.0,
                                                v_fixed_range=fixed)
            assert f"inst {u.u_inst:.2g})" in w.fpp_rs_label.text()
            shown[auto] = u.u_inst
        assert shown[False] > shown[True]

    def test_a_saved_spots_statistics_use_it(self, main_window):
        from resistamet_gui.session.spot_stats import SpotSamples, spot_statistics
        run = {'measurement': {'nplc': 1.0, 'fpp_voltage_range_auto': False,
                               'fpp_voltage_compliance': 20.0}}
        samples = SpotSamples()
        for row in self.ROWS:
            samples.add(row[1], row[2], {'rs': row[4], 'rho': row[5], 'sigma': row[6]}, 'OK')
        on_fixed = spot_statistics(samples, model='2400', nplc=1.0, v_fixed_range=20.0)
        on_auto = spot_statistics(samples, model='2400', nplc=1.0)
        got = main_window._fpp_statistics(self.ROWS, run)
        assert got['rs']['u_inst'] == on_fixed['rs']['u_inst'] != on_auto['rs']['u_inst']

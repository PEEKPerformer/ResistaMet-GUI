"""
GUI Smoke Test — catches widget parenting, missing attributes, and crash-on-open bugs.
Requires PyQt5 but NOT a real instrument.
"""
import sys
import os
import pytest

# Skip entire module if PyQt5 is not available
pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt


@pytest.fixture(scope="module")
def app():
    """Create a QApplication for the test session."""
    # Use offscreen platform to avoid needing a display
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication(sys.argv)
    yield application


@pytest.fixture
def main_window(app, tmp_path, monkeypatch):
    """Create a ResistanceMeterApp with a temp config directory."""
    from resistamet_gui import constants
    original_config = constants.CONFIG_FILE
    constants.CONFIG_FILE = str(tmp_path / "config.json")

    from resistamet_gui.ui.main_window import ResistanceMeterApp

    # Bypass the modal user selection dialog in __init__
    monkeypatch.setattr(ResistanceMeterApp, 'select_user', lambda self: None)

    window = ResistanceMeterApp()

    # Simulate user selection manually
    window.config_manager.add_user("test_user")
    window.current_user = "test_user"
    window.user_label.setText("User: test_user")
    window.user_settings = window.config_manager.get_user_settings("test_user")
    window.update_ui_from_settings()

    yield window

    constants.CONFIG_FILE = original_config
    window.close()


class TestTabCreation:
    """Verify all tabs create without errors and have required attributes."""

    def test_resistance_tab_has_widgets(self, main_window):
        w = main_window.tab_resistance
        assert hasattr(w, 'res_test_current')
        assert hasattr(w, 'res_voltage_compliance')
        assert hasattr(w, 'res_measurement_type')
        assert hasattr(w, 'res_auto_range')
        assert hasattr(w, 'nplc')
        assert hasattr(w, 'sampling_rate')
        assert hasattr(w, 'live_readout')
        assert hasattr(w, 'canvas')
        assert hasattr(w, 'start_button')
        assert hasattr(w, 'stop_button')
        assert hasattr(w, 'pause_button')
        assert hasattr(w, 'mark_event_button')
        assert hasattr(w, 'status_label')

    def test_voltage_source_tab_has_widgets(self, main_window):
        w = main_window.tab_voltage_source
        assert hasattr(w, 'vsource_voltage')
        assert hasattr(w, 'vsource_current_compliance')
        assert hasattr(w, 'vsource_current_range_auto')
        assert hasattr(w, 'vsource_duration')
        assert hasattr(w, 'vsource_run_continuous')
        assert hasattr(w, 'nplc')
        assert hasattr(w, 'sampling_rate')
        assert hasattr(w, 'live_readout')
        assert hasattr(w, 'v_plot_var')

    def test_current_source_tab_has_widgets(self, main_window):
        w = main_window.tab_current_source
        assert hasattr(w, 'isource_current')
        assert hasattr(w, 'isource_voltage_compliance')
        assert hasattr(w, 'isource_voltage_range_auto')
        assert hasattr(w, 'isource_duration')
        assert hasattr(w, 'isource_run_continuous')
        assert hasattr(w, 'nplc')
        assert hasattr(w, 'sampling_rate')
        assert hasattr(w, 'live_readout')
        assert hasattr(w, 'i_plot_var')

    def test_four_point_tab_has_widgets(self, main_window):
        w = main_window.tab_four_point
        assert hasattr(w, 'fpp_current')
        assert hasattr(w, 'fpp_voltage_compliance')
        assert hasattr(w, 'fpp_spacing_cm')
        assert hasattr(w, 'fpp_thickness_um')
        assert hasattr(w, 'fpp_alpha')
        assert hasattr(w, 'fpp_k_factor')
        assert hasattr(w, 'fpp_model')
        assert hasattr(w, 'fpp_samples')
        assert hasattr(w, 'nplc')
        assert hasattr(w, 'sampling_rate')
        assert hasattr(w, 'live_readout')
        assert hasattr(w, 'fpp_show_plot')
        assert hasattr(w, 'fpp_table')
        assert hasattr(w, 'fpp_summary')
        assert hasattr(w, 'canvas')
        # New: histogram, spots, delta mode
        assert hasattr(w, 'fpp_histogram')
        assert hasattr(w, 'fpp_spots_table')
        assert hasattr(w, 'fpp_spot_name')
        assert hasattr(w, '_fpp_spots')
        assert hasattr(w, '_fpp_spot_counter')
        assert hasattr(w, 'fpp_delta_mode')
        assert hasattr(w, 'fpp_delta_settling')


class TestSettingsDialog:
    """Verify settings dialog opens and all widgets are accessible."""

    def test_user_settings_dialog_opens(self, main_window):
        from resistamet_gui.ui.dialogs import SettingsDialog
        dialog = SettingsDialog(main_window.config_manager, "test_user", main_window)
        # All widgets should be alive and readable
        assert dialog.gpib_address.text() is not None
        assert dialog.sampling_rate.value() > 0
        assert dialog.nplc.value() > 0
        assert dialog.settling_time.value() >= 0
        assert dialog.res_test_current.value() > 0
        assert dialog.res_voltage_compliance.value() > 0
        assert dialog.vsource_voltage is not None
        assert dialog.vsource_current_compliance.value() > 0
        assert dialog.isource_current is not None
        assert dialog.isource_voltage_compliance.value() > 0
        assert dialog.stop_on_compliance is not None
        dialog.close()

    def test_global_settings_dialog_opens(self, main_window):
        from resistamet_gui.ui.dialogs import SettingsDialog
        dialog = SettingsDialog(main_window.config_manager, parent=main_window)
        assert dialog.gpib_address.text() is not None
        dialog.close()

    def test_display_tab_widgets(self, main_window):
        from resistamet_gui.ui.dialogs import SettingsDialog
        dialog = SettingsDialog(main_window.config_manager, "test_user", main_window)
        assert dialog.enable_plot is not None
        assert dialog.plot_update_interval.value() > 0
        assert dialog.buffer_size is not None
        dialog.close()

    def test_file_tab_widgets(self, main_window):
        from resistamet_gui.ui.dialogs import SettingsDialog
        dialog = SettingsDialog(main_window.config_manager, "test_user", main_window)
        assert dialog.auto_save_interval.value() > 0
        assert dialog.data_directory.text() is not None
        dialog.close()


class TestGatherSettings:
    """Verify gather_settings_for_mode returns complete settings for each mode."""

    def test_resistance_settings(self, main_window):
        s = main_window.gather_settings_for_mode('resistance')
        m = s['measurement']
        assert 'res_test_current' in m
        assert 'res_voltage_compliance' in m
        assert 'nplc' in m
        assert 'sampling_rate' in m
        assert 'gpib_address' in m

    def test_source_v_settings(self, main_window):
        s = main_window.gather_settings_for_mode('source_v')
        m = s['measurement']
        assert 'vsource_voltage' in m
        assert 'vsource_current_compliance' in m
        assert 'vsource_duration_hours' in m
        assert 'nplc' in m

    def test_source_v_continuous_duration(self, main_window):
        """When run_continuous is checked, duration should be 0."""
        main_window.tab_voltage_source.vsource_run_continuous.setChecked(True)
        s = main_window.gather_settings_for_mode('source_v')
        assert s['measurement']['vsource_duration_hours'] == 0.0

    def test_source_i_settings(self, main_window):
        s = main_window.gather_settings_for_mode('source_i')
        m = s['measurement']
        assert 'isource_current' in m
        assert 'isource_voltage_compliance' in m
        assert 'isource_duration_hours' in m
        assert 'nplc' in m

    def test_four_point_settings(self, main_window):
        s = main_window.gather_settings_for_mode('four_point')
        m = s['measurement']
        assert 'fpp_current' in m
        assert 'fpp_spacing_cm' in m
        assert 'fpp_model' in m
        assert 'nplc' in m
        assert 'sampling_rate' in m


class TestUIInteractions:
    """Test UI interactions that don't require instruments."""

    def test_tab_switching(self, main_window):
        """All tabs should be switchable."""
        for i in range(main_window.main_tabs.count()):
            main_window.main_tabs.setCurrentIndex(i)

    def test_update_ui_from_settings(self, main_window):
        """Should not crash."""
        main_window.update_ui_from_settings()

    def test_clear_all_plots(self, main_window):
        """Should not crash."""
        main_window.clear_all_plots()

    def test_canvas_labels_update(self, main_window):
        """Should not crash for any mode."""
        for mode in ['resistance', 'source_v', 'source_i', 'four_point']:
            main_window.update_canvas_labels_for_mode(mode)

    def test_four_point_model_info(self, main_window):
        """Should not crash."""
        main_window.update_four_point_model_info()


class TestHistogramCanvas:
    """Test the new HistogramCanvas widget."""

    def test_histogram_update(self, main_window):
        w = main_window.tab_four_point
        w.fpp_histogram.update_histogram([1.0, 2.0, 3.0, 2.5, 2.1], 'Rs (Ω/□)')

    def test_histogram_empty(self, main_window):
        w = main_window.tab_four_point
        w.fpp_histogram.update_histogram([], 'Rs (Ω/□)')

    def test_histogram_nan_values(self, main_window):
        w = main_window.tab_four_point
        w.fpp_histogram.update_histogram([1.0, float('nan'), 2.0, float('nan')], 'Rs')

    def test_bar_chart(self, main_window):
        w = main_window.tab_four_point
        w.fpp_histogram.update_bar_chart(['Spot 1', 'Spot 2'], [10.0, 12.0], [0.5, 0.8])

    def test_clear(self, main_window):
        w = main_window.tab_four_point
        w.fpp_histogram.clear_histogram()


class TestSpotManagement:
    """Test multi-spot tracking functionality."""

    def test_save_spot_empty(self, main_window):
        """Save spot with no data should warn, not crash."""
        main_window._save_fpp_spot()  # no data, should log warning

    def test_save_spot_with_data(self, main_window):
        """Save spot with data should archive and clear."""
        w = main_window.tab_four_point
        w._fpp_rows = [(0, 0.001, 0.001, 1.0, 4.532, 0.001, 1000.0, 'OK', '')]
        main_window._save_fpp_spot()
        assert len(w._fpp_spots) == 1
        assert w._fpp_spots[0]['name'] == 'Spot 1'
        assert len(w._fpp_rows) == 0  # cleared after save

    def test_clear_all_spots(self, main_window):
        w = main_window.tab_four_point
        w._fpp_spots = [{'name': 'test', 'n': 1, 'rs_mean': 1, 'rs_std': 0,
                          'rho_mean': 0, 'rho_std': 0, 'sigma_mean': 0, 'sigma_std': 0, 'rows': []}]
        main_window._clear_all_fpp_spots()
        assert len(w._fpp_spots) == 0
        assert w._fpp_spot_counter == 1

    def test_delta_mode_settings(self, main_window):
        """Delta mode checkbox should be gatherable in settings."""
        w = main_window.tab_four_point
        w.fpp_delta_mode.setChecked(True)
        w.fpp_delta_settling.setValue(0.2)
        s = main_window.gather_settings_for_mode('four_point')
        assert s['measurement']['fpp_delta_mode'] is True
        assert s['measurement']['fpp_delta_settling'] == 0.2

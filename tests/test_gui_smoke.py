"""
GUI Smoke Test — catches widget parenting, missing attributes, and crash-on-open bugs.
Requires PySide6 but NOT a real instrument.
"""
import sys
import os
import pytest

# Skip entire module if PySide6 is not available
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt


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
        assert hasattr(w, 'res_offset_comp')
        assert hasattr(w, 'sampling_rate')
        assert hasattr(w, 'live_readout')
        assert hasattr(w, 'canvas')
        assert hasattr(w, 'start_button')
        assert hasattr(w, 'stop_button')
        assert hasattr(w, 'pause_button')
        assert hasattr(w, 'mark_event_button')
        assert hasattr(w, 'status_label')
        # NPLC is NOT on the tab (it's in Settings dialog)
        assert not hasattr(w, 'nplc')

    def test_voltage_source_tab_has_widgets(self, main_window):
        w = main_window.tab_voltage_source
        assert hasattr(w, 'vsource_voltage')
        assert hasattr(w, 'vsource_current_compliance')
        assert hasattr(w, 'vsource_current_range_auto')
        assert hasattr(w, 'vsource_duration')
        assert hasattr(w, 'vsource_run_continuous')
        assert hasattr(w, 'sampling_rate')
        assert hasattr(w, 'live_readout')
        assert hasattr(w, 'v_plot_var')
        assert not hasattr(w, 'nplc')

    def test_current_source_tab_has_widgets(self, main_window):
        w = main_window.tab_current_source
        assert hasattr(w, 'isource_current')
        assert hasattr(w, 'isource_voltage_compliance')
        assert hasattr(w, 'isource_voltage_range_auto')
        assert hasattr(w, 'isource_duration')
        assert hasattr(w, 'isource_run_continuous')
        assert hasattr(w, 'sampling_rate')
        assert hasattr(w, 'live_readout')
        assert hasattr(w, 'i_plot_var')
        assert not hasattr(w, 'nplc')

    def test_sweep_tab_has_widgets(self, main_window):
        w = main_window.tab_sweep
        assert hasattr(w, 'sweep_source')
        assert hasattr(w, 'sweep_start')
        assert hasattr(w, 'sweep_stop')
        assert hasattr(w, 'sweep_step')
        assert hasattr(w, 'sweep_compliance')
        assert hasattr(w, 'sweep_delay')
        assert hasattr(w, 'sweep_direction')
        assert hasattr(w, 'sweep_nplc')
        assert hasattr(w, 'iv_canvas')
        assert hasattr(w, 'start_button')
        assert hasattr(w, 'stop_button')
        assert hasattr(w, 'live_readout')

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
        assert hasattr(w, 'fpp_table')
        assert hasattr(w, 'fpp_summary')
        # 4PP visualizes via the histogram + tables, not a time-series MplCanvas
        assert hasattr(w, 'fpp_histogram')
        assert hasattr(w, 'fpp_spots_table')
        assert hasattr(w, 'fpp_spot_name')
        assert hasattr(w, '_fpp_spots')
        assert hasattr(w, '_fpp_spot_counter')
        assert hasattr(w, 'fpp_delta_mode')
        assert hasattr(w, 'fpp_delta_settling')

    def test_vdp_tab_has_widgets(self, main_window):
        w = main_window.tab_vdp
        # Parameter inputs
        assert hasattr(w, 'vdp_current')
        assert hasattr(w, 'vdp_voltage_compliance')
        assert hasattr(w, 'vdp_voltage_range_auto')
        assert hasattr(w, 'vdp_thickness_cm')
        assert hasattr(w, 'vdp_settling_s')
        assert hasattr(w, 'vdp_readings_per_polarity')
        assert hasattr(w, 'nplc')
        # Workflow widgets
        assert hasattr(w, 'vdp_step_label')
        assert hasattr(w, 'vdp_proceed_button')
        assert hasattr(w, 'vdp_readings_table')
        assert hasattr(w, 'vdp_homogeneity_banner')
        # Restructured result panel: headline labels + bar chart + stats.
        assert hasattr(w, 'vdp_rs_label')
        assert hasattr(w, 'vdp_rho_label')
        assert hasattr(w, 'vdp_bar_chart')
        assert hasattr(w, 'vdp_stats_label')
        # Bar chart accepts data + clear without crashing.
        w.vdp_bar_chart.resize(420, 130)
        w.vdp_bar_chart.set_data([1.27e-3, 1.21e-3, 1.26e-3, 1.25e-3], ["G1", "G2", "G3", "G4"])
        w.vdp_bar_chart.grab()
        w.vdp_bar_chart.clear()
        w.vdp_bar_chart.grab()
        # Collapse / restore of the instruction panel.
        assert hasattr(w, 'vdp_instr_group')
        w.vdp_instr_group.setVisible(False)
        w.vdp_instr_group.setVisible(True)
        # Geometry diagram is a custom QWidget; smoke check that it
        # accepts a configuration dict without crashing.
        assert hasattr(w, 'vdp_diagram')
        w.vdp_diagram.set_configuration({
            'source_high': 2, 'source_low': 1,
            'sense_high': 3, 'sense_low': 4,
        })
        # Trigger a real paint cycle so any int/float coord regression in
        # paintEvent is caught here.
        w.vdp_diagram.resize(260, 260)
        w.vdp_diagram.grab()
        w.vdp_diagram.set_configuration(None)  # back to idle
        w.vdp_diagram.grab()

        # Protocol filmstrip: 4-cell progress display
        assert hasattr(w, 'vdp_filmstrip')
        w.vdp_filmstrip.resize(520, 120)
        w.vdp_filmstrip.set_current(0)
        w.vdp_filmstrip.grab()
        w.vdp_filmstrip.mark_completed(0)
        w.vdp_filmstrip.set_current(1)
        w.vdp_filmstrip.grab()
        w.vdp_filmstrip.reset()
        w.vdp_filmstrip.grab()
        # Readings table is 8 rows (4 geometries x 2 polarities) prepopulated
        # with F76 labels.
        assert w.vdp_readings_table.rowCount() == 8
        labels = [w.vdp_readings_table.item(i, 0).text() for i in range(8)]
        assert labels == [
            "V_21,34", "V_12,34", "V_32,41", "V_23,41",
            "V_43,12", "V_34,12", "V_14,23", "V_41,23",
        ]
        # Control row buttons
        assert hasattr(w, 'start_button')
        assert hasattr(w, 'stop_button')
        # vdP has no continuous canvas
        assert w.canvas is None
        # Proceed button starts disabled (only enabled when worker is waiting)
        assert not w.vdp_proceed_button.isEnabled()


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
        # auto_zero moved to per-tab UI (resistance / source_v / source_i)
        # in v1.9.x — no longer lives on the settings dialog.
        assert not hasattr(dialog, 'auto_zero')
        assert main_window.tab_resistance.auto_zero.currentText() in ('on', 'once', 'off')
        assert dialog.filter_enabled is not None
        assert dialog.filter_type is not None
        assert dialog.filter_count.value() > 0
        assert dialog.res_offset_comp is not None
        assert dialog.safety_voltage_warn_v is not None
        assert dialog.safety_voltage_warn_silenced is not None
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
        # plot_update_interval / plot_figsize were removed from the dialog in
        # v1.9.x — the refresh rate is hard-capped at ~60 fps in the timer
        # setup and figsize never reached the canvas constructors.
        assert not hasattr(dialog, 'plot_update_interval')
        assert not hasattr(dialog, 'plot_width')
        assert dialog.buffer_size is not None
        dialog.close()

    def test_file_tab_widgets(self, main_window):
        from resistamet_gui.ui.dialogs import SettingsDialog
        dialog = SettingsDialog(main_window.config_manager, "test_user", main_window)
        assert dialog.auto_save_interval.value() > 0
        assert dialog.data_directory.text() is not None
        dialog.close()

    def test_output_tab_widgets_load_defaults(self, main_window):
        from resistamet_gui.ui.dialogs import SettingsDialog
        dialog = SettingsDialog(main_window.config_manager, "test_user", main_window)
        # Defaults: format=csv (index 0), compression=never (index 0).
        assert dialog.output_format.currentIndex() == 0
        assert dialog.output_compression.currentIndex() == 0
        # Threshold must be the configured default and disabled outside auto mode.
        assert dialog.output_compression_threshold.value() == 5.0
        assert not dialog.output_compression_threshold.isEnabled()
        dialog.close()

    def test_output_tab_auto_enables_threshold(self, main_window):
        from resistamet_gui.ui.dialogs import SettingsDialog
        dialog = SettingsDialog(main_window.config_manager, "test_user", main_window)
        # Switching compression to 'auto' (index 2) must enable the threshold.
        dialog.output_compression.setCurrentIndex(2)
        assert dialog.output_compression_threshold.isEnabled()
        dialog.close()

    def test_output_tab_hdf5_disables_compression_widgets(self, main_window):
        from resistamet_gui.ui.dialogs import SettingsDialog
        dialog = SettingsDialog(main_window.config_manager, "test_user", main_window)
        # Format 'hdf5' is index 1. Whether enabled depends on h5py presence —
        # only run the disable-flow assertion when the option is selectable.
        model_item = dialog.output_format.model().item(1)
        if not model_item.isEnabled():
            dialog.close()
            return
        dialog.output_format.setCurrentIndex(1)
        assert not dialog.output_compression.isEnabled()
        assert not dialog.output_compression_threshold.isEnabled()
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

    def test_sweep_settings(self, main_window):
        s = main_window.gather_settings_for_mode('sweep')
        m = s['measurement']
        assert 'sweep_source' in m
        assert 'sweep_start' in m
        assert 'sweep_stop' in m
        assert 'sweep_step' in m
        assert 'sweep_compliance' in m
        assert 'nplc' in m

    def test_four_point_settings(self, main_window):
        s = main_window.gather_settings_for_mode('four_point')
        m = s['measurement']
        assert 'fpp_current' in m
        assert 'fpp_spacing_cm' in m
        assert 'fpp_model' in m
        assert 'nplc' in m
        assert 'sampling_rate' in m

    def test_vdp_settings(self, main_window):
        s = main_window.gather_settings_for_mode('vdp')
        m = s['measurement']
        for key in (
            'vdp_current', 'vdp_voltage_compliance', 'vdp_voltage_range_auto',
            'vdp_thickness_cm', 'vdp_settling_s', 'vdp_readings_per_polarity',
            'nplc',
        ):
            assert key in m, f"vdp_settings missing {key}"


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
        for mode in ['resistance', 'source_v', 'source_i', 'four_point', 'sweep']:
            main_window.update_canvas_labels_for_mode(mode)

    def test_require_sample_name_returns_existing(self, main_window):
        """If the sample field is populated, no prompt is shown."""
        main_window.sample_input.setText("preset_sample")
        # Should NOT call QInputDialog (would hang in a non-interactive test
        # if it did); just returns the existing value.
        assert main_window._require_sample_name() == "preset_sample"

    def test_require_sample_name_prompts_when_empty(self, main_window, monkeypatch):
        """Empty field -> inline QInputDialog; accepted text populates the field."""
        from PySide6.QtWidgets import QInputDialog
        main_window.sample_input.setText("")
        monkeypatch.setattr(
            QInputDialog, "getText",
            classmethod(lambda cls, *a, **k: ("entered_via_prompt", True))
        )
        assert main_window._require_sample_name() == "entered_via_prompt"
        # The top-bar field is populated so the value stays visible.
        assert main_window.sample_input.text() == "entered_via_prompt"

    def test_require_sample_name_returns_none_on_cancel(self, main_window, monkeypatch):
        """User cancels the prompt -> helper returns None and field stays empty."""
        from PySide6.QtWidgets import QInputDialog
        main_window.sample_input.setText("")
        monkeypatch.setattr(
            QInputDialog, "getText",
            classmethod(lambda cls, *a, **k: ("", False))
        )
        assert main_window._require_sample_name() is None
        assert main_window.sample_input.text() == ""

    def test_require_sample_name_rejects_whitespace_only(self, main_window, monkeypatch):
        """Prompt accepted with whitespace-only text -> still treated as missing."""
        from PySide6.QtWidgets import QInputDialog
        main_window.sample_input.setText("")
        monkeypatch.setattr(
            QInputDialog, "getText",
            classmethod(lambda cls, *a, **k: ("   ", True))
        )
        assert main_window._require_sample_name() is None

    def test_vdp_tab_is_sixth(self, main_window):
        """Van der Pauw is added as the 6th measurement tab (index 5).

        Total tab count may be 7 (the Results Viewer tab is lazily
        appended after settings load); we only pin the vdP position.
        """
        assert main_window.main_tabs.tabText(5) == "Van der Pauw"
        assert main_window.main_tabs.widget(5) is main_window.tab_vdp

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

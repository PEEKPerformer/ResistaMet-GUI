"""Capture what ``gather_settings_for_mode`` produces, as test fixtures.

Run this against the GUI *before* the resolver takes over the non-widget part
of gathering (PR-15 in ``docs/design/tauri_backend_split.md``). The captured
dicts are the parity target: ``tests/test_gather_golden.py`` replays them
through both the GUI and ``schema.resolve``, so the refactor has to reproduce
today's settings exactly, key for key.

Usage::

    QT_QPA_PLATFORM=offscreen python tools/capture_gather_goldens.py

Writes ``tests/goldens/gather/<mode>_<variant>.json``, each holding the
profile, the widget-derived overrides and the expected result.

The widget-read table below mirrors ``gather_settings_for_mode``. That
duplication is deliberate: it is the only way to feed the resolver the same
inputs the GUI read, and PR-15 moves an equivalent table into the window.
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

GOLDEN_DIR = REPO_ROOT / "tests" / "goldens" / "gather"

MODES = ('resistance', 'source_v', 'source_i', 'four_point', 'sweep', 'vdp')


def overrides_from_widgets(window, mode):
    """Read the same widgets ``gather_settings_for_mode`` reads."""
    widget = window.get_widget_for_mode(mode)
    values = {}

    if mode == 'resistance':
        values['res_test_current'] = widget.res_test_current.value()
        values['res_voltage_compliance'] = widget.res_voltage_compliance.value()
        values['res_measurement_type'] = widget.res_measurement_type.currentText()
        values['res_auto_range'] = widget.res_auto_range.isChecked()
        values['res_offset_comp'] = widget.res_offset_comp.isChecked()
    elif mode == 'source_v':
        values['vsource_voltage'] = widget.vsource_voltage.value()
        values['vsource_current_compliance'] = widget.vsource_current_compliance.value()
        values['vsource_current_range_auto'] = widget.vsource_current_range_auto.isChecked()
        values['vsource_duration_hours'] = widget.vsource_duration.value()
        values['vsource_run_continuous'] = widget.vsource_run_continuous.isChecked()
    elif mode == 'source_i':
        values['isource_current'] = widget.isource_current.value()
        values['isource_voltage_compliance'] = widget.isource_voltage_compliance.value()
        values['isource_voltage_range_auto'] = widget.isource_voltage_range_auto.isChecked()
        values['isource_duration_hours'] = widget.isource_duration.value()
        values['isource_run_continuous'] = widget.isource_run_continuous.isChecked()
    elif mode == 'four_point':
        values['fpp_current'] = widget.fpp_current.value()
        values['fpp_voltage_compliance'] = widget.fpp_voltage_compliance.value()
        values['fpp_voltage_range_auto'] = widget.fpp_voltage_range_auto.isChecked()
        values['fpp_spacing_cm'] = widget.fpp_spacing_cm.value()
        values['fpp_thickness_um'] = widget.fpp_thickness_um.value()
        values['fpp_alpha'] = widget.fpp_alpha.value()
        values['fpp_model'] = widget.fpp_model.currentText()
        values['fpp_k_factor'] = widget.fpp_k_factor.value()
        values['fpp_samples'] = int(widget.fpp_samples.value())
        values['fpp_diameter_cm'] = float(widget.fpp_diameter_cm.value())
        values['fpp_geometry'] = widget.fpp_geometry.currentText()
        # The spin box's minimum doubles as its "not measured" special value;
        # the worker and the F84 code read that as NaN.
        temperature = widget.fpp_temperature_c.value()
        values['fpp_temperature_c'] = float('nan') if temperature <= -49.999 else float(temperature)
        values['fpp_dopant_type'] = widget.fpp_dopant_type.currentText()
        values['fpp_delta_mode'] = widget.fpp_delta_mode.isChecked()
        values['fpp_delta_settling'] = widget.fpp_delta_settling.value()
        values['fpp_power_warn_w'] = widget.fpp_power_warn_w.value()
        values['fpp_power_stop_w'] = widget.fpp_power_stop_w.value()
        values['fpp_stop_on_overpower'] = widget.fpp_stop_on_overpower.isChecked()
    elif mode == 'sweep':
        values['sweep_source'] = widget.sweep_source.currentText()
        values['sweep_start'] = widget.sweep_start.value()
        values['sweep_stop'] = widget.sweep_stop.value()
        values['sweep_step'] = widget.sweep_step.value()
        values['sweep_compliance'] = widget.sweep_compliance.value()
        values['sweep_delay'] = widget.sweep_delay.value()
        values['sweep_direction'] = widget.sweep_direction.currentText()
    elif mode == 'vdp':
        values['vdp_current'] = widget.vdp_current.value()
        values['vdp_voltage_compliance'] = widget.vdp_voltage_compliance.value()
        values['vdp_voltage_range_auto'] = widget.vdp_voltage_range_auto.isChecked()
        values['vdp_thickness_cm'] = widget.vdp_thickness_cm.value()
        values['vdp_settling_s'] = widget.vdp_settling_s.value()
        values['vdp_readings_per_polarity'] = int(widget.vdp_readings_per_polarity.value())

    # NPLC and sampling rate come from the tab when it has them.
    if hasattr(widget, 'nplc'):
        values['nplc'] = widget.nplc.value()
    elif hasattr(widget, 'sweep_nplc'):
        values['nplc'] = widget.sweep_nplc.value()
    if hasattr(widget, 'sampling_rate'):
        values['sampling_rate'] = widget.sampling_rate.value()
    if hasattr(widget, 'auto_zero'):
        values['auto_zero'] = widget.auto_zero.currentText()
    return values


def perturb(window, mode):
    """Move the tab off its defaults so the goldens exercise real values."""
    widget = window.get_widget_for_mode(mode)
    if mode == 'resistance':
        widget.res_test_current.setValue(2.5e-4)
        widget.res_measurement_type.setCurrentText('4-wire')
        widget.res_offset_comp.setChecked(False)
        widget.sampling_rate.setValue(4.0)
        widget.auto_zero.setCurrentText('off')
    elif mode == 'source_v':
        widget.vsource_voltage.setValue(42.0)
        widget.vsource_duration.setValue(2.0)
        widget.vsource_run_continuous.setChecked(True)
    elif mode == 'source_i':
        widget.isource_current.setValue(-5e-4)
        widget.isource_duration.setValue(3.0)
        widget.isource_run_continuous.setChecked(False)
    elif mode == 'four_point':
        widget.fpp_current.setValue(5e-5)
        widget.fpp_thickness_um.setValue(1.2)
        widget.fpp_temperature_c.setValue(23.5)
        widget.fpp_dopant_type.setCurrentText('n')
        widget.fpp_delta_mode.setChecked(True)
        widget.nplc.setValue(2.0)
    elif mode == 'sweep':
        widget.sweep_source.setCurrentText('current')
        widget.sweep_start.setValue(-1.0)
        widget.sweep_stop.setValue(1.0)
        widget.sweep_step.setValue(0.1)
        widget.sweep_direction.setCurrentText('up_down')
        widget.sweep_nplc.setValue(0.5)
    elif mode == 'vdp':
        widget.vdp_current.setValue(2e-3)
        widget.vdp_thickness_cm.setValue(0.05)
        widget.vdp_readings_per_polarity.setValue(3)


def capture(window, mode, variant):
    return {
        'mode': mode,
        'variant': variant,
        'profile': json.loads(json.dumps(window.user_settings)),
        'overrides': json.loads(json.dumps(overrides_from_widgets(window, mode))),
        'expected': json.loads(json.dumps(window.gather_settings_for_mode(mode))),
    }


def main():
    from PySide6.QtWidgets import QApplication
    from resistamet_gui import constants

    tmp_config = REPO_ROOT / "tools" / "_golden_capture_config.json"
    constants.CONFIG_FILE = str(tmp_config)

    from resistamet_gui.config import ConfigManager
    from resistamet_gui.ui import main_window as main_window_module
    from resistamet_gui.ui.main_window import ResistanceMeterApp

    main_window_module.ConfigManager = (
        lambda *args, **kwargs: ConfigManager(config_file=str(tmp_config))
    )
    ResistanceMeterApp.select_user = lambda self: None

    app = QApplication.instance() or QApplication([sys.argv[0]])
    window = ResistanceMeterApp()
    window.config_manager.add_user("golden_user")
    window.current_user = "golden_user"
    window.user_settings = window.config_manager.get_user_settings("golden_user")
    window.update_ui_from_settings()

    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for mode in MODES:
        for variant in ('defaults', 'perturbed'):
            if variant == 'perturbed':
                perturb(window, mode)
            golden = capture(window, mode, variant)
            path = GOLDEN_DIR / f"{mode}_{variant}.json"
            # allow_nan keeps fpp_temperature_c's NaN, which is the value the
            # worker actually receives for "not measured".
            path.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n")
            written.append(path)

    window.close()
    tmp_config.unlink(missing_ok=True)
    for path in written:
        print(f"wrote {path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()

"""End-to-end: aux co-logging is mode-agnostic — proven on a non-4PP run.

Runs a *resistance* measurement under --simulate with aux logging enabled and
asserts the saved CSV gains aux_* columns. This is the "Keithley + other data
sources" generality: the same co-logging path works on any continuous mode,
not just 4-point-probe.
"""
from __future__ import annotations

import csv
import glob
import os
import sys
import time

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.e2e
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

DUT_OHMS = 100.0
SIM_TEMP_C = 25.0


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def sim_window(app, tmp_path, monkeypatch):
    from resistamet_gui.simulator import enable_simulation
    enable_simulation(dut_resistance_ohms=DUT_OHMS, model="2420", sim_temp_c=SIM_TEMP_C)

    monkeypatch.chdir(tmp_path)
    from resistamet_gui import constants
    monkeypatch.setattr(constants, "CONFIG_FILE", str(tmp_path / "config.json"))

    from resistamet_gui.ui.main_window import ResistanceMeterApp

    def _no_dialog(self):
        self.current_user = "e2e"
        self.user_label.setText("User: <b>e2e</b>")
        self.user_settings = self.config_manager.get_user_settings("e2e")
        self.update_ui_from_settings()
        for buf in self.data_buffers.values():
            buf.clear()
        self.clear_all_plots()
        self.set_all_controls_enabled(True)
    monkeypatch.setattr(ResistanceMeterApp, "select_user", _no_dialog)

    window = ResistanceMeterApp()
    window.sample_input.setText("AUX-MODE")
    window.show()
    app.processEvents()
    try:
        yield window
    finally:
        if window.measurement_running:
            window.stop_current_measurement()
            _wait_until(lambda: not window.measurement_running, timeout=3.0, app=app)
        window._stop_aux_preview()
        window.close()


def _wait_until(condition, *, timeout, app):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        app.processEvents()
        time.sleep(0.05)
    return False


def _pump_for(seconds, app):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.05)


def _switch_to(window, label, app):
    idx = next(i for i in range(window.main_tabs.count())
               if window.main_tabs.tabText(i) == label)
    window.main_tabs.setCurrentIndex(idx)
    app.processEvents()


def _newest_csv():
    csvs = sorted(glob.glob("measurement_data/**/*.csv", recursive=True),
                  key=os.path.getmtime)
    assert csvs, "no CSV written"
    return csvs[-1]


def _header(path):
    with open(path) as f:
        for line in f:
            if not line.startswith("#"):
                return next(csv.reader([line]))
    raise AssertionError("no header row")


def test_resistance_run_co_logs_aux_columns(sim_window, app):
    sim_window.user_settings['measurement']['aux_log_enabled'] = True
    _switch_to(sim_window, "Resistance Measurement", app)
    tab = sim_window.tab_resistance
    tab.start_button.click()
    app.processEvents()
    assert sim_window.measurement_running, "resistance run did not start"
    _pump_for(2.0, app)
    sim_window.stop_current_measurement()
    assert _wait_until(lambda: not sim_window.measurement_running, timeout=3.0, app=app)

    header = _header(_newest_csv())
    assert "R_ohm" in header, f"not a resistance CSV? {header}"
    assert "aux_t_sample" in header, f"aux columns missing on resistance run: {header}"
    assert header.index("aux_t_sample") < header.index("compliance")


def test_resistance_schema_unchanged_with_aux_off(sim_window, app):
    from resistamet_gui.data_export import get_column_config
    baseline, _ = get_column_config("resistance")

    sim_window.user_settings['measurement']['aux_log_enabled'] = False
    _switch_to(sim_window, "Resistance Measurement", app)
    tab = sim_window.tab_resistance
    tab.start_button.click()
    app.processEvents()
    _pump_for(1.5, app)
    sim_window.stop_current_measurement()
    assert _wait_until(lambda: not sim_window.measurement_running, timeout=3.0, app=app)

    header = _header(_newest_csv())
    assert header == baseline, f"resistance schema drifted with aux off: {header}"
    assert not any(c.startswith("aux_") for c in header)

"""End-to-end: the live auxiliary-sensor readout on the 4PP tab.

Verifies the equilibration-watch lifecycle under --simulate: an idle preview
opens the sensor and shows a value; Start hands the serial port off to the
worker (preview released); the readout keeps updating during the run from
data_point; and the preview resumes once the run ends.
"""
from __future__ import annotations

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
    window.sample_input.setText("AUX-MON")
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


def _switch_to_4pp(window, app):
    idx = next(i for i in range(window.main_tabs.count())
               if window.main_tabs.tabText(i) == "4-Point Probe")
    window.main_tabs.setCurrentIndex(idx)
    app.processEvents()


def test_idle_preview_shows_temperature(sim_window, app):
    _switch_to_4pp(sim_window, app)
    tab = sim_window.tab_four_point
    tab.fpp_log_temp.setChecked(True)          # triggers _start_aux_preview
    app.processEvents()
    assert sim_window._aux_preview_sensor is not None, "preview did not open"

    sim_window._refresh_aux_preview()          # deterministic tick
    text = tab.fpp_temp_readout.text()
    assert "°C" in text, f"no unit in readout: {text!r}"
    # The simulated thermocouple streams ~SIM_TEMP_C.
    assert "25" in text, f"expected ~25 C in readout: {text!r}"


def test_start_releases_preview_and_run_feeds_readout(sim_window, app):
    _switch_to_4pp(sim_window, app)
    tab = sim_window.tab_four_point
    tab.fpp_log_temp.setChecked(True)
    app.processEvents()
    assert sim_window._aux_preview_sensor is not None

    tab.start_button.click()
    app.processEvents()
    assert sim_window.measurement_running, "4PP run did not start"
    # Port handed off: the preview handle must be released before the worker
    # opens its own.
    assert sim_window._aux_preview_sensor is None, "preview not released on Start"

    _pump_for(2.0, app)
    in_run_text = tab.fpp_temp_readout.text()
    assert any(ch.isdigit() for ch in in_run_text), f"no live value during run: {in_run_text!r}"

    sim_window.stop_current_measurement()
    assert _wait_until(lambda: not sim_window.measurement_running, timeout=3.0, app=app)
    # Preview resumes once the port is free again.
    assert _wait_until(lambda: sim_window._aux_preview_sensor is not None,
                       timeout=2.0, app=app), "preview did not resume after run"

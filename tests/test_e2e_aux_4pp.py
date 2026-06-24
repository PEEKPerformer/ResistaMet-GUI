"""End-to-end: 4PP co-logging an auxiliary sensor under --simulate.

Drives the real Start → MeasurementWorker → FakeSerialSensor → exporter seam
and asserts the saved CSV gains aux_* columns with plausible values when
logging is on, and is schema-identical to a normal 4PP run when it is off.
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
    window.sample_input.setText("AUX-DUT")
    window.show()
    app.processEvents()
    try:
        yield window
    finally:
        if window.measurement_running:
            window.stop_current_measurement()
            _wait_until(lambda: not window.measurement_running, timeout=3.0, app=app)
        window.close()


def _switch_to(window, label, app):
    idx = next(i for i in range(window.main_tabs.count())
               if window.main_tabs.tabText(i) == label)
    window.main_tabs.setCurrentIndex(idx)
    app.processEvents()


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


def _read_csv_data(path):
    with open(path) as f:
        return list(csv.reader(line for line in f if not line.startswith('#')))


def _run_4pp(window, app, seconds=3.0):
    _switch_to(window, "4-Point Probe", app)
    tab = window.tab_four_point
    tab.start_button.click()
    app.processEvents()
    assert window.measurement_running, "4PP worker did not start"
    _pump_for(seconds, app)
    window.stop_current_measurement()
    assert _wait_until(lambda: not window.measurement_running, timeout=3.0, app=app)


def _newest_4pp_csv():
    csvs = sorted(glob.glob("measurement_data/**/*_4PP_*.csv", recursive=True),
                  key=os.path.getmtime)
    assert csvs, "no 4PP CSV written"
    return csvs[-1]


def test_aux_columns_present_with_plausible_values(sim_window, app):
    sim_window.user_settings['measurement']['aux_log_enabled'] = True
    sim_window.user_settings['measurement']['aux_address'] = "ASRL6::INSTR"
    _run_4pp(sim_window, app, seconds=3.0)

    rows = _read_csv_data(_newest_4pp_csv())
    header, data = rows[0], rows[1:]
    assert "aux_t_sample" in header, f"missing aux_t_sample in header: {header}"
    assert "aux_t_coldjunction" in header, f"missing aux_t_coldjunction: {header}"
    assert data, "no 4PP data rows written"

    ti = header.index("aux_t_sample")
    temps = [float(r[ti]) for r in data if r[ti] not in ("", "nan", "NaN")]
    assert temps, "no aux temperature values recorded"
    bad = [t for t in temps if abs(t - SIM_TEMP_C) > 2.0]
    assert not bad, f"aux temps implausible (expected ~{SIM_TEMP_C}): {bad[:3]}"


def test_aux_columns_land_before_compliance(sim_window, app):
    sim_window.user_settings['measurement']['aux_log_enabled'] = True
    _run_4pp(sim_window, app, seconds=2.5)
    header = _read_csv_data(_newest_4pp_csv())[0]
    # aux columns splice in just before compliance/event.
    assert header.index("aux_t_sample") < header.index("compliance")
    assert header.index("aux_t_coldjunction") < header.index("compliance")


def test_logging_off_leaves_schema_unchanged(sim_window, app):
    from resistamet_gui.data_export import get_column_config
    baseline, _ = get_column_config("four_point")

    sim_window.user_settings['measurement']['aux_log_enabled'] = False
    _run_4pp(sim_window, app, seconds=2.5)
    header = _read_csv_data(_newest_4pp_csv())[0]
    assert header == baseline, (
        f"4PP schema drifted with logging off: {header} != {baseline}"
    )
    assert not any(c.startswith("aux_") for c in header)

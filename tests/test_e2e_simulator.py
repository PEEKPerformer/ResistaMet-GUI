"""End-to-end integration tests: drive each tab's Start button → worker
→ simulator → buffer/export, and assert the recorded values match Ohm's
law against a known fake DUT.

The existing unit tests cover construction (test_gui_smoke), the worker
in isolation (test_workers), the simulator's SCPI fidelity
(test_fake_matches_hardware), and pure-function math (test_calculations).
None of them exercise the seam where a tab's Start click flows through
``start_measurement``, ``MeasurementWorker``, ``pyvisa.ResourceManager``
(monkey-patched to the in-package fake), back through the data signal,
and finally into ``EnhancedDataBuffer`` / CSV export. v1.6.0 shipped a
real bug at that seam (``widget.canvas.clear_plot()`` on a 4PP tab whose
canvas had been removed); this file is the regression net.

Each test runs ~1-2 seconds against a 100Ω fake DUT and verifies the
recorded V/I/R values against Ohm's law. Drift > tolerance, missing
points, or a crash before Stop is the signal of a real defect.
"""
from __future__ import annotations

import csv
import glob
import math
import os
import sys
import time

import pytest

# Skip the whole module if PyQt5 isn't installed (matches test_gui_smoke).
pytest.importorskip("PyQt5")

# All tests in this module are end-to-end. The marker is informational —
# pytest.ini ignores this file in the default run; CI runs it explicitly.
pytestmark = pytest.mark.e2e

# Offscreen platform so the tests run in CI without a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication  # noqa: E402

# Known DUT resistance for Ohm's-law assertions across all modes.
DUT_OHMS = 100.0


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def sim_window(app, tmp_path, monkeypatch):
    """Yield a freshly-constructed ResistanceMeterApp wired to the in-package
    Keithley simulator with a known DUT. cwd is the tmp_path so CSV/JSON
    exports land in a per-test directory that pytest cleans up.
    """
    from resistamet_gui.simulator import enable_simulation
    enable_simulation(dut_resistance_ohms=DUT_OHMS, model="2420")

    # Run inside tmp_path so measurement_data/ writes don't pollute the repo.
    monkeypatch.chdir(tmp_path)

    from resistamet_gui import constants
    monkeypatch.setattr(constants, "CONFIG_FILE", str(tmp_path / "config.json"))

    from resistamet_gui.ui.main_window import ResistanceMeterApp

    # Skip the modal user dialog and seed a usable demo user.
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
    window.sample_input.setText("E2E-DUT")
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


def _drive_timed_run(window, tab, label, seconds, app):
    """Click Start on a time-series tab, pump events for ``seconds``, then
    cleanly Stop. Returns the number of points captured."""
    _switch_to(window, label, app)
    tab.start_button.click()
    app.processEvents()
    assert window.measurement_running, f"{label}: worker did not start"
    _pump_for(seconds, app)
    window.stop_current_measurement()
    assert _wait_until(lambda: not window.measurement_running, timeout=3.0, app=app), (
        f"{label}: worker did not stop within timeout"
    )
    mode_key = {
        "Resistance Measurement": "resistance",
        "Voltage Source": "source_v",
        "Current Source": "source_i",
        "4-Point Probe": "four_point",
    }[label]
    return list(window.data_buffers[mode_key].timestamps), \
           list(window.data_buffers[mode_key].voltage), \
           list(window.data_buffers[mode_key].current), \
           list(window.data_buffers[mode_key].resistance)


def test_resistance_records_ohms_law(sim_window, app):
    ts, _, _, rs = _drive_timed_run(
        sim_window, sim_window.tab_resistance, "Resistance Measurement",
        seconds=1.5, app=app,
    )
    assert len(ts) >= 3, f"too few points: {len(ts)}"
    finite_rs = [r for r in rs if r is not None and not math.isnan(r)]
    assert finite_rs, "no resistance values recorded"
    bad = [r for r in finite_rs if abs(r - DUT_OHMS) > 0.01]
    assert not bad, f"resistance drift: {bad[:3]} (expected {DUT_OHMS})"


def test_voltage_source_records_correct_current(sim_window, app):
    # Default sourced voltage is 1.0 V into 100 Ω → I = 10 mA.
    ts, vs, is_, _ = _drive_timed_run(
        sim_window, sim_window.tab_voltage_source, "Voltage Source",
        seconds=1.5, app=app,
    )
    assert len(ts) >= 3, f"too few points: {len(ts)}"
    bad_v = [v for v in vs if v is not None and abs(v - 1.0) > 1e-3]
    bad_i = [i for i in is_ if i is not None and abs(i - 0.01) > 1e-5]
    assert not bad_v, f"V drift: {bad_v[:3]}"
    assert not bad_i, f"I drift (expected 10 mA): {bad_i[:3]}"


def test_current_source_records_correct_voltage(sim_window, app):
    # Default sourced current is 1 mA into 100 Ω → V = 0.1 V.
    ts, vs, is_, _ = _drive_timed_run(
        sim_window, sim_window.tab_current_source, "Current Source",
        seconds=1.5, app=app,
    )
    assert len(ts) >= 3, f"too few points: {len(ts)}"
    bad_v = [v for v in vs if v is not None and abs(v - 0.1) > 1e-4]
    bad_i = [i for i in is_ if i is not None and abs(i - 1e-3) > 1e-7]
    assert not bad_v, f"V drift (expected 0.1 V): {bad_v[:3]}"
    assert not bad_i, f"I drift: {bad_i[:3]}"


def test_four_point_probe_records_v_i_at_source(sim_window, app):
    # 4PP sources current, measures voltage. With DUT = 100 Ω and the form's
    # configured source current, V should equal I_src × 100.
    ts, vs, is_, _ = _drive_timed_run(
        sim_window, sim_window.tab_four_point, "4-Point Probe",
        seconds=1.5, app=app,
    )
    assert len(ts) >= 3, f"too few points: {len(ts)}"
    src_i = sim_window.tab_four_point.fpp_current.value()
    expected_v = src_i * DUT_OHMS
    bad_v = [v for v in vs if v is not None and abs(v - expected_v) > 1e-4]
    bad_i = [i for i in is_ if i is not None and abs(i - src_i) > 1e-7]
    assert not bad_v, f"V drift (expected {expected_v}): {bad_v[:3]}"
    assert not bad_i, f"I drift (expected {src_i}): {bad_i[:3]}"


def test_iv_sweep_writes_linear_csv(sim_window, app, tmp_path):
    # Sweep is atomic — values land in IVCanvas + CSV/JSON, not the buffer.
    # Verify by reading the saved CSV.
    _switch_to(sim_window, "I-V Sweep", app)
    w = sim_window.tab_sweep
    w.sweep_source.setCurrentText("voltage")
    w.sweep_start.setValue(-1.0)
    w.sweep_stop.setValue(1.0)
    w.sweep_step.setValue(0.05)
    w.sweep_compliance.setValue(0.1)
    w.sweep_direction.setCurrentText("up")
    w.start_button.click()
    app.processEvents()
    assert _wait_until(
        lambda: not sim_window.measurement_running,
        timeout=10.0, app=app,
    ), "sweep did not finish within timeout"

    csvs = sorted(glob.glob("measurement_data/**/*.csv", recursive=True))
    assert csvs, "sweep produced no CSV file"
    with open(csvs[0]) as f:
        rows = list(csv.reader(f))
    header = rows[0]
    # Expected columns: point, V_source, I_meas, compliance
    assert header[:4] == ["point", "V_source", "I_meas", "compliance"], (
        f"unexpected sweep CSV header: {header}"
    )
    data = rows[1:]
    assert len(data) >= 30, f"too few sweep points in CSV: {len(data)}"
    bad = []
    for r in data:
        v, i = float(r[1]), float(r[2])
        if abs(i - v / DUT_OHMS) > 1e-5:
            bad.append((v, i))
    assert not bad, f"sweep CSV non-linear (expected I = V/{DUT_OHMS}): {bad[:3]}"
    # Endpoints sanity: first row at -1.0 V, last at +1.0 V.
    first_v = float(data[0][1])
    last_v = float(data[-1][1])
    assert abs(first_v - (-1.0)) < 1e-9, f"sweep first V = {first_v}, expected -1.0"
    assert abs(last_v - 1.0) < 1e-9, f"sweep last V = {last_v}, expected 1.0"

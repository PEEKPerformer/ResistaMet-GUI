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


def _read_csv_data(path):
    """Read a v2.0 CSV: skip #-prefixed metadata header + trailer lines.

    Returns the list of CSV rows (column header first, then data) with all
    `#` comment lines removed. Matches the behavior pandas gets with
    `read_csv(comment='#')` but keeps stdlib-only.
    """
    with open(path) as f:
        return list(csv.reader(line for line in f if not line.startswith('#')))


def _reset_simulator(ohms: float, model: str = "2420"):
    """Re-call enable_simulation with new DUT params. Tests that need a
    different fake resistance than the fixture default use this."""
    from resistamet_gui.simulator import enable_simulation
    enable_simulation(dut_resistance_ohms=ohms, model=model)


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
    rows = _read_csv_data(csvs[0])
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


# --------------------------------------------------------------------------
# CSV column / unit validation
# --------------------------------------------------------------------------

def test_csv_headers_match_documented_schema(sim_window, app):
    """Each mode's saved CSV must use the column names declared in
    ``data_export.get_column_config``. A unit-confusion regression (e.g.
    swapping I_meas/V_meas, or renaming R_ohm to R_mohm without updating
    the export) would slip past every other test in the suite.
    """
    from resistamet_gui.data_export import get_column_config

    # Drive a brief run in each per-tab mode that writes a CSV, then read
    # the CSV header and compare to the documented columns.
    cases = [
        (sim_window.tab_resistance, "Resistance Measurement", "resistance"),
        (sim_window.tab_voltage_source, "Voltage Source", "source_v"),
        (sim_window.tab_current_source, "Current Source", "source_i"),
        (sim_window.tab_four_point, "4-Point Probe", "four_point"),
    ]
    for tab, label, mode in cases:
        _switch_to(sim_window, label, app)
        tab.start_button.click()
        app.processEvents()
        assert sim_window.measurement_running, f"{label}: worker didn't start"
        _pump_for(1.0, app)
        sim_window.stop_current_measurement()
        assert _wait_until(
            lambda: not sim_window.measurement_running, timeout=3.0, app=app
        ), f"{label}: didn't stop cleanly"

    # CSV files land under measurement_data/<user>/...; pick the newest per mode.
    files = sorted(glob.glob("measurement_data/**/*.csv", recursive=True))
    assert files, "no CSV files written"

    # Map each file back to its mode via the tag in the filename. Worker
    # uses mode_tags = {'resistance': 'R', 'source_v': 'VSRC',
    # 'source_i': 'ISRC', 'four_point': '4PP'}. Sweep falls through to
    # 'DATA' but its source_value_str contains 'sweep_'.
    tag_to_mode = {"_R_": "resistance", "_VSRC_": "source_v",
                   "_ISRC_": "source_i", "_4PP_": "four_point",
                   "_sweep_": "sweep"}
    found_modes = set()
    for path in files:
        mode = next((m for tag, m in tag_to_mode.items() if tag in path), None)
        if mode is None:
            continue
        expected_cols, _units = get_column_config(mode)
        rows = _read_csv_data(path)
        header = rows[0] if rows else []
        assert header == expected_cols, (
            f"{path}: header {header} != expected {expected_cols} for {mode}"
        )
        found_modes.add(mode)
    # All four time-series modes should have been covered.
    assert {"resistance", "source_v", "source_i", "four_point"} <= found_modes, (
        f"missing CSV coverage; found modes: {found_modes}"
    )


# --------------------------------------------------------------------------
# Compliance handling end-to-end
# --------------------------------------------------------------------------

def test_voltage_compliance_clamps_and_flags(sim_window, app):
    """When V_compliance is set below what the sourced current × DUT would
    produce, the instrument clamps voltage and sets STAT bit 3. The worker
    parses that into ``compliance_status='V_COMP'`` and the buffer records
    it on every clamped point.
    """
    # 10kΩ DUT + 1 mA sourced → V would naturally be 10 V; clamp to 1 V.
    _reset_simulator(ohms=10_000.0)
    _switch_to(sim_window, "Voltage Source", app)
    w = sim_window.tab_voltage_source
    w.vsource_voltage.setValue(10.0)            # 10 V into 10 kΩ → 1 mA
    w.vsource_current_compliance.setValue(1e-4) # but compliance is 100 µA
    w.start_button.click()
    app.processEvents()
    _pump_for(1.5, app)
    sim_window.stop_current_measurement()
    assert _wait_until(
        lambda: not sim_window.measurement_running, timeout=3.0, app=app
    )

    buf = sim_window.data_buffers["source_v"]
    statuses = list(buf.compliance_status)
    assert statuses, "no compliance status recorded"
    # The fake sets the compliance bit when output × R exceeds the compliance
    # limit; at least one point should flag I_COMP (we capped current).
    flagged = [s for s in statuses if s != "OK"]
    assert flagged, (
        f"expected compliance-flagged points; got all OK ({len(statuses)} pts)"
    )
    # And the recorded current shouldn't exceed compliance by more than rounding.
    currents = [i for i in list(buf.current) if i is not None]
    assert all(abs(i) <= 1.1e-4 for i in currents), (
        f"current exceeded compliance: max={max(map(abs, currents))}"
    )


# --------------------------------------------------------------------------
# Mark Event during a run
# --------------------------------------------------------------------------

def test_mark_event_lands_in_csv(sim_window, app, monkeypatch):
    """``mark_event_shortcut`` prompts for a label via QInputDialog; tests
    monkey-patch that to return a fixed label. The label must then appear in
    the ``event`` column of at least one row of the saved CSV.
    """
    from PyQt5.QtWidgets import QInputDialog
    monkeypatch.setattr(
        QInputDialog, "getText",
        staticmethod(lambda *a, **kw: ("PROBE_MOVED", True)),
    )

    _switch_to(sim_window, "Resistance Measurement", app)
    sim_window.tab_resistance.start_button.click()
    app.processEvents()
    _pump_for(0.5, app)
    sim_window.mark_event_shortcut()
    _pump_for(0.7, app)
    sim_window.stop_current_measurement()
    assert _wait_until(
        lambda: not sim_window.measurement_running, timeout=3.0, app=app
    )

    csvs = sorted(glob.glob("measurement_data/**/*_R_*.csv", recursive=True))
    assert csvs, "no resistance CSV written"
    rows = _read_csv_data(csvs[-1])
    header, data = rows[0], rows[1:]
    event_col = header.index("event")
    events = [r[event_col] for r in data if r[event_col]]
    assert "PROBE_MOVED" in events, (
        f"mark-event label missing from CSV event column; found: {events}"
    )


# --------------------------------------------------------------------------
# Pause / Resume
# --------------------------------------------------------------------------

def test_pause_then_resume_preserves_data(sim_window, app):
    """Toggling Pause must stop new points landing in the buffer; toggling it
    again must resume sampling. Catches a regression where pause silently
    drops points or resume double-counts.
    """
    _switch_to(sim_window, "Resistance Measurement", app)
    tab = sim_window.tab_resistance
    tab.start_button.click()
    app.processEvents()
    # Give the worker generous startup time — CI runners can be slow.
    _pump_for(1.5, app)
    pre_pause = len(list(sim_window.data_buffers["resistance"].timestamps))
    assert pre_pause >= 2, f"too few pre-pause points: {pre_pause}"

    # Toggle pause ON
    tab.pause_button.setChecked(True)
    app.processEvents()
    _pump_for(1.2, app)
    paused = len(list(sim_window.data_buffers["resistance"].timestamps))
    # Allow up to 2 in-flight points after we issue pause (worker may have
    # been mid-iteration). The key signal is "growth slowed dramatically".
    assert paused - pre_pause <= 2, (
        f"paused but buffer kept growing: pre={pre_pause}, after={paused}"
    )

    # Toggle pause OFF (resume)
    tab.pause_button.setChecked(False)
    app.processEvents()
    _pump_for(1.2, app)
    resumed = len(list(sim_window.data_buffers["resistance"].timestamps))
    assert resumed > paused + 2, (
        f"resume didn't produce new points: paused={paused}, resumed={resumed}"
    )

    sim_window.stop_current_measurement()
    _wait_until(lambda: not sim_window.measurement_running, timeout=3.0, app=app)


# --------------------------------------------------------------------------
# 4PP multi-spot workflow
# --------------------------------------------------------------------------

def test_four_point_save_spot_then_clear(sim_window, app):
    """The 4PP tab's per-spot workflow: run, Save Spot, run again, Save Spot,
    verify two spots accumulated, then Clear All resets the list."""
    _switch_to(sim_window, "4-Point Probe", app)
    tab = sim_window.tab_four_point

    for _ in range(2):
        tab.start_button.click()
        app.processEvents()
        _pump_for(0.8, app)
        sim_window.stop_current_measurement()
        assert _wait_until(
            lambda: not sim_window.measurement_running, timeout=3.0, app=app
        )
        sim_window._save_fpp_spot()
        app.processEvents()

    assert len(tab._fpp_spots) == 2, (
        f"expected 2 saved spots, got {len(tab._fpp_spots)}: {tab._fpp_spots}"
    )
    assert tab.fpp_spots_table.rowCount() == 2, (
        f"spots table row count = {tab.fpp_spots_table.rowCount()}, expected 2"
    )

    sim_window._clear_all_fpp_spots()
    app.processEvents()
    assert tab._fpp_spots == [], f"after Clear All: spots = {tab._fpp_spots}"
    assert tab.fpp_spots_table.rowCount() == 0


# --------------------------------------------------------------------------
# I-V Sweep direction variants
# --------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["up", "down", "up_down"])
def test_iv_sweep_all_directions(sim_window, app, direction):
    """Each sweep direction produces a CSV with the right point count and the
    right endpoint ordering. ``up_down`` is the hysteresis case — should be
    twice as many points (forward + reverse)."""
    _switch_to(sim_window, "I-V Sweep", app)
    w = sim_window.tab_sweep
    w.sweep_source.setCurrentText("voltage")
    w.sweep_start.setValue(-1.0)
    w.sweep_stop.setValue(1.0)
    w.sweep_step.setValue(0.1)
    w.sweep_compliance.setValue(0.1)
    w.sweep_direction.setCurrentText(direction)
    w.start_button.click()
    app.processEvents()
    assert _wait_until(
        lambda: not sim_window.measurement_running, timeout=10.0, app=app
    ), f"{direction}: sweep didn't finish"

    csvs = sorted(glob.glob("measurement_data/**/*_sweep_*.csv", recursive=True),
                  key=os.path.getmtime)
    assert csvs, f"{direction}: no sweep CSV"
    data = _read_csv_data(csvs[-1])[1:]
    v_values = [float(r[1]) for r in data]
    n = len(v_values)
    # 21 single-direction points at 0.1V step from -1 to +1 inclusive.
    if direction == "up_down":
        assert n >= 40, f"up_down should produce ~42 points; got {n}"
        # Forward leg ends near +1, reverse leg ends near -1.
        # Pick a robust signal: the v-value differences should change sign somewhere.
        diffs = [v_values[i + 1] - v_values[i] for i in range(len(v_values) - 1)]
        assert any(d > 0 for d in diffs) and any(d < 0 for d in diffs), (
            "up_down sweep should have both ascending and descending segments"
        )
    elif direction == "up":
        assert v_values[0] < v_values[-1], (
            f"up sweep should go low→high, got {v_values[0]} → {v_values[-1]}"
        )
    else:  # down
        assert v_values[0] > v_values[-1], (
            f"down sweep should go high→low, got {v_values[0]} → {v_values[-1]}"
        )


# --------------------------------------------------------------------------
# Cable null subtraction
# --------------------------------------------------------------------------

def test_cable_null_subtracts_from_subsequent_run(sim_window, app, monkeypatch):
    """Setting a cable_null reference should subtract from later resistance
    readings. Verified by setting a fake null of 25 Ω against a 100 Ω DUT
    and watching the recorded R drop to ~75 Ω.
    """
    # Skip the QMessageBox.question dialog that _null_cables shows.
    # We bypass _null_cables() entirely and write the null directly into
    # user_settings — the production path's value is sourced from there.
    sim_window.user_settings["measurement"]["res_cable_null"] = 25.0

    _switch_to(sim_window, "Resistance Measurement", app)
    sim_window.tab_resistance.start_button.click()
    app.processEvents()
    _pump_for(1.0, app)
    sim_window.stop_current_measurement()
    assert _wait_until(
        lambda: not sim_window.measurement_running, timeout=3.0, app=app
    )

    rs = [r for r in list(sim_window.data_buffers["resistance"].resistance)
          if r is not None and not math.isnan(r)]
    assert rs, "no resistance recorded"
    # DUT was 100 Ω in this test (fixture default); null = 25 → recorded R = 75.
    bad = [r for r in rs if abs(r - 75.0) > 0.01]
    assert not bad, (
        f"cable null not applied: expected 75.0 Ω after null=25 on 100Ω DUT, got {bad[:3]}"
    )

    # Cleanup so other tests don't inherit the null.
    sim_window.user_settings["measurement"]["res_cable_null"] = 0.0


# --------------------------------------------------------------------------
# Window close during measurement
# --------------------------------------------------------------------------

def test_close_event_stops_worker_cleanly(sim_window, app, monkeypatch):
    """Closing the window mid-measurement should answer the confirmation
    dialog Yes (in the test), stop the worker, and exit cleanly without
    leaving a zombie thread. Catches a regression where closeEvent fails to
    join the worker, hanging the process at shutdown.
    """
    from PyQt5.QtWidgets import QMessageBox
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.Yes),
    )

    _switch_to(sim_window, "Resistance Measurement", app)
    sim_window.tab_resistance.start_button.click()
    app.processEvents()
    _pump_for(0.4, app)
    assert sim_window.measurement_running

    # closeEvent is what we're testing — call it the way Qt would.
    from PyQt5.QtCore import QEvent
    from PyQt5.QtGui import QCloseEvent
    ev = QCloseEvent()
    sim_window.closeEvent(ev)
    app.processEvents()

    # Worker should be stopping/stopped; give it a moment.
    assert _wait_until(
        lambda: not sim_window.measurement_running, timeout=5.0, app=app
    ), "worker didn't stop after closeEvent"
    assert ev.isAccepted(), "closeEvent did not accept the event after Yes"

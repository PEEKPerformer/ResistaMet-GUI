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

import glob
import math
import os

import pytest

# Skip the whole module if PySide6 isn't installed (matches test_gui_smoke).
pytest.importorskip("PySide6")

# All tests in this module are end-to-end. The marker is informational —
# pytest.ini ignores this file in the default run; CI runs it explicitly.
pytestmark = pytest.mark.e2e

# Offscreen platform so the tests run in CI without a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Shared harness: the app / sim_window fixtures live in conftest.py; the
# pump/CSV helpers in e2e_utils.py. Aliased to keep test bodies unchanged.
from .e2e_utils import (  # noqa: E402
    newest_csv,
    pump_for as _pump_for,
    read_csv_data as _read_csv_data,
    switch_to as _switch_to,
    wait_until as _wait_until,
)

# Known DUT resistance for Ohm's-law assertions across all modes.
DUT_OHMS = 100.0


def _reset_simulator(ohms: float, model: str = "2420"):
    """Re-call enable_simulation with new DUT params. Tests that need a
    different fake resistance than the fixture default use this."""
    from resistamet_gui.simulator import enable_simulation
    enable_simulation(dut_resistance_ohms=ohms, model=model)


_MODE_KEYS = {
    "Resistance Measurement": "resistance",
    "Voltage Source": "source_v",
    "Current Source": "source_i",
    "4-Point Probe": "four_point",
}


def _points(window, mode_key):
    return len(list(window.data_buffers[mode_key].timestamps))


def _drive_timed_run(window, tab, label, seconds, app):
    """Click Start on a time-series tab, pump events for ``seconds`` and
    until at least three points have landed (a slow runner may need longer),
    then cleanly Stop. Returns the buffers' contents."""
    mode_key = _MODE_KEYS[label]
    _switch_to(window, label, app)
    tab.start_button.click()
    app.processEvents()
    assert window.measurement_running, f"{label}: worker did not start"
    _pump_for(seconds, app)
    _wait_until(lambda: len(list(window.data_buffers[mode_key].timestamps)) >= 3,
                timeout=15.0, app=app)
    window.stop_current_measurement()
    assert _wait_until(lambda: not window.measurement_running, timeout=3.0, app=app), (
        f"{label}: worker did not stop within timeout"
    )
    return list(window.data_buffers[mode_key].timestamps), \
           list(window.data_buffers[mode_key].voltage), \
           list(window.data_buffers[mode_key].current), \
           list(window.data_buffers[mode_key].resistance)


def _finite(values, n, what):
    """Every one of the ``n`` recorded values, which must all be finite: a
    NaN reading fails here rather than slipping past a tolerance check."""
    finite = [x for x in values if x is not None and math.isfinite(x)]
    assert len(finite) == n, (
        f"{what}: {n - len(finite)} of {n} readings missing or not finite")
    return finite


def test_resistance_records_ohms_law(sim_window, app):
    ts, _, _, rs = _drive_timed_run(
        sim_window, sim_window.tab_resistance, "Resistance Measurement",
        seconds=3.0, app=app,
    )
    assert len(ts) >= 3, f"too few points: {len(ts)}"
    finite_rs = _finite(rs, len(ts), "R")
    bad = [r for r in finite_rs if abs(r - DUT_OHMS) > 0.01]
    assert not bad, f"resistance drift: {bad[:3]} (expected {DUT_OHMS})"

    # The buffer keeps only R in this mode; V and I are in the CSV.
    rows = _read_csv_data(newest_csv("measurement_data/**/*_R_*.csv"))
    header, data = rows[0], rows[1:]
    assert data, "no resistance rows written"
    vi, ii = header.index("V_meas"), header.index("I_meas")
    ratios = [float(r[vi]) / float(r[ii]) for r in data]
    bad = [x for x in ratios if not (math.isfinite(x) and abs(x - DUT_OHMS) <= 0.01)]
    assert not bad, f"V_meas/I_meas != {DUT_OHMS} Ω: {bad[:3]}"


def test_voltage_source_records_correct_current(sim_window, app):
    # Default sourced voltage is 1.0 V into 100 Ω → I = 10 mA.
    ts, vs, is_, _ = _drive_timed_run(
        sim_window, sim_window.tab_voltage_source, "Voltage Source",
        seconds=3.0, app=app,
    )
    assert len(ts) >= 3, f"too few points: {len(ts)}"
    bad_v = [v for v in _finite(vs, len(ts), "V") if abs(v - 1.0) > 1e-3]
    bad_i = [i for i in _finite(is_, len(ts), "I") if abs(i - 0.01) > 1e-5]
    assert not bad_v, f"V drift: {bad_v[:3]}"
    assert not bad_i, f"I drift (expected 10 mA): {bad_i[:3]}"


def test_current_source_records_correct_voltage(sim_window, app):
    # Default sourced current is 1 mA into 100 Ω → V = 0.1 V.
    ts, vs, is_, _ = _drive_timed_run(
        sim_window, sim_window.tab_current_source, "Current Source",
        seconds=3.0, app=app,
    )
    assert len(ts) >= 3, f"too few points: {len(ts)}"
    bad_v = [v for v in _finite(vs, len(ts), "V") if abs(v - 0.1) > 1e-4]
    bad_i = [i for i in _finite(is_, len(ts), "I") if abs(i - 1e-3) > 1e-7]
    assert not bad_v, f"V drift (expected 0.1 V): {bad_v[:3]}"
    assert not bad_i, f"I drift: {bad_i[:3]}"


def test_four_point_probe_records_v_i_at_source(sim_window, app):
    # 4PP sources current, measures voltage. With DUT = 100 Ω and the form's
    # configured source current, V should equal I_src × 100. The run is
    # 3.0 s rather than 1.5 s because 4PP now applies the accuracy-mode
    # timing override (auto_zero=on, filter_count=10) which caps the
    # sustainable rate at ~1.7 Hz — so ≥3 points needs at least ~1.8 s
    # of run time plus the initial settling delay.
    ts, vs, is_, _ = _drive_timed_run(
        sim_window, sim_window.tab_four_point, "4-Point Probe",
        seconds=3.0, app=app,
    )
    assert len(ts) >= 3, f"too few points: {len(ts)}"
    src_i = sim_window.tab_four_point.fpp_current.value()
    expected_v = src_i * DUT_OHMS
    bad_v = [v for v in _finite(vs, len(ts), "V") if abs(v - expected_v) > 1e-4]
    bad_i = [i for i in _finite(is_, len(ts), "I") if abs(i - src_i) > 1e-7]
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
        timeout=30.0, app=app,
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

#: The documented column header of each time-series mode, written out here
#: rather than read from ``get_column_config``, which is what writes it.
_DOCUMENTED_HEADERS = {
    "resistance": ["elapsed_s", "V_meas", "I_meas", "R_ohm", "R_unc_ohm",
                   "compliance", "event"],
    "source_v": ["elapsed_s", "V_set", "I_meas", "R_calc", "I_unc_A",
                 "R_calc_unc_ohm", "compliance", "event"],
    "source_i": ["elapsed_s", "V_meas", "I_set", "R_calc", "V_unc_V",
                 "R_calc_unc_ohm", "compliance", "event"],
    "four_point": ["elapsed_s", "V", "I", "V_over_I", "Rs_ohm_sq", "rho_ohm_cm",
                   "sigma_S_cm", "V_unc_V", "I_unc_A", "compliance", "event"],
}


def _csv_columns(path):
    """The data rows of a CSV as {column name: [float, ...]}, with the
    compliance column kept as text."""
    rows = _read_csv_data(path)
    header, data = rows[0], rows[1:]
    assert data, f"{path}: no data rows"
    cols = {}
    for k, name in enumerate(header):
        if name in ("compliance", "event"):
            cols[name] = [r[k] for r in data]
        else:
            cols[name] = [float(r[k]) if r[k] not in ("",) else float("nan")
                          for r in data]
    return header, cols


def _all_close(values, expected, rel, what):
    bad = [v for v in values if not math.isfinite(v)
           or abs(v - expected) > rel * abs(expected)]
    assert not bad, f"{what}: expected {expected}, got {bad[:3]} of {len(values)}"


def _all_positive(values, what):
    bad = [v for v in values if not (math.isfinite(v) and v > 0)]
    assert not bad, f"{what}: expected finite and > 0, got {bad[:3]}"


def _check_csv_values(mode, cols, fpp_current):
    """The values under each header are the simulated 100 Ω DUT's."""
    assert set(cols["compliance"]) == {"OK"}, f"{mode}: {set(cols['compliance'])}"
    elapsed = cols["elapsed_s"]
    assert all(b > a for a, b in zip(elapsed, elapsed[1:])), (
        f"{mode}: elapsed_s not increasing: {elapsed[:5]}")
    if mode == "resistance":
        # Default test current 1 mA into 100 Ω: V = 0.1 V.
        _all_close(cols["V_meas"], 0.1, 1e-6, "resistance V_meas")
        _all_close(cols["I_meas"], 1e-3, 1e-6, "resistance I_meas")
        _all_close(cols["R_ohm"], DUT_OHMS, 1e-6, "resistance R_ohm")
        _all_positive(cols["R_unc_ohm"], "resistance R_unc_ohm")
    elif mode == "source_v":
        # Default 1 V into 100 Ω: I = 10 mA.
        _all_close(cols["V_set"], 1.0, 1e-6, "source_v V_set")
        _all_close(cols["I_meas"], 0.01, 1e-6, "source_v I_meas")
        _all_close(cols["R_calc"], DUT_OHMS, 1e-6, "source_v R_calc")
        _all_positive(cols["I_unc_A"], "source_v I_unc_A")
        _all_positive(cols["R_calc_unc_ohm"], "source_v R_calc_unc_ohm")
    elif mode == "source_i":
        # Default 1 mA into 100 Ω: V = 0.1 V.
        _all_close(cols["V_meas"], 0.1, 1e-6, "source_i V_meas")
        _all_close(cols["I_set"], 1e-3, 1e-6, "source_i I_set")
        _all_close(cols["R_calc"], DUT_OHMS, 1e-6, "source_i R_calc")
        _all_positive(cols["V_unc_V"], "source_i V_unc_V")
        _all_positive(cols["R_calc_unc_ohm"], "source_i R_calc_unc_ohm")
    else:  # four_point, legacy thin-film path with the default K = 4.532
        _all_close(cols["I"], fpp_current, 1e-6, "4PP I")
        _all_close(cols["V"], fpp_current * DUT_OHMS, 1e-6, "4PP V")
        _all_close(cols["V_over_I"], DUT_OHMS, 1e-6, "4PP V_over_I")
        _all_close(cols["Rs_ohm_sq"], 4.532 * DUT_OHMS, 1e-6, "4PP Rs_ohm_sq")
        _all_positive(cols["V_unc_V"], "4PP V_unc_V")
        _all_positive(cols["I_unc_A"], "4PP I_unc_A")


def test_csv_headers_match_documented_schema(sim_window, app):
    """Each mode's saved CSV carries the documented column names, and the
    values under them are the simulated DUT's: V under the V column, I
    under the I column, R in ohms. A unit-confusion regression (swapping
    I_meas/V_meas, or renaming R_ohm to R_mohm) fails here.
    """
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
        assert _wait_until(lambda: _points(sim_window, mode) >= 2, timeout=15.0, app=app), (
            f"{label}: fewer than 2 points within 15 s")
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
    fpp_current = sim_window.tab_four_point.fpp_current.value()
    found_modes = set()
    for path in files:
        mode = next((m for tag, m in tag_to_mode.items() if tag in path), None)
        if mode is None:
            continue
        expected_cols = _DOCUMENTED_HEADERS[mode]
        header, cols = _csv_columns(path)
        assert header == expected_cols, (
            f"{path}: header {header} != expected {expected_cols} for {mode}"
        )
        _check_csv_values(mode, cols, fpp_current)
        found_modes.add(mode)
    # All four time-series modes should have been covered.
    assert {"resistance", "source_v", "source_i", "four_point"} <= found_modes, (
        f"missing CSV coverage; found modes: {found_modes}"
    )


# --------------------------------------------------------------------------
# Compliance handling end-to-end
# --------------------------------------------------------------------------

def test_voltage_compliance_clamps_and_flags(sim_window, app):
    """When the current compliance is set below what the sourced voltage
    into the DUT would draw, the instrument clamps the current and sets
    STAT bit 3. The worker records ``compliance_status='I_COMP'`` on every
    clamped point.
    """
    # 10kΩ DUT + 1 mA sourced → V would naturally be 10 V; clamp to 1 V.
    _reset_simulator(ohms=10_000.0)
    _switch_to(sim_window, "Voltage Source", app)
    w = sim_window.tab_voltage_source
    w.vsource_voltage.setValue(10.0)            # 10 V into 10 kΩ → 1 mA
    w.vsource_current_compliance.setValue(1e-4) # but compliance is 100 µA
    w.start_button.click()
    app.processEvents()
    assert _wait_until(
        lambda: len(list(sim_window.data_buffers["source_v"].compliance_status)) >= 2,
        timeout=15.0, app=app,
    ), "no source_v points landed"
    sim_window.stop_current_measurement()
    assert _wait_until(
        lambda: not sim_window.measurement_running, timeout=3.0, app=app
    )

    buf = sim_window.data_buffers["source_v"]
    statuses = list(buf.compliance_status)
    assert statuses, "no compliance status recorded"
    # Every point is clamped from the first, and source-V compliance is a
    # current limit: each one is I_COMP, no other label.
    assert set(statuses) == {"I_COMP"}, (
        f"expected I_COMP on all {len(statuses)} points; got {set(statuses)}"
    )
    # And the recorded current shouldn't exceed compliance by more than rounding.
    currents = [i for i in list(buf.current) if i is not None]
    assert all(abs(i) <= 1.1e-4 for i in currents), (
        f"current exceeded compliance: max={max(map(abs, currents))}"
    )


def test_source_v_compliance_is_read_from_the_status_bit(sim_window, app, monkeypatch):
    """The instrument's compliance bit alone flags a point I_COMP.

    The fake here sets STAT bit 3 while the measured current stays at
    10 mA, a tenth of the 100 mA limit, so the worker's software check on
    the current (>= 0.99 x limit) cannot fire: only the parsed bit can.
    """
    from resistamet_gui._simulator import FakeKeithley

    real = FakeKeithley._compute_one_point

    def always_in_compliance(self, source_value):
        v, i, r, _ = real(self, source_value)
        return v, i, r, True

    monkeypatch.setattr(FakeKeithley, "_compute_one_point", always_in_compliance)

    _switch_to(sim_window, "Voltage Source", app)
    w = sim_window.tab_voltage_source
    w.vsource_voltage.setValue(1.0)               # 1 V into 100 Ω → 10 mA
    w.vsource_current_compliance.setValue(0.1)    # limit 100 mA
    w.start_button.click()
    app.processEvents()
    assert _wait_until(
        lambda: _points(sim_window, "source_v") >= 2, timeout=15.0, app=app,
    ), "no source_v points landed"
    sim_window.stop_current_measurement()
    assert _wait_until(
        lambda: not sim_window.measurement_running, timeout=3.0, app=app
    )

    buf = sim_window.data_buffers["source_v"]
    currents = _finite(buf.current, len(buf.timestamps), "I")
    assert all(abs(i - 0.01) < 1e-5 for i in currents), currents[:3]
    statuses = list(buf.compliance_status)
    assert set(statuses) == {"I_COMP"}, (
        f"STAT bit 3 set on every point, but statuses were {set(statuses)}"
    )


# --------------------------------------------------------------------------
# Mark Event during a run
# --------------------------------------------------------------------------

def test_mark_event_lands_in_csv(sim_window, app, monkeypatch):
    """``mark_event_shortcut`` prompts for a label via QInputDialog; tests
    monkey-patch that to return a fixed label. The label must then appear in
    the ``event`` column of at least one row of the saved CSV.
    """
    from PySide6.QtWidgets import QInputDialog
    monkeypatch.setattr(
        QInputDialog, "getText",
        staticmethod(lambda *a, **kw: ("PROBE_MOVED", True)),
    )

    _switch_to(sim_window, "Resistance Measurement", app)
    sim_window.tab_resistance.start_button.click()
    app.processEvents()
    assert _wait_until(lambda: _points(sim_window, "resistance") >= 1, timeout=15.0, app=app)
    sim_window.mark_event_shortcut()
    marked_at = _points(sim_window, "resistance")
    _pump_for(0.7, app)
    assert _wait_until(lambda: _points(sim_window, "resistance") >= marked_at + 2,
                       timeout=15.0, app=app), "no rows after the mark"
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
    # Wait for the points rather than assume a rate; CI runners can be slow.
    assert _wait_until(
        lambda: len(list(sim_window.data_buffers["resistance"].timestamps)) >= 2,
        timeout=15.0, app=app,
    ), "too few pre-pause points"
    pre_pause = len(list(sim_window.data_buffers["resistance"].timestamps))

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
    assert _wait_until(
        lambda: len(list(sim_window.data_buffers["resistance"].timestamps)) > paused + 2,
        timeout=15.0, app=app,
    ), f"resume didn't produce new points: paused={paused}, now={len(list(sim_window.data_buffers['resistance'].timestamps))}"

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
        # 4PP runs in accuracy mode (~0.6 s per sample). Wait for a reading
        # rather than assume a rate: Save Spot has nothing to save otherwise.
        assert _wait_until(
            lambda: len(getattr(tab, "_fpp_rows", [])) >= 1, timeout=15.0, app=app
        ), "no 4PP reading landed"
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


def test_four_point_spots_are_linked_in_their_files(sim_window, app):
    """Two placements from the window are two files of one map: the same
    ``map_id``, the index and name the spot table shows, and a map summary
    beside them. Clear All ends the map."""
    import glob
    import json

    from resistamet_gui.data_export import parse_metadata
    _switch_to(sim_window, "4-Point Probe", app)
    tab = sim_window.tab_four_point

    def measure_spot():
        before = set(glob.glob("measurement_data/**/*_4PP_*.csv", recursive=True))
        saved = tab.fpp_spots_table.rowCount()
        tab.start_button.click()
        app.processEvents()
        # Wait for a reading rather than assume a rate: a Save Spot with no
        # rows saves nothing, and the next run would still be spot 1.
        assert _wait_until(
            lambda: len(getattr(tab, "_fpp_rows", [])) >= 1, timeout=15.0, app=app
        ), "no 4PP reading landed"
        sim_window.stop_current_measurement()
        assert _wait_until(
            lambda: not sim_window.measurement_running, timeout=3.0, app=app
        )
        new = set(glob.glob("measurement_data/**/*_4PP_*.csv", recursive=True)) - before
        assert len(new) == 1, f"expected one new 4PP file, got {sorted(new)}"
        sim_window._save_fpp_spot()
        app.processEvents()
        assert tab.fpp_spots_table.rowCount() == saved + 1
        return parse_metadata(new.pop(), text_keys=("spot.map_id", "spot.label"))

    tab.fpp_spot_name.setText("centre")
    first = measure_spot()
    second = measure_spot()

    assert first["spot.map_id"] == second["spot.map_id"]
    assert (first["spot.index"], first["spot.label"]) == (1, "centre")
    assert (second["spot.index"], second["spot.label"]) == (2, "Spot 2")
    assert [tab.fpp_spots_table.item(row, 0).text() for row in range(2)] == ["centre", "Spot 2"]

    summaries = glob.glob(f"measurement_data/**/{first['spot.map_id']}_map.json", recursive=True)
    assert len(summaries) == 1
    with open(summaries[0]) as f:
        assert [spot["index"] for spot in json.load(f)["spots"]] == [1, 2]

    sim_window._clear_all_fpp_spots()
    third = measure_spot()
    assert third["spot.map_id"] != first["spot.map_id"]
    assert third["spot.index"] == 1


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
    assert _wait_until(lambda: _points(sim_window, "resistance") >= 2, timeout=15.0, app=app), (
        "no resistance within 15 s")
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
    from PySide6.QtWidgets import QMessageBox
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
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QCloseEvent
    ev = QCloseEvent()
    sim_window.closeEvent(ev)
    app.processEvents()

    # Worker should be stopping/stopped; give it a moment.
    assert _wait_until(
        lambda: not sim_window.measurement_running, timeout=5.0, app=app
    ), "worker didn't stop after closeEvent"
    assert ev.isAccepted(), "closeEvent did not accept the event after Yes"

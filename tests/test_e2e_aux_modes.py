"""End-to-end: aux co-logging is mode-agnostic — proven on a non-4PP run.

Runs a *resistance* measurement under --simulate with aux logging enabled and
asserts the saved CSV gains aux_* columns (+ aux_fault provenance). This is
the "Keithley + other data sources" generality: the same co-logging path
works on any continuous mode, not just 4-point-probe.
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.e2e
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from .e2e_utils import (  # noqa: E402
    csv_header,
    newest_csv,
    pump_for,
    switch_to,
    wait_until,
)


def _run_resistance(window, app, seconds=2.0):
    switch_to(window, "Resistance Measurement", app)
    tab = window.tab_resistance
    tab.start_button.click()
    app.processEvents()
    assert window.measurement_running, "resistance run did not start"
    pump_for(seconds, app)
    window.stop_current_measurement()
    assert wait_until(lambda: not window.measurement_running, timeout=3.0, app=app)


def test_resistance_run_co_logs_aux_columns(sim_window, app):
    sim_window.user_settings['measurement']['aux_log_enabled'] = True
    _run_resistance(sim_window, app, seconds=2.0)

    header = csv_header(newest_csv())
    assert "R_ohm" in header, f"not a resistance CSV? {header}"
    assert "aux_t_sample" in header, f"aux columns missing on resistance run: {header}"
    assert "aux_fault" in header, f"aux_fault missing on resistance run: {header}"
    assert header.index("aux_t_sample") < header.index("compliance")
    assert header.index("aux_fault") < header.index("compliance")


def test_resistance_schema_unchanged_with_aux_off(sim_window, app):
    from resistamet_gui.data_export import get_column_config
    baseline, _ = get_column_config("resistance")

    sim_window.user_settings['measurement']['aux_log_enabled'] = False
    _run_resistance(sim_window, app, seconds=1.5)

    header = csv_header(newest_csv())
    assert header == baseline, f"resistance schema drifted with aux off: {header}"
    assert not any(c.startswith("aux_") for c in header)

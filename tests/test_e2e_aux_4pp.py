"""End-to-end: 4PP co-logging an auxiliary sensor under --simulate.

Drives the real Start → MeasurementWorker → FakeSerialSensor → exporter seam
and asserts the saved CSV gains aux_* value columns plus the aux_fault
provenance column when logging is on, and is schema-identical to a normal
4PP run when it is off.
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.e2e
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from .conftest import E2E_SIM_TEMP_C  # noqa: E402
from .e2e_utils import (  # noqa: E402
    csv_header,
    newest_csv,
    pump_for,
    read_csv_data,
    switch_to,
    wait_until,
)

SIM_TEMP_C = E2E_SIM_TEMP_C


def _enable_aux(window):
    window.user_settings['measurement']['aux_log_enabled'] = True


def _run_4pp(window, app, seconds=3.0):
    switch_to(window, "4-Point Probe", app)
    tab = window.tab_four_point
    tab.start_button.click()
    app.processEvents()
    assert window.measurement_running, "4PP worker did not start"
    pump_for(seconds, app)
    window.stop_current_measurement()
    assert wait_until(lambda: not window.measurement_running, timeout=3.0, app=app)


def _newest_4pp_csv():
    return newest_csv("measurement_data/**/*_4PP_*.csv")


def test_aux_columns_present_with_plausible_values(sim_window, app):
    _enable_aux(sim_window)
    _run_4pp(sim_window, app, seconds=3.0)

    rows = read_csv_data(_newest_4pp_csv())
    header, data = rows[0], rows[1:]
    assert "aux_t_sample" in header, f"missing aux_t_sample in header: {header}"
    assert "aux_t_coldjunction" in header, f"missing aux_t_coldjunction: {header}"
    assert "aux_fault" in header, f"missing aux_fault provenance column: {header}"
    assert data, "no 4PP data rows written"

    ti = header.index("aux_t_sample")
    temps = [float(r[ti]) for r in data if r[ti] not in ("", "nan", "NaN")]
    assert temps, "no aux temperature values recorded"
    bad = [t for t in temps if abs(t - SIM_TEMP_C) > 2.0]
    assert not bad, f"aux temps implausible (expected ~{SIM_TEMP_C}): {bad[:3]}"

    # Healthy simulated stream → every row's fault provenance is "0".
    fi = header.index("aux_fault")
    faults = {r[fi] for r in data}
    assert faults == {"0"}, f"expected clean aux_fault column, got {faults}"


def test_aux_columns_land_before_compliance(sim_window, app):
    _enable_aux(sim_window)
    _run_4pp(sim_window, app, seconds=2.5)
    header = csv_header(_newest_4pp_csv())
    # aux value columns + aux_fault splice in just before compliance/event.
    assert header.index("aux_t_sample") < header.index("compliance")
    assert header.index("aux_t_coldjunction") < header.index("compliance")
    assert header.index("aux_fault") < header.index("compliance")
    # aux_fault is the LAST aux column (values first, provenance last).
    assert header.index("aux_fault") > header.index("aux_t_coldjunction")


def test_logging_off_leaves_schema_unchanged(sim_window, app):
    from resistamet_gui.data_export import get_column_config
    baseline, _ = get_column_config("four_point")

    sim_window.user_settings['measurement']['aux_log_enabled'] = False
    _run_4pp(sim_window, app, seconds=2.5)
    header = csv_header(_newest_4pp_csv())
    assert header == baseline, (
        f"4PP schema drifted with logging off: {header} != {baseline}"
    )
    assert not any(c.startswith("aux_") for c in header)

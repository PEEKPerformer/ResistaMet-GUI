"""End-to-end: the live auxiliary-sensor readout on the 4PP tab.

Verifies the equilibration-watch lifecycle under --simulate: an idle preview
opens the sensor and shows a value; tab switches do NOT close/reopen the
port (no DTR reset churn — the reader thread keeps caching); Start hands the
serial port off to the worker; the readout keeps updating during the run
from data_point (for any mode); and the preview resumes once the run ends.
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.e2e
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from .e2e_utils import pump_for, switch_to, wait_until  # noqa: E402


def _enable_aux_on_4pp_tab(window, app):
    window.user_settings['measurement']['aux_log_enabled'] = True
    switch_to(window, "4-Point Probe", app)   # tab-change reconciles the preview
    window._maybe_start_aux_preview()          # explicit; must be a true no-op if on
    app.processEvents()


def test_idle_preview_shows_temperature(sim_window, app):
    _enable_aux_on_4pp_tab(sim_window, app)
    assert sim_window._aux_preview_sensor is not None, "preview did not open"

    tab = sim_window.tab_four_point
    # The reader thread needs a beat to cache the first line; the running
    # QTimer repaints as soon as it lands.
    assert wait_until(lambda: "°C" in tab.fpp_temp_readout.text(),
                      timeout=3.0, app=app), (
        f"no live value in readout: {tab.fpp_temp_readout.text()!r}")
    # First number in the readout is the K-type tip — the sim streams ~25 °C
    # with noise, so parse rather than substring-match.
    import re
    text = tab.fpp_temp_readout.text()
    m = re.search(r"(-?\d+(?:\.\d+)?)", text)
    assert m, f"no numeric value in readout: {text!r}"
    assert abs(float(m.group(1)) - 25.0) < 2.0, f"expected ~25 C: {text!r}"


def test_preview_not_reopened_when_config_unchanged(sim_window, app):
    """_maybe_start_aux_preview with unchanged (driver, address) must keep the
    SAME sensor object — a close/reopen would DTR-reset a native-USB board
    into its ~2 s dead window on every tab flip or settings accept."""
    _enable_aux_on_4pp_tab(sim_window, app)
    first = sim_window._aux_preview_sensor
    assert first is not None

    sim_window._maybe_start_aux_preview()
    app.processEvents()
    assert sim_window._aux_preview_sensor is first, "preview was reopened needlessly"


def test_tab_switch_keeps_port_open(sim_window, app):
    """Leaving the 4PP tab pauses the repaint timer but keeps the port (and
    reader thread) alive; returning resumes with the same sensor object."""
    _enable_aux_on_4pp_tab(sim_window, app)
    sensor = sim_window._aux_preview_sensor
    assert sensor is not None

    switch_to(sim_window, "Resistance Measurement", app)
    assert sim_window._aux_preview_sensor is sensor, "tab leave closed the port"
    assert not sim_window._aux_monitor_timer.isActive(), (
        "repaint timer should pause off the 4PP tab")

    switch_to(sim_window, "4-Point Probe", app)
    assert sim_window._aux_preview_sensor is sensor, "tab return reopened the port"
    assert sim_window._aux_monitor_timer.isActive(), (
        "repaint timer should resume on the 4PP tab")


def test_start_releases_preview_and_run_feeds_readout(sim_window, app):
    _enable_aux_on_4pp_tab(sim_window, app)
    assert sim_window._aux_preview_sensor is not None

    tab = sim_window.tab_four_point
    tab.start_button.click()
    app.processEvents()
    assert sim_window.measurement_running, "4PP run did not start"
    # Port handed off: the preview handle must be released before the worker
    # opens its own.
    assert sim_window._aux_preview_sensor is None, "preview not released on Start"

    assert wait_until(
        lambda: any(ch.isdigit() for ch in tab.fpp_temp_readout.text()),
        timeout=4.0, app=app,
    ), f"no live value during run: {tab.fpp_temp_readout.text()!r}"

    sim_window.stop_current_measurement()
    assert wait_until(lambda: not sim_window.measurement_running, timeout=3.0, app=app)
    # Preview resumes once the port is free again.
    assert wait_until(lambda: sim_window._aux_preview_sensor is not None,
                      timeout=2.0, app=app), "preview did not resume after run"


def test_disable_stops_preview_and_releases_port(sim_window, app):
    _enable_aux_on_4pp_tab(sim_window, app)
    assert sim_window._aux_preview_sensor is not None

    sim_window.user_settings['measurement']['aux_log_enabled'] = False
    sim_window._maybe_start_aux_preview()   # settings-accept / user-switch path
    app.processEvents()
    assert sim_window._aux_preview_sensor is None, "disable did not release the port"
    assert sim_window.tab_four_point.fpp_temp_readout.text() == "Aux sensor: off"


# ---------------------------------------------------------------------------
# Aux connect failures must not trigger the SMU's GPIB-address remediation.
# ---------------------------------------------------------------------------

def test_is_address_error_ignores_aux_failures():
    from resistamet_gui.ui.main_window import ResistanceMeterApp

    is_addr = ResistanceMeterApp._is_address_error
    # SMU failures still light up the selector...
    assert is_addr("Instrument at GPIB0::24::INSTR was not detected. Check...")
    assert is_addr("No instrument responded at GPIB0::24::INSTR.")
    # ...but the worker prefixes aux failures, which must be excluded even
    # though the humanized text contains the same trigger substrings.
    assert not is_addr(
        "Auxiliary sensor: Instrument at ASRL6::INSTR was not detected. "
        "Check that it's powered on..."
    )
    assert not is_addr("AUXILIARY SENSOR: no instrument responded at ASRL6::INSTR")

"""Shared fixtures.

The ``fake_instrument`` fixture monkey-patches ``pyvisa.ResourceManager``
so that any code under test that opens ``GPIB0::24::INSTR`` (or whatever
address it has been configured with) gets a stateful FakeKeithley instead
of a real instrument. Tests can mutate ``fake_instrument.dut_resistance``
or call ``fake_instrument.fail_next_query()`` to exercise edge cases.
"""
from __future__ import annotations

from typing import Iterator

import pyvisa
import pytest

from .fakes.fake_keithley import FakeKeithley, FakeResourceManager


@pytest.fixture
def fake_rm(monkeypatch) -> FakeResourceManager:
    """Replace ``pyvisa.ResourceManager`` with a FakeResourceManager.

    Any code path under test that does ``pyvisa.ResourceManager()`` will
    transparently get the fake. The fake exposes ``opened`` so tests can
    reach into the FakeKeithley instances that were opened.
    """
    rm = FakeResourceManager()

    def _factory(*args, **kwargs):
        return rm

    monkeypatch.setattr(pyvisa, "ResourceManager", _factory)
    return rm


@pytest.fixture
def fake_instrument(fake_rm) -> Iterator[FakeKeithley]:
    """A pre-opened FakeKeithley for tests that drive it directly.

    For tests that want to exercise the real ``Keithley2400.connect()`` path
    while still hitting a fake, depend on ``fake_rm`` instead and let the
    code under test open its own resource.
    """
    dev = fake_rm.open_resource("GPIB0::24::INSTR")
    try:
        yield dev
    finally:
        dev.close()


# ---------------------------------------------------------------------------
# Shared end-to-end (--simulate) harness. One copy of the window/bootstrap
# machinery for every e2e module — see tests/e2e_utils.py for the pump/CSV
# helpers that pair with these fixtures.
# ---------------------------------------------------------------------------

E2E_DUT_OHMS = 100.0
E2E_SIM_TEMP_C = 25.0


@pytest.fixture(scope="session")
def app():
    """Session-wide offscreen QApplication for e2e tests."""
    pytest.importorskip("PySide6")
    import os
    import sys
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def sim_window_factory(app, tmp_path, monkeypatch):
    """Factory for a ResistanceMeterApp wired to the in-package simulator.

    Call with optional ``sample_name`` and ``enable_simulation`` kwargs
    (``dut_resistance_ohms``, ``sim_temp_c``, ...). cwd is tmp_path so
    measurement_data/ writes land in a per-test directory. All windows are
    torn down at test end: measurement stopped, aux preview released, closed.
    """
    from .e2e_utils import wait_until

    created = []

    def make(sample_name="E2E-DUT", **sim_kwargs):
        from resistamet_gui.simulator import enable_simulation
        sim_kwargs.setdefault("dut_resistance_ohms", E2E_DUT_OHMS)
        sim_kwargs.setdefault("model", "2420")
        sim_kwargs.setdefault("sim_temp_c", E2E_SIM_TEMP_C)
        enable_simulation(**sim_kwargs)

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
        window.sample_input.setText(sample_name)
        window.show()
        app.processEvents()
        created.append(window)
        return window

    yield make

    for window in created:
        if window.measurement_running:
            window.stop_current_measurement()
            wait_until(lambda: not window.measurement_running, timeout=3.0, app=app)
        window._stop_aux_preview()
        window.close()


@pytest.fixture
def sim_window(sim_window_factory):
    """A default simulator-backed window (100 Ω DUT, model 2420)."""
    return sim_window_factory()

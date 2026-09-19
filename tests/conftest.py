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


@pytest.fixture(autouse=True)
def _undo_simulation() -> Iterator[None]:
    """No test leaves the simulator bound to ``pyvisa`` for the next one.

    ``enable_simulation`` patches the module attribute process-wide, which is
    right for the app and wrong for a test session: one e2e test would leave
    every later test measuring the fake instead of whatever it meant to.
    Autouse so a new fixture cannot forget; a no-op when nothing enabled it.
    """
    yield
    from resistamet_gui.simulator import disable_simulation

    disable_simulation()


@pytest.fixture(autouse=True)
def _private_instrument_locks(request, tmp_path_factory, monkeypatch) -> None:
    """Every test takes its instrument locks in a directory of its own.

    The real location is one per machine on purpose, so that two ResistaMet
    processes exclude each other. A test session is not a second ResistaMet:
    two suites running at once (two checkouts, CI shards, an editor's test
    runner) would refuse each other's simulated ``GPIB0::24`` and fail with
    "in use by another process". A test of the shared location itself opts
    out with ``@pytest.mark.shared_lock_dir``.
    """
    from resistamet_gui.session import instrument_lock

    if request.node.get_closest_marker('shared_lock_dir'):
        return
    locks = tmp_path_factory.mktemp('locks')
    monkeypatch.setattr(instrument_lock, 'default_lock_dir', lambda: locks)


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
def main_window(app, tmp_path, monkeypatch):
    """A ResistanceMeterApp on a temp config, with no instrument involved.

    The widget-level counterpart to ``sim_window``: no simulator, no run, just
    the window and its tabs. One copy lives here so the config isolation below
    is not reimplemented per module.
    """
    pytest.importorskip("PySide6")
    from resistamet_gui import constants
    monkeypatch.setattr(constants, "CONFIG_FILE", str(tmp_path / "config.json"))

    from resistamet_gui.config import ConfigManager
    from resistamet_gui.ui import main_window as main_window_module
    from resistamet_gui.ui.main_window import ResistanceMeterApp

    # Patching constants.CONFIG_FILE is not enough: ConfigManager binds it as
    # a default argument at import time, so whichever path was live when
    # resistamet_gui.config was first imported wins for the whole session —
    # the real config.json when another test module imported it first.
    config_path = str(tmp_path / "config.json")
    monkeypatch.setattr(
        main_window_module, 'ConfigManager',
        lambda *args, **kwargs: ConfigManager(config_file=config_path),
    )

    # Bypass the modal user selection dialog in __init__
    monkeypatch.setattr(ResistanceMeterApp, 'select_user', lambda self: None)

    window = ResistanceMeterApp()

    # Simulate user selection manually
    window.config_manager.add_user("test_user")
    window.current_user = "test_user"
    window.user_label.setText("User: test_user")
    window.user_settings = window.config_manager.get_user_settings("test_user")
    window.update_ui_from_settings()

    yield window

    window.close()


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

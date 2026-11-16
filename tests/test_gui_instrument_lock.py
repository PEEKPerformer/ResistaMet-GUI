"""The window stays off a bus another process is measuring on.

Cable null sends ``*RST`` and ``:OUTP ON``; the connection test sends
``*IDN?``. Either one landing in the middle of a sidecar's run corrupts
that run, so both take the per-address instrument lock a run holds and
are refused while someone else has it.
"""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QMessageBox

from resistamet_gui.session.instrument_lock import hold_instrument

ADDRESS = 'GPIB0::24::INSTR'


@pytest.fixture
def shown(monkeypatch):
    """Answer OK to every message box and keep what each one said."""
    boxes = []

    def _box(parent, title, text, *args, **kwargs):
        boxes.append((title, text))
        return QMessageBox.Ok

    for name in ('information', 'warning', 'critical', 'question'):
        monkeypatch.setattr(QMessageBox, name, staticmethod(_box))
    return boxes


class _CountingRM:
    """Counts every touch of the bus, including the resource listing."""

    def __init__(self, rm):
        self._rm = rm
        self.listed = 0

    def list_resources(self):
        self.listed += 1
        return self._rm.list_resources()

    def open_resource(self, *args, **kwargs):
        return self._rm.open_resource(*args, **kwargs)


@pytest.fixture
def bus(fake_rm, monkeypatch):
    import pyvisa
    counting = _CountingRM(fake_rm)
    monkeypatch.setattr(pyvisa, 'ResourceManager', lambda *args: counting)
    counting.opened = fake_rm.opened
    return counting


class TestHeldByAnotherProcess:
    def test_cable_null_writes_nothing(self, main_window, bus, shown):
        with hold_instrument(ADDRESS):
            main_window._null_cables()
        assert bus.opened == [] and bus.listed == 0
        assert shown[-1] == ("Busy", f"{ADDRESS} is in use by another ResistaMet process")
        assert main_window.user_settings['measurement'].get('res_cable_null', 0.0) == 0.0

    def test_the_connection_test_writes_nothing(self, main_window, bus, shown):
        with hold_instrument(ADDRESS):
            main_window.test_instrument_connection()
        assert bus.opened == [] and bus.listed == 0
        assert shown[-1] == ("Busy", f"{ADDRESS} is in use by another ResistaMet process")


class TestFree:
    def test_cable_null_measures_and_lets_go(self, main_window, bus, shown):
        main_window._null_cables()
        (instrument,) = bus.opened
        assert ('write', ':OUTP ON') in instrument.command_log
        assert instrument.command_log[-1] == ('write', ':OUTP OFF')
        assert main_window.user_settings['measurement']['res_cable_null'] > 0
        with hold_instrument(ADDRESS, wait_s=0):
            pass

    def test_the_connection_test_asks_and_lets_go(self, main_window, bus, shown):
        main_window.test_instrument_connection()
        (instrument,) = bus.opened
        assert ('query', '*IDN?') in instrument.command_log
        assert shown[-1][0] == "Connection OK"
        with hold_instrument(ADDRESS, wait_s=0):
            pass

    def test_a_failed_cable_null_lets_go(self, main_window, bus, shown, fake_rm):
        fake_rm.fail_next_open(1)
        main_window._null_cables()
        assert shown[-1][0] == "Null Failed"
        with hold_instrument(ADDRESS, wait_s=0):
            pass

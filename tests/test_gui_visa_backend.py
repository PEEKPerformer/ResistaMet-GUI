"""Scan and Test open the bus the way a run does.

A machine set to pyvisa-py reaches its instrument through pyvisa-py only.
If Detect Devices or Test Connection opens pyvisa's default instead, the
operator is told "not found" about an instrument the run can reach.
"""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDialog, QMessageBox

from resistamet_gui import visa_backend
from resistamet_gui.ui.dialogs import SettingsDialog

INTERFACE = 'PRLGX-ASRL::/dev/cu.usbserial-PX1::INTFC'


@pytest.fixture
def opened(fake_rm, monkeypatch):
    """Record what ``visa_backend.resource_manager`` is asked for."""
    calls = []

    def _resource_manager(visa_library='', gpib_interface=''):
        calls.append((visa_library, gpib_interface))
        return fake_rm

    monkeypatch.setattr(visa_backend, 'resource_manager', _resource_manager)
    return calls


@pytest.fixture
def quiet_dialogs(monkeypatch):
    """No modal blocks the test; every message box text is kept."""
    shown = []

    def _box(parent, title, text, *args, **kwargs):
        shown.append((title, text))
        return QMessageBox.Ok

    for name in ('information', 'warning', 'critical'):
        monkeypatch.setattr(QMessageBox, name, staticmethod(_box))
    monkeypatch.setattr(QDialog, 'exec', lambda self: 0)
    return shown


@pytest.fixture
def py_machine(main_window):
    """The window, on a machine configured for pyvisa-py behind a Prologix."""
    config = main_window.config_manager
    config.set_machine_local('visa_library', '@py')
    config.set_machine_local('gpib_interface', INTERFACE)
    main_window.user_settings = config.get_user_settings(main_window.current_user)
    return main_window


def test_the_address_picker_scans_the_configured_backend(py_machine, opened, quiet_dialogs):
    py_machine.prompt_gpib_selection('GPIB0::24::INSTR')
    assert opened == [('@py', INTERFACE)]


def test_the_connection_test_uses_the_configured_backend(py_machine, opened, quiet_dialogs):
    py_machine.test_instrument_connection()
    assert opened == [('@py', INTERFACE)]
    assert quiet_dialogs[-1][0] == "Connection OK"


def test_detect_devices_scans_the_configured_backend(py_machine, opened, quiet_dialogs):
    dialog = SettingsDialog(py_machine.config_manager, py_machine.current_user)
    dialog.detect_gpib_devices()
    assert opened == [('@py', INTERFACE)]


def test_the_library_string_reaches_pyvisa(py_machine, quiet_dialogs, monkeypatch):
    """Through the real ``resource_manager``, down to pyvisa itself."""
    import pyvisa
    from tests.fakes.fake_keithley import FakeResourceManager

    libraries = []

    def _factory(*args):
        libraries.append(args)
        return FakeResourceManager()

    monkeypatch.setattr(pyvisa, 'ResourceManager', _factory)
    py_machine.config_manager.set_machine_local('gpib_interface', '')
    py_machine.test_instrument_connection()
    assert libraries == [('@py',)]


def test_an_interface_that_will_not_open_is_reported(py_machine, fake_rm, quiet_dialogs):
    """The fake has no Prologix resource, so opening the interface fails."""
    dialog = SettingsDialog(py_machine.config_manager, py_machine.current_user)
    dialog.detect_gpib_devices()
    assert INTERFACE in quiet_dialogs[-1][1]
    assert dialog.isEnabled()

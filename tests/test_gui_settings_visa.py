"""The Settings dialog's VISA backend and GPIB interface rows.

Both are machine-local, like the address beside them: they are read and
stored through the config's machine-local accessors, for a user's dialog
and for the global one alike.
"""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QMessageBox

from resistamet_gui import visa_backend
from resistamet_gui.config import ConfigManager
from resistamet_gui.ui.dialogs import SettingsDialog

INTERFACE = 'PRLGX-ASRL::5::INTFC'


@pytest.fixture
def shown(monkeypatch):
    boxes = []

    def _box(parent, title, text, *args, **kwargs):
        boxes.append((title, text))
        return QMessageBox.Ok

    for name in ('information', 'warning', 'critical'):
        monkeypatch.setattr(QMessageBox, name, staticmethod(_box))
    return boxes


@pytest.fixture
def config(app, tmp_path):
    manager = ConfigManager(config_file=str(tmp_path / 'config.json'))
    manager.add_user('ada')
    return manager


def _choose(dialog, library):
    dialog.visa_library.setCurrentIndex(
        [key for key, _ in dialog._visa_library_choices].index(library))


def test_the_backend_combo_offers_the_three_choices(config):
    dialog = SettingsDialog(config, 'ada')
    labels = [dialog.visa_library.itemText(i) for i in range(dialog.visa_library.count())]
    assert labels == list(visa_backend.CHOICES.values())
    assert dialog.visa_library.currentIndex() == 0
    assert dialog.gpib_interface.text() == ''


@pytest.mark.parametrize('username', ['ada', None])
def test_both_rows_show_this_machines_values(config, username):
    config.set_machine_local('visa_library', '@py')
    config.set_machine_local('gpib_interface', INTERFACE)
    dialog = SettingsDialog(config, username)
    assert dialog.visa_library.currentText() == 'pyvisa-py'
    assert dialog.gpib_interface.text() == INTERFACE


@pytest.mark.parametrize('username', ['ada', None])
def test_save_stores_both_for_this_machine(config, shown, username):
    dialog = SettingsDialog(config, username)
    _choose(dialog, '@py')
    dialog.gpib_interface.setText(f'  {INTERFACE} ')
    dialog.save_settings()

    reloaded = ConfigManager(config_file=config.config_file)
    assert reloaded.get_visa_library() == '@py'
    assert reloaded.get_gpib_interface() == INTERFACE


def test_back_to_automatic_is_stored(config, shown):
    config.set_machine_local('visa_library', '@py')
    dialog = SettingsDialog(config, 'ada')
    _choose(dialog, '')
    dialog.save_settings()
    assert ConfigManager(config_file=config.config_file).get_visa_library() == ''


def test_a_library_path_survives_the_dialog(config, shown):
    """A path is legal in the config; the combo shows it and Save keeps it."""
    config.set_machine_local('visa_library', '/opt/visa/libvisa.so')
    dialog = SettingsDialog(config, 'ada')
    assert dialog.visa_library.currentText() == '/opt/visa/libvisa.so'
    dialog.save_settings()
    assert config.get_visa_library() == '/opt/visa/libvisa.so'


def test_a_name_that_cannot_be_an_interface_is_refused(config, shown):
    dialog = SettingsDialog(config, 'ada')
    dialog.gpib_interface.setText('COM5')
    dialog.nplc.setValue(5.0)
    dialog.save_settings()
    assert shown[-1][0] == "GPIB Interface"
    assert dialog.result() != SettingsDialog.Accepted
    assert config.get_gpib_interface() == ''
    assert config.get_user_settings('ada')['measurement']['nplc'] != 5.0


def test_detect_devices_scans_what_the_dialog_shows(config, shown, fake_rm, monkeypatch):
    """Not what was last saved: the operator picks a backend, then scans."""
    calls = []

    def _resource_manager(visa_library='', gpib_interface=''):
        calls.append((visa_library, gpib_interface))
        return fake_rm

    monkeypatch.setattr(visa_backend, 'resource_manager', _resource_manager)
    monkeypatch.setattr(SettingsDialog, 'exec', lambda self: 0)
    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(QDialog, 'exec', lambda self: 0)

    dialog = SettingsDialog(config, 'ada')
    _choose(dialog, '@py')
    dialog.gpib_interface.setText(INTERFACE)
    dialog.detect_gpib_devices()
    assert calls == [('@py', INTERFACE)]
    assert config.get_visa_library() == ''

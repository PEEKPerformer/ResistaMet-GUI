"""Where the tabs' values come from, and what may replace them.

A tab holds the working values of this session. Three things put values on
it: selecting a user (every key, from that user's profile), saving the
Settings dialog (only the keys the save changed) and Load Profile (every key
the file holds for that tab). ``gather_settings_for_mode`` reads them back
for a run. These tests hold the readers and the writers together.
"""
import json
import math
import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QFileDialog, QMessageBox

from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.ui import main_window as main_window_module
from resistamet_gui.ui.dialogs import SettingsDialog
from resistamet_gui.ui.main_window import ResistanceMeterApp

# The real method: the ``main_window`` fixture replaces it on the class so
# that building a window opens no dialog.
_SELECT_USER = ResistanceMeterApp.select_user

MODES = ('resistance', 'source_v', 'source_i', 'four_point', 'sweep', 'vdp')

_DEFAULTS = DEFAULT_SETTINGS['measurement']

#: A profile that differs from the defaults in every key a tab holds. A key
#: ``gather_settings_for_mode`` reads from a widget and that is missing here
#: fails ``test_selecting_a_user_loads_every_key_a_tab_holds``: add it, and
#: its setter to ``ResistanceMeterApp._tab_setters``.
OTHER_PROFILE = {
    'sampling_rate': 1.5,   # under every tab's rate cap at these timings
    'nplc': 0.5,
    'auto_zero': 'off',
    'res_test_current': 5e-3,
    'res_voltage_compliance': 12.5,
    'res_measurement_type': '4-wire',
    'res_auto_range': not _DEFAULTS['res_auto_range'],
    'res_offset_comp': not _DEFAULTS['res_offset_comp'],
    'vsource_voltage': 2.5,
    'vsource_current_compliance': 0.02,
    'vsource_current_range_auto': not _DEFAULTS['vsource_current_range_auto'],
    'vsource_duration_hours': 2.5,
    'isource_current': 2e-3,
    'isource_voltage_compliance': 15.0,
    'isource_voltage_range_auto': not _DEFAULTS['isource_voltage_range_auto'],
    'isource_duration_hours': 1.5,
    'fpp_current': 2e-3,
    'fpp_voltage_compliance': 7.5,
    'fpp_voltage_range_auto': not _DEFAULTS['fpp_voltage_range_auto'],
    'fpp_spacing_cm': 0.2,
    'fpp_thickness_um': 200.0,
    'fpp_alpha': 0.9,
    'fpp_model': 'finite_thin',
    'fpp_k_factor': 4.4,
    'fpp_samples': 35,
    'fpp_diameter_cm': 5.0,
    'fpp_geometry': 'square',
    'fpp_temperature_c': 25.0,
    'fpp_dopant_type': 'n',
    'fpp_delta_mode': not _DEFAULTS['fpp_delta_mode'],
    'fpp_delta_settling': 1.5,
    'fpp_power_warn_w': 0.02,
    'fpp_power_stop_w': 0.2,
    'fpp_stop_on_overpower': not _DEFAULTS['fpp_stop_on_overpower'],
    'sweep_source': 'current',
    'sweep_start': -1.0,
    'sweep_stop': 40.0,
    'sweep_step': 0.5,
    'sweep_compliance': 0.05,
    'sweep_delay': 0.05,
    'sweep_direction': 'up_down',
    'sweep_measurement_type': '4-wire',
    'vdp_current': 2e-3,
    'vdp_voltage_compliance': 7.5,
    'vdp_voltage_range_auto': not _DEFAULTS['vdp_voltage_range_auto'],
    'vdp_thickness_cm': 0.05,
    'vdp_settling_s': 0.5,
    'vdp_readings_per_polarity': 3,
}

#: Read by gather, but not settings: each mirrors its mode's duration.
RUN_UNTIL_STOPPED = {'vsource_run_continuous': 'vsource_duration_hours',
                     'isource_run_continuous': 'isource_duration_hours'}


def same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    if isinstance(a, bool) or isinstance(b, bool) or isinstance(a, str) or isinstance(b, str):
        return a == b
    if a is None or b is None:
        return a is b
    return a == pytest.approx(b, rel=1e-9, abs=1e-12)


def tab_values(window, mode):
    """What gather reads from the tab's widgets for ``mode``."""
    return window._overrides_from_widgets(mode, window.get_widget_for_mode(mode))


def all_tab_values(window):
    return {(mode, key): value for mode in MODES
            for key, value in tab_values(window, mode).items()}


def differing(before, after):
    return sorted(key for key in before if not same(before[key], after[key]))


@pytest.fixture(autouse=True)
def quiet_boxes(monkeypatch):
    for name in ('information', 'warning', 'critical'):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.Ok))


@pytest.fixture
def window(main_window):
    """The window with a second user, ``bob``, on OTHER_PROFILE."""
    config = main_window.config_manager
    config.add_user('bob')
    config.update_user_settings('bob', {'measurement': dict(OTHER_PROFILE)})
    return main_window


def switch_user(window, monkeypatch, username):
    """Choose ``username`` in the user dialog, through the window's own slot."""
    class _Chosen:
        selected_user = username

        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return True

    monkeypatch.setattr(main_window_module, 'UserSelectionDialog', _Chosen)
    _SELECT_USER(window)
    assert window.current_user == username


def save_settings_dialog(window, monkeypatch, edit=None, *, shared=False):
    """Open the Settings dialog, optionally change something, press Save."""
    def _exec(dialog):
        if edit is not None:
            edit(dialog)
        dialog.save_settings()
        return dialog.result()

    monkeypatch.setattr(SettingsDialog, 'exec', _exec)
    if shared:
        window.open_global_settings()
    else:
        window.open_user_settings()


# --- 1. selecting a user ----------------------------------------------------

class TestSelectingAUser:
    @pytest.mark.parametrize('mode', MODES)
    def test_selecting_a_user_loads_every_key_a_tab_holds(self, window, monkeypatch, mode):
        before = tab_values(window, mode)

        switch_user(window, monkeypatch, 'bob')

        after = tab_values(window, mode)
        for key, value in after.items():
            if key in RUN_UNTIL_STOPPED:
                assert value is (OTHER_PROFILE[RUN_UNTIL_STOPPED[key]] == 0.0)
                continue
            assert key in OTHER_PROFILE, (
                f"gather reads '{key}' from the {mode} tab; give it a value in "
                "OTHER_PROFILE and a setter in _tab_setters")
            assert not same(before[key], OTHER_PROFILE[key]), (
                f"OTHER_PROFILE['{key}'] is the default; the test would prove nothing")
            assert same(value, OTHER_PROFILE[key]), (
                f"{mode} tab still holds {key}={value!r} after the switch; "
                f"the profile says {OTHER_PROFILE[key]!r}")

    @pytest.mark.parametrize('mode', MODES)
    def test_every_key_gather_reads_has_a_setter(self, window, mode):
        setters = window._tab_setters()[mode]
        read = set(tab_values(window, mode)) - set(RUN_UNTIL_STOPPED)
        assert read - set(setters) == set()

    def test_switching_back_restores_the_first_users_values(self, window, monkeypatch):
        first = all_tab_values(window)

        switch_user(window, monkeypatch, 'bob')
        assert differing(first, all_tab_values(window))
        switch_user(window, monkeypatch, 'test_user')

        assert differing(first, all_tab_values(window)) == []

    def test_what_one_user_typed_does_not_reach_the_next(self, window, monkeypatch):
        """The reviewer's case: delta mode, its settling and a sweep stop."""
        window.config_manager.add_user('carol')
        fpp, sweep = window.tab_four_point, window.tab_sweep
        fpp.fpp_delta_mode.setChecked(True)
        fpp.fpp_delta_settling.setValue(1.5)
        sweep.sweep_stop.setValue(40.0)
        sweep.sweep_nplc.setValue(0.01)

        switch_user(window, monkeypatch, 'carol')

        four_point = window.gather_settings_for_mode('four_point')['measurement']
        swept = window.gather_settings_for_mode('sweep')['measurement']
        assert four_point['fpp_delta_mode'] is _DEFAULTS['fpp_delta_mode']
        assert four_point['fpp_delta_settling'] == _DEFAULTS['fpp_delta_settling']
        assert swept['sweep_stop'] == _DEFAULTS['sweep_stop']
        assert swept['nplc'] == _DEFAULTS['nplc']


# --- 2. saving the Settings dialog ------------------------------------------

class TestSavingSettings:
    @staticmethod
    def _type_on_the_tabs(window):
        """Values the Settings dialog has no field for, and two it has."""
        fpp = window.tab_four_point
        fpp.fpp_thickness_um.setValue(200.0)
        fpp.fpp_diameter_cm.setValue(5.0)
        fpp.fpp_delta_mode.setChecked(True)
        fpp.nplc.setValue(2.0)
        window.tab_sweep.sweep_stop.setValue(40.0)
        window.tab_resistance.res_test_current.setValue(5e-3)
        window.tab_voltage_source.v_plot_var.setCurrentText('resistance')

    @pytest.mark.parametrize('shared', [False, True], ids=['user', 'global'])
    def test_a_save_that_changes_nothing_leaves_the_tabs_alone(self, window, monkeypatch,
                                                               shared):
        self._type_on_the_tabs(window)
        before = all_tab_values(window)

        save_settings_dialog(window, monkeypatch, shared=shared)

        assert differing(before, all_tab_values(window)) == []
        assert window.tab_voltage_source.v_plot_var.currentText() == 'resistance'

    def test_the_thickness_typed_on_the_tab_still_reaches_the_run(self, window, monkeypatch):
        self._type_on_the_tabs(window)

        save_settings_dialog(window, monkeypatch,
                             lambda dialog: dialog.data_directory.setText('elsewhere'))

        run = window.gather_settings_for_mode('four_point')
        assert run['measurement']['fpp_thickness_um'] == 200.0
        assert run['measurement']['fpp_diameter_cm'] == 5.0
        assert run['file']['data_directory'] == 'elsewhere'

    def test_a_key_the_dialog_changed_reaches_its_tabs_and_only_it(self, window, monkeypatch):
        self._type_on_the_tabs(window)
        before = all_tab_values(window)

        save_settings_dialog(window, monkeypatch,
                             lambda dialog: dialog.vsource_voltage.setValue(3.0))

        after = all_tab_values(window)
        assert differing(before, after) == [('source_v', 'vsource_voltage')]
        assert after[('source_v', 'vsource_voltage')] == 3.0

    def test_a_shared_key_the_dialog_changed_reaches_every_tab_that_has_it(self, window,
                                                                           monkeypatch):
        self._type_on_the_tabs(window)
        before = all_tab_values(window)

        # A shorter integration, so no tab's sampling-rate cap comes down.
        save_settings_dialog(window, monkeypatch, lambda dialog: dialog.nplc.setValue(0.5))

        after = all_tab_values(window)
        assert differing(before, after) == [('four_point', 'nplc'), ('sweep', 'nplc'),
                                            ('vdp', 'nplc')]
        assert {after[key] for key in differing(before, after)} == {0.5}

    def test_a_shared_setting_a_user_has_not_stored_follows_a_global_save(self, main_window,
                                                                         monkeypatch):
        # test_user has stored nothing, so runs on the shared values.
        save_settings_dialog(main_window, monkeypatch,
                             lambda dialog: dialog.res_voltage_compliance.setValue(8.0),
                             shared=True)

        assert main_window.tab_resistance.res_voltage_compliance.value() == 8.0


# --- 3. cable null ----------------------------------------------------------

def null_label_matches_the_run(window) -> float:
    """The applied cable null, after checking the label says the same."""
    applied = window.gather_settings_for_mode('resistance')['measurement']['res_cable_null']
    text = window.tab_resistance.null_label.text()
    if applied == 0.0:
        assert text == "Cable null: OFF"
    else:
        assert text != "Cable null: OFF" and text.startswith("Cable null: ")
    return applied


class TestCableNull:
    @pytest.fixture
    def nulled(self, window, fake_rm, monkeypatch):
        """A window whose cables were nulled on the (fake) instrument."""
        import pyvisa
        monkeypatch.setattr(pyvisa, 'ResourceManager', lambda *args: fake_rm)
        monkeypatch.setattr(QMessageBox, 'question', staticmethod(lambda *a, **k: QMessageBox.Ok))
        window._null_cables()
        assert null_label_matches_the_run(window) > 0
        return window

    def test_it_survives_a_settings_save(self, nulled, monkeypatch):
        applied = null_label_matches_the_run(nulled)

        save_settings_dialog(nulled, monkeypatch, lambda dialog: dialog.nplc.setValue(0.5))

        assert null_label_matches_the_run(nulled) == applied

    def test_it_survives_a_global_settings_save(self, nulled, monkeypatch):
        applied = null_label_matches_the_run(nulled)

        save_settings_dialog(nulled, monkeypatch, shared=True)

        assert null_label_matches_the_run(nulled) == applied

    def test_it_is_not_written_to_the_profile(self, nulled, monkeypatch):
        save_settings_dialog(nulled, monkeypatch)

        stored = nulled.config_manager.get_user_settings(nulled.current_user)
        assert stored['measurement']['res_cable_null'] == 0.0

    def test_another_user_starts_without_it(self, nulled, monkeypatch):
        switch_user(nulled, monkeypatch, 'bob')

        assert null_label_matches_the_run(nulled) == 0.0

    def test_clearing_it_clears_both(self, nulled):
        nulled._clear_cable_null()

        assert null_label_matches_the_run(nulled) == 0.0

    def test_a_null_stored_in_the_profile_is_shown(self, window, monkeypatch):
        window.config_manager.merge_user_settings('bob', {'measurement': {'res_cable_null': 0.35}})

        switch_user(window, monkeypatch, 'bob')

        assert null_label_matches_the_run(window) == 0.35
        assert window.tab_resistance.null_label.text() == "Cable null: 350.0 mΩ"


# --- 4. Save Profile / Load Profile -----------------------------------------

class TestProfileFiles:
    @staticmethod
    def _save(window, monkeypatch, mode, path):
        window.main_tabs.setCurrentWidget(window.get_widget_for_mode(mode))
        monkeypatch.setattr(QFileDialog, 'getSaveFileName',
                            staticmethod(lambda *a, **k: (str(path), '')))
        window.save_profile_for_mode()
        with open(path) as f:
            return json.load(f)

    @staticmethod
    def _load(window, monkeypatch, mode, path):
        window.main_tabs.setCurrentWidget(window.get_widget_for_mode(mode))
        monkeypatch.setattr(QFileDialog, 'getOpenFileName',
                            staticmethod(lambda *a, **k: (str(path), '')))
        window.load_profile_to_mode()

    @pytest.mark.parametrize('mode', ['resistance', 'source_v', 'source_i', 'four_point'])
    def test_load_restores_every_key_save_wrote_for_the_tab(self, window, monkeypatch,
                                                           tmp_path, mode):
        switch_user(window, monkeypatch, 'bob')
        saved = self._save(window, monkeypatch, mode, tmp_path / 'profile.json')
        switch_user(window, monkeypatch, 'test_user')
        defaults = tab_values(window, mode)

        self._load(window, monkeypatch, mode, tmp_path / 'profile.json')

        loaded = window.gather_settings_for_mode(mode)['measurement']
        for key in tab_values(window, mode):
            if key in RUN_UNTIL_STOPPED:
                continue
            assert not same(defaults[key], saved[key]), f"'{key}' was saved at its default"
            assert same(loaded[key], saved[key]), (
                f"'{key}' was saved as {saved[key]!r} and loaded as {loaded[key]!r}")

    def test_a_finite_diameter_setup_reloads_onto_the_same_formula(self, main_window,
                                                                   monkeypatch, tmp_path):
        fpp = main_window.tab_four_point
        fpp.fpp_diameter_cm.setValue(5.0)
        fpp.fpp_geometry.setCurrentText('square')
        fpp.fpp_temperature_c.setValue(25.0)
        fpp.fpp_dopant_type.setCurrentText('n')
        fpp.fpp_delta_mode.setChecked(True)
        fpp.fpp_delta_settling.setValue(1.5)
        fpp.fpp_samples.setValue(35)
        fpp.fpp_power_warn_w.setValue(0.02)
        fpp.fpp_power_stop_w.setValue(0.2)
        fpp.fpp_stop_on_overpower.setChecked(False)
        saved = self._save(main_window, monkeypatch, 'four_point', tmp_path / 'fpp.json')
        main_window.update_ui_from_settings()   # back to the stored profile

        self._load(main_window, monkeypatch, 'four_point', tmp_path / 'fpp.json')

        loaded = main_window.gather_settings_for_mode('four_point')['measurement']
        assert differing(saved, loaded) == []

    def test_an_unmeasured_temperature_round_trips(self, main_window, monkeypatch, tmp_path):
        saved = self._save(main_window, monkeypatch, 'four_point', tmp_path / 'fpp.json')
        assert math.isnan(saved['fpp_temperature_c'])
        main_window.tab_four_point.fpp_temperature_c.setValue(25.0)

        self._load(main_window, monkeypatch, 'four_point', tmp_path / 'fpp.json')

        loaded = main_window.gather_settings_for_mode('four_point')['measurement']
        assert math.isnan(loaded['fpp_temperature_c'])

    def test_resistance_timing_round_trips(self, main_window, monkeypatch, tmp_path):
        res = main_window.tab_resistance
        res.res_offset_comp.setChecked(not _DEFAULTS['res_offset_comp'])
        res.auto_zero.setCurrentText('off')
        saved = self._save(main_window, monkeypatch, 'resistance', tmp_path / 'r.json')
        main_window.update_ui_from_settings()

        self._load(main_window, monkeypatch, 'resistance', tmp_path / 'r.json')

        loaded = main_window.gather_settings_for_mode('resistance')['measurement']
        assert differing(saved, loaded) == []

    def test_a_legacy_thickness_in_cm_still_loads(self, main_window, monkeypatch, tmp_path):
        path = tmp_path / 'old.json'
        path.write_text(json.dumps({'fpp_thickness_cm': 0.02}))

        self._load(main_window, monkeypatch, 'four_point', path)

        assert main_window.tab_four_point.fpp_thickness_um.value() == pytest.approx(200.0)

    def test_a_file_does_not_move_what_the_tab_has_no_widget_for(self, main_window,
                                                                 monkeypatch, tmp_path):
        path = tmp_path / 'foreign.json'
        path.write_text(json.dumps({'gpib_address': 'GPIB0::9::INSTR', 'settling_time': 9.0,
                                    'safety_voltage_warn_silenced': True,
                                    'res_cable_null': 3.0, 'fpp_current': 2e-3}))
        before = main_window.gather_settings_for_mode('four_point')['measurement']

        self._load(main_window, monkeypatch, 'four_point', path)

        after = main_window.gather_settings_for_mode('four_point')['measurement']
        assert differing(before, after) == ['fpp_current']

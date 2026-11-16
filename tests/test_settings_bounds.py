"""Model bounds must match the widget ranges they describe.

The schema's job is to say what the GUI already allows. Checking that by eye
goes stale the first time someone widens a spin box, so this reads
``minimum()``/``maximum()`` off the real tabs and compares them with the
model's ``ge``/``le`` (or ``gt``/``lt``) metadata.

A deliberate divergence is allowed — it just has to be listed in
``LOOSER_THAN_WIDGET`` (lower bound) or ``UPPER_LOOSER_THAN_WIDGET`` (upper
bound) with the reason, so it shows up in review.

The Settings dialog is held to the same rule as the tabs, and every
``Literal`` to the items of the combo box that writes it: a profile the
PySide6 app can save must be one the API can run.
"""
import os
import typing

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from resistamet_gui.schema.settings_common import (
    AuxSensorSettings,
    DisplaySettings,
    FileSettings,
    InstrumentSettings,
    OutputSettings,
    SafetySettings,
)
from resistamet_gui.schema.settings_modes import (
    CurrentSourceSettings,
    FourPointSettings,
    ResistanceSettings,
    SweepSettings,
    VdpSettings,
    VoltageSourceSettings,
)

# model field -> widget attribute on that mode's tab
FIELD_WIDGETS = [
    (ResistanceSettings, 'tab_resistance', {
        'res_test_current': 'res_test_current',
        'res_voltage_compliance': 'res_voltage_compliance',
    }),
    (VoltageSourceSettings, 'tab_voltage_source', {
        'vsource_voltage': 'vsource_voltage',
        'vsource_current_compliance': 'vsource_current_compliance',
        'vsource_duration_hours': 'vsource_duration',
    }),
    (CurrentSourceSettings, 'tab_current_source', {
        'isource_current': 'isource_current',
        'isource_voltage_compliance': 'isource_voltage_compliance',
        'isource_duration_hours': 'isource_duration',
    }),
    (FourPointSettings, 'tab_four_point', {
        'fpp_current': 'fpp_current',
        'fpp_voltage_compliance': 'fpp_voltage_compliance',
        'fpp_spacing_cm': 'fpp_spacing_cm',
        'fpp_thickness_um': 'fpp_thickness_um',
        'fpp_alpha': 'fpp_alpha',
        'fpp_k_factor': 'fpp_k_factor',
        'fpp_samples': 'fpp_samples',
        'fpp_diameter_cm': 'fpp_diameter_cm',
        'fpp_temperature_c': 'fpp_temperature_c',
        'fpp_delta_settling': 'fpp_delta_settling',
        'fpp_power_warn_w': 'fpp_power_warn_w',
        'fpp_power_stop_w': 'fpp_power_stop_w',
    }),
    (SweepSettings, 'tab_sweep', {
        'sweep_start': 'sweep_start',
        'sweep_stop': 'sweep_stop',
        'sweep_step': 'sweep_step',
        'sweep_compliance': 'sweep_compliance',
        'sweep_delay': 'sweep_delay',
    }),
    (VdpSettings, 'tab_vdp', {
        'vdp_current': 'vdp_current',
        'vdp_voltage_compliance': 'vdp_voltage_compliance',
        'vdp_thickness_cm': 'vdp_thickness_cm',
        'vdp_settling_s': 'vdp_settling_s',
        'vdp_readings_per_polarity': 'vdp_readings_per_polarity',
    }),
]

# model field -> widget attribute on the Settings dialog
DIALOG_WIDGETS = [
    (InstrumentSettings, {
        'nplc': 'nplc',
        'sampling_rate': 'sampling_rate',
        'settling_time': 'settling_time',
        'filter_count': 'filter_count',
    }),
    (SafetySettings, {'safety_voltage_warn_v': 'safety_voltage_warn_v'}),
    (FileSettings, {'auto_save_interval': 'auto_save_interval'}),
    (OutputSettings, {'compression_threshold_mb': 'output_compression_threshold'}),
    (DisplaySettings, {'buffer_size': 'buffer_size'}),
    # The dialog repeats some of the tabs' fields as profile defaults.
    (ResistanceSettings, {
        'res_test_current': 'res_test_current',
        'res_voltage_compliance': 'res_voltage_compliance',
    }),
    (VoltageSourceSettings, {
        'vsource_voltage': 'vsource_voltage',
        'vsource_current_compliance': 'vsource_current_compliance',
        'vsource_duration_hours': 'vsource_duration_hours',
    }),
    (CurrentSourceSettings, {
        'isource_current': 'isource_current',
        'isource_voltage_compliance': 'isource_voltage_compliance',
        'isource_duration_hours': 'isource_duration_hours',
    }),
]

# field -> why the model declares no upper bound where the dialog has one
NO_UPPER_BOUND = {
    'compression_threshold_mb': 'a size in MB; the dialog stops at 10 GB for its own layout',
    'buffer_size': 'a point count; the dialog stops at a million, 0 = unlimited',
}

# Literal field -> (where, attribute): the combo box that writes it. 'dialog'
# is the Settings dialog, anything else a tab of the main window.
COMBO_BOXES = {
    (InstrumentSettings, 'auto_zero'): [('tab_resistance', 'auto_zero'),
                                        ('tab_voltage_source', 'auto_zero'),
                                        ('tab_current_source', 'auto_zero')],
    (InstrumentSettings, 'filter_type'): [('dialog', 'filter_type')],
    (OutputSettings, 'format'): [('dialog', '_output_format_choices')],
    (OutputSettings, 'compression'): [('dialog', '_output_compression_choices')],
    (ResistanceSettings, 'res_measurement_type'): [('tab_resistance', 'res_measurement_type'),
                                                   ('dialog', 'res_measurement_type')],
    (FourPointSettings, 'fpp_model'): [('tab_four_point', 'fpp_model')],
    (FourPointSettings, 'fpp_geometry'): [('tab_four_point', 'fpp_geometry')],
    (FourPointSettings, 'fpp_dopant_type'): [('tab_four_point', 'fpp_dopant_type')],
    (SweepSettings, 'sweep_source'): [('tab_sweep', 'sweep_source')],
    (SweepSettings, 'sweep_direction'): [('tab_sweep', 'sweep_direction')],
}

# Literal field -> why no PySide6 combo box writes it
NO_COMBO_BOX = {
    (FourPointSettings, 'fpp_sample_shape'): 'the sample outline is set from the desktop app only',
    (FourPointSettings, 'fpp_position_correction'): "one legal value, 'warn'; nothing to choose",
}

ALL_SETTINGS_MODELS = [
    InstrumentSettings, AuxSensorSettings, SafetySettings, FileSettings, OutputSettings,
    DisplaySettings, ResistanceSettings, VoltageSourceSettings, CurrentSourceSettings,
    FourPointSettings, SweepSettings, VdpSettings,
]

# field -> why the model bound deliberately differs from the widget's
LOOSER_THAN_WIDGET = {
    # The widget starts at 0 ("not entered"); a strict run request requires
    # > 0, which the resolver enforces rather than the field.
    'vdp_thickness_cm': 'widget allows 0 = unset; strict requests reject it',
    # Both spin boxes read in mA/uA-scaled engineering units whose lower stop
    # is the widget's display resolution, not a physical limit.
    'vdp_current': 'widget minimum is a display resolution, model requires > 0',
    'vdp_voltage_compliance': 'widget minimum is a display resolution, model requires > 0',
    'sweep_step': 'widget minimum is a display resolution, model requires > 0',
}

# field -> why the model's upper bound is deliberately above the widget's
UPPER_LOOSER_THAN_WIDGET = {
    # The PySide6 spin box stops at 3 in either unit. The model bounds the
    # compliance per source: 3.15 A on a voltage-sourced sweep, 210 V on a
    # current-sourced one (SweepSettings._compliance_fits_its_unit), and its
    # ``le`` is the larger of the two.
    'sweep_compliance': 'widget stops at 3 in either unit; the model bound follows the source',
}


def _bounds(model, field):
    """Return (lower, upper) numeric constraints declared on a model field."""
    lower = upper = None
    for item in model.model_fields[field].metadata:
        for attr, target in (('ge', 'lower'), ('gt', 'lower'), ('le', 'upper'), ('lt', 'upper')):
            if hasattr(item, attr):
                value = float(getattr(item, attr))
                if target == 'lower':
                    lower = value
                else:
                    upper = value
    return lower, upper


@pytest.mark.parametrize("model,tab_attr,mapping", FIELD_WIDGETS,
                          ids=[m.__name__ for m, _, _ in FIELD_WIDGETS])
def test_bounds_match_widgets(main_window, model, tab_attr, mapping):
    tab = getattr(main_window, tab_attr)
    for field, widget_attr in mapping.items():
        widget = getattr(tab, widget_attr)
        lower, upper = _bounds(model, field)
        assert lower is not None and upper is not None, f"{field} declares no bounds"

        if field in UPPER_LOOSER_THAN_WIDGET:
            assert upper >= widget.maximum(), (
                f"{field} is documented as looser than the widget but is tighter"
            )
        else:
            assert upper == pytest.approx(widget.maximum()), (
                f"{model.__name__}.{field} upper bound {upper} != widget "
                f"{widget.maximum()}"
            )
        if field in LOOSER_THAN_WIDGET:
            assert lower <= widget.minimum(), (
                f"{field} is documented as looser than the widget but is tighter"
            )
        else:
            assert lower == pytest.approx(widget.minimum()), (
                f"{model.__name__}.{field} lower bound {lower} != widget "
                f"{widget.minimum()}"
            )


def test_every_documented_divergence_is_used():
    """A stale LOOSER_THAN_WIDGET entry would hide a real mismatch."""
    covered = {field for _, _, mapping in FIELD_WIDGETS for field in mapping}
    assert set(LOOSER_THAN_WIDGET) <= covered
    assert set(UPPER_LOOSER_THAN_WIDGET) <= covered
    assert set(NO_UPPER_BOUND) <= {field for _, mapping in DIALOG_WIDGETS for field in mapping}


@pytest.fixture
def settings_dialog(main_window):
    from resistamet_gui.ui.dialogs import SettingsDialog

    dialog = SettingsDialog(main_window.config_manager, parent=main_window)
    yield dialog
    dialog.deleteLater()


@pytest.mark.parametrize("model,mapping", DIALOG_WIDGETS,
                          ids=[m.__name__ for m, _ in DIALOG_WIDGETS])
def test_bounds_match_the_settings_dialog(settings_dialog, model, mapping):
    """Whatever the dialog lets a profile hold, the model must accept.

    It did not: the touch-safety threshold was ``le=200`` against a spin box
    that goes to 1100 V, so a profile saved at 250 V failed every API run.
    """
    for field, widget_attr in mapping.items():
        widget = getattr(settings_dialog, widget_attr)
        lower, upper = _bounds(model, field)
        assert lower == pytest.approx(widget.minimum()), (
            f"{model.__name__}.{field} lower bound {lower} != dialog {widget.minimum()}")
        if field in NO_UPPER_BOUND:
            assert upper is None, f"{field} is documented as unbounded above but is not"
        else:
            assert upper == pytest.approx(widget.maximum()), (
                f"{model.__name__}.{field} upper bound {upper} != dialog {widget.maximum()}")


def _literal_fields(model):
    for name, info in model.model_fields.items():
        if typing.get_origin(info.annotation) is typing.Literal:
            yield name, list(typing.get_args(info.annotation))


def _combo_values(owner, attr):
    """The values a combo box can write: its texts, or the data keys of a
    ``(key, label)`` choice list where the dialog shows labels instead."""
    source = getattr(owner, attr)
    if isinstance(source, list):
        return [key for key, _label in source]
    return [source.itemText(index) for index in range(source.count())]


@pytest.mark.parametrize("model,field", list(COMBO_BOXES),
                          ids=[f"{m.__name__}.{f}" for m, f in COMBO_BOXES])
def test_literals_match_their_combo_boxes(main_window, settings_dialog, model, field):
    allowed = dict(_literal_fields(model))[field]
    for where, attr in COMBO_BOXES[(model, field)]:
        owner = settings_dialog if where == 'dialog' else getattr(main_window, where)
        assert _combo_values(owner, attr) == allowed, (
            f"{model.__name__}.{field} allows {allowed}; {where}.{attr} offers "
            f"{_combo_values(owner, attr)}")


def test_every_literal_is_compared_or_excused():
    """A new Literal needs its combo box named here, or a reason it has none."""
    literals = {(model, name) for model in ALL_SETTINGS_MODELS
                for name, _ in _literal_fields(model)}
    assert literals == set(COMBO_BOXES) | set(NO_COMBO_BOX)
    assert not set(COMBO_BOXES) & set(NO_COMBO_BOX)

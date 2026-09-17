"""Model bounds must match the widget ranges they describe.

The schema's job is to say what the GUI already allows. Checking that by eye
goes stale the first time someone widens a spin box, so this reads
``minimum()``/``maximum()`` off the real tabs and compares them with the
model's ``ge``/``le`` (or ``gt``/``lt``) metadata.

A deliberate divergence is allowed — it just has to be listed in
``LOOSER_THAN_WIDGET`` with the reason, so it shows up in review.
"""
import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from resistamet_gui.schema.settings_modes import (
    CurrentSourceSettings,
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

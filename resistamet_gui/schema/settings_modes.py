"""One model per measurement mode.

Each model describes the ``measurement`` keys that mode's tab writes today in
``gather_settings_for_mode``; the shared groups live in ``settings_common``.
Bounds are the current widget ranges, and ``test_settings_bounds.py`` reads
``minimum()``/``maximum()`` off the real tabs and compares, so a widget range
edited without the model is a test failure rather than a silent divergence.

Defaults are :data:`~resistamet_gui.constants.DEFAULT_SETTINGS` by reference.
See ``docs/design/tauri_backend_split.md`` section 4.2.
"""
from typing import Any, Dict, Literal, Optional

from pydantic import ConfigDict, Field

from ..constants import DEFAULT_SETTINGS
from .settings_common import SettingsModel

_M = DEFAULT_SETTINGS['measurement']


class ResistanceSettings(SettingsModel):
    """Source I, measure R. 2-wire or 4-wire, optional cable null."""

    res_test_current: float = Field(default=_M['res_test_current'], ge=1e-7, le=3.0)
    res_voltage_compliance: float = Field(default=_M['res_voltage_compliance'], ge=0.1, le=200.0)
    res_measurement_type: Literal['2-wire', '4-wire'] = _M['res_measurement_type']
    res_auto_range: bool = _M['res_auto_range']
    # Offset-compensated ohms; halves throughput, tightens sigma_R.
    res_offset_comp: bool = _M['res_offset_comp']
    # Written by the cable-null procedure, not typed by the operator, but it
    # rides in the same dict because the worker subtracts it per reading.
    res_cable_null: float = Field(default=_M['res_cable_null'], ge=0.0)


class VoltageSourceSettings(SettingsModel):
    """Source V, measure I. ``vsource_duration_hours`` of 0 runs until stopped."""

    vsource_voltage: float = Field(default=_M['vsource_voltage'], ge=-200.0, le=200.0)
    vsource_current_compliance: float = Field(
        default=_M['vsource_current_compliance'], ge=1e-7, le=3.0)
    vsource_current_range_auto: bool = _M['vsource_current_range_auto']
    vsource_duration_hours: float = Field(default=_M['vsource_duration_hours'], ge=0.0, le=168.0)


class CurrentSourceSettings(SettingsModel):
    """Source I, measure V. ``isource_duration_hours`` of 0 runs until stopped."""

    isource_current: float = Field(default=_M['isource_current'], ge=-3.0, le=3.0)
    isource_voltage_compliance: float = Field(
        default=_M['isource_voltage_compliance'], ge=0.1, le=200.0)
    isource_voltage_range_auto: bool = _M['isource_voltage_range_auto']
    isource_duration_hours: float = Field(default=_M['isource_duration_hours'], ge=0.0, le=168.0)


class SweepSettings(SettingsModel):
    """Bulk linear sweep, run by the instrument's own sweep engine.

    Start/stop keep the +/-200 V bounds for both source types, as the widgets
    do today; source-aware bounds are a follow-up.
    """

    sweep_source: Literal['voltage', 'current'] = _M['sweep_source']
    sweep_start: float = Field(default=_M['sweep_start'], ge=-200.0, le=200.0)
    sweep_stop: float = Field(default=_M['sweep_stop'], ge=-200.0, le=200.0)
    sweep_step: float = Field(default=_M['sweep_step'], gt=0.0, le=200.0)
    sweep_compliance: float = Field(default=_M['sweep_compliance'], ge=1e-7, le=3.0)
    sweep_delay: float = Field(default=_M['sweep_delay'], ge=0.0, le=10.0)
    sweep_direction: Literal['up', 'down', 'up_down'] = _M['sweep_direction']


class VdpSettings(SettingsModel):
    """van der Pauw, ASTM F76 Method A.

    Thickness stays >= 0 here because a stored profile legitimately holds 0 for
    "never entered"; a run request needs > 0, which the resolver enforces in
    strict mode (the UI prompts for it on Start).
    """

    vdp_current: float = Field(default=_M['vdp_current'], gt=0.0, le=1.0)
    vdp_voltage_compliance: float = Field(default=_M['vdp_voltage_compliance'], gt=0.0, le=200.0)
    vdp_voltage_range_auto: bool = _M['vdp_voltage_range_auto']
    vdp_thickness_cm: float = Field(default=_M['vdp_thickness_cm'], ge=0.0, le=10.0)
    vdp_settling_s: float = Field(default=_M['vdp_settling_s'], ge=0.0, le=10.0)
    vdp_readings_per_polarity: int = Field(default=_M['vdp_readings_per_polarity'], ge=1, le=100)


class FourPointSettings(SettingsModel):
    """Four-point probe, with the ASTM F84 correction-factor inputs.

    ``fpp_temperature_c`` is ``None`` when the temperature was not measured.
    The UI carries that as a -50 C sentinel on its spin box (the widget's
    "not measured" special value) and the worker as NaN; the resolver converts
    between the two, and neither representation reaches this model.
    """

    fpp_current: float = Field(default=_M['fpp_current'], ge=-3.0, le=3.0)
    fpp_voltage_compliance: float = Field(default=_M['fpp_voltage_compliance'], ge=0.1, le=200.0)
    fpp_voltage_range_auto: bool = _M['fpp_voltage_range_auto']
    fpp_spacing_cm: float = Field(default=_M['fpp_spacing_cm'], ge=0.001, le=5.0)
    # 0 = unknown thickness: sheet resistance only, no resistivity.
    fpp_thickness_um: float = Field(default=_M['fpp_thickness_um'], ge=0.0, le=5000.0)
    fpp_alpha: float = Field(default=_M['fpp_alpha'], ge=0.0, le=10.0)
    fpp_k_factor: float = Field(default=_M['fpp_k_factor'], ge=0.1, le=50.0)
    # 0 = run until stopped.
    fpp_samples: int = Field(default=_M['fpp_samples'], ge=0, le=1_000_000)
    fpp_model: Literal['thin_film', 'semi_infinite', 'finite_thin', 'finite_alpha'] = _M['fpp_model']
    # 0 = treat the specimen as infinite (F2 = 4.5324).
    fpp_diameter_cm: float = Field(default=_M['fpp_diameter_cm'], ge=0.0, le=100.0)
    fpp_geometry: Literal[
        'circle', 'square', 'rectangle_2', 'rectangle_3', 'rectangle_4'
    ] = _M['fpp_geometry']
    fpp_temperature_c: Optional[float] = Field(default=None, ge=-50.0, le=200.0)
    fpp_dopant_type: Literal['none', 'n', 'p'] = _M['fpp_dopant_type']
    fpp_delta_mode: bool = _M['fpp_delta_mode']
    fpp_delta_settling: float = Field(default=_M['fpp_delta_settling'], ge=0.01, le=5.0)
    fpp_power_warn_w: float = Field(default=_M['fpp_power_warn_w'], ge=1e-4, le=10.0)
    fpp_power_stop_w: float = Field(default=_M['fpp_power_stop_w'], ge=1e-4, le=22.0)
    fpp_stop_on_overpower: bool = _M['fpp_stop_on_overpower']

    def worst_case_power_w(self) -> float:
        """|I| x |V_compliance|: the most the probe can dissipate.

        The same product the worker's pre-flight refuses to start above
        (``workers.py``), computed here so a client can see it before asking
        for a run.
        """
        return abs(self.fpp_current) * abs(self.fpp_voltage_compliance)


#: Mode name -> the model describing that mode's measurement keys.
MODE_MODELS = {
    'resistance': ResistanceSettings,
    'source_v': VoltageSourceSettings,
    'source_i': CurrentSourceSettings,
    'four_point': FourPointSettings,
    'sweep': SweepSettings,
    'vdp': VdpSettings,
}


class RunRequest(SettingsModel):
    """What a client asks for. Never persisted.

    ``overrides`` is flat measurement keys, the same shape the tabs produce
    today, so one request format serves the UI, a script and the MCP layer.
    Strict validation of the keys happens in the resolver, which knows which
    ones the profile owns.
    """

    model_config = ConfigDict(extra='forbid')

    mode: Literal['resistance', 'source_v', 'source_i', 'four_point', 'sweep', 'vdp']
    username: str = Field(min_length=1)
    sample_name: str = Field(min_length=1)
    overrides: Dict[str, Any] = Field(default_factory=dict)
    # Session runs only: how long a prompt may sit unanswered before the run
    # aborts with the output off. The PySide6 path never times out.
    prompt_timeout_s: float = Field(default=900.0, gt=0.0)

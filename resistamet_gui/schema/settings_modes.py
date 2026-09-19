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

from pydantic import ConfigDict, Field, field_validator, model_validator

from ..constants import DEFAULT_SETTINGS
from .settings_common import SettingsModel
from .spots import SpotRequest, check_spot_mode

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


#: The compliance of a sweep limits what is *measured*, so its unit follows
#: the source: a current when sourcing voltage, a voltage when sourcing
#: current. These are the family's envelope, not one model's: 3.15 A is the
#: compliance ceiling of the 3 A class (2420/2425), 210 V that of the 2400's
#: 200 V range. The instrument in use may allow less and rejects what it
#: cannot do.
SWEEP_MAX_CURRENT_COMPLIANCE_A = 3.15
SWEEP_MAX_VOLTAGE_COMPLIANCE_V = 210.0

#: Start, stop and step are what is *sourced*, in the source's unit, and are
#: held to what the fixed-level modes may source: ``vsource_voltage`` (200 V)
#: and ``isource_current`` (3 A). A sweep is not allowed further than a
#: constant output is.
SWEEP_MAX_SOURCE_VOLTAGE_V = 200.0
SWEEP_MAX_SOURCE_CURRENT_A = 3.0


class SweepSettings(SettingsModel):
    """Bulk linear sweep, run by the instrument's own sweep engine.

    Start, stop, step and compliance change unit with ``sweep_source``. Their
    ``ge``/``le`` are the wider of the two units' limits -- the PySide6 spin
    boxes', which do not follow the source -- and the validators below apply
    the limit of the unit the value is actually in. For a headless client
    the model is the only gate: without them a current-sourced sweep "to
    200" validated, and 200 there is amperes.
    """

    sweep_source: Literal['voltage', 'current'] = _M['sweep_source']
    sweep_start: float = Field(default=_M['sweep_start'], ge=-SWEEP_MAX_SOURCE_VOLTAGE_V,
                               le=SWEEP_MAX_SOURCE_VOLTAGE_V)
    sweep_stop: float = Field(default=_M['sweep_stop'], ge=-SWEEP_MAX_SOURCE_VOLTAGE_V,
                              le=SWEEP_MAX_SOURCE_VOLTAGE_V)
    sweep_step: float = Field(default=_M['sweep_step'], gt=0.0, le=SWEEP_MAX_SOURCE_VOLTAGE_V)
    # ``le`` is the larger of the two per-source limits; the validator below
    # applies the one that goes with ``sweep_source``.
    sweep_compliance: float = Field(default=_M['sweep_compliance'], ge=1e-7,
                                    le=SWEEP_MAX_VOLTAGE_COMPLIANCE_V)
    sweep_delay: float = Field(default=_M['sweep_delay'], ge=0.0, le=10.0)
    sweep_direction: Literal['up', 'down', 'up_down'] = _M['sweep_direction']

    @field_validator('sweep_start', 'sweep_stop', 'sweep_step')
    @classmethod
    def _source_value_fits_its_unit(cls, value, info):
        """Bound a sourced value in the unit it is in.

        The mirror of the compliance validator below: on a voltage-sourced
        sweep the value is a voltage and ``ge``/``le`` have already held it;
        on a current-sourced one it is a current. A field validator so the
        issue is keyed to the field; ``sweep_source`` is declared first and
        is in ``info.data`` unless it was itself invalid, where the tighter
        current limit applies.
        """
        if info.data.get('sweep_source') == 'voltage':
            return value
        if abs(value) > SWEEP_MAX_SOURCE_CURRENT_A:
            raise ValueError(
                f"a current-sourced sweep's {info.field_name.split('_')[1]} is a current: "
                f"within +/-{SWEEP_MAX_SOURCE_CURRENT_A:g} A")
        return value

    @field_validator('sweep_compliance')
    @classmethod
    def _compliance_fits_its_unit(cls, value, info):
        """Bound the compliance in the unit it is in.

        It used to be ``le=3`` whatever the source, which let a 3 A current
        limit through but capped a current-sourced sweep at 3 V. A field
        validator, not a model one, so the issue is keyed to
        ``sweep_compliance``; ``sweep_source`` is declared first and is
        therefore already in ``info.data`` (absent only when it was itself
        invalid, where the tighter current limit applies).
        """
        if info.data.get('sweep_source') == 'current':
            return value  # a voltage, already held to 210 V by ``le``
        if value > SWEEP_MAX_CURRENT_COMPLIANCE_A:
            raise ValueError(
                f"a voltage-sourced sweep's compliance is a current: at most "
                f"{SWEEP_MAX_CURRENT_COMPLIANCE_A:g} A")
        return value


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
    The PySide6 UI carries that as a -50 C sentinel on its spin box (the
    widget's "not measured" special value) and a profile as NaN. The resolver
    shows this model ``None`` in place of NaN and converts nothing in the run
    settings: NaN from a profile stays NaN and a JSON client's ``null`` stays
    ``None``. The consumers read both as "not measured".
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
    # The sample outline, for the position check of a spot. While the shape is
    # 'unbounded' the two legacy keys above describe the outline instead; see
    # ``spots.sample_geometry_from_settings``. 0 = dimension not entered.
    fpp_sample_shape: Literal['unbounded', 'circle', 'rectangle'] = _M['fpp_sample_shape']
    fpp_sample_diameter_mm: float = Field(default=_M['fpp_sample_diameter_mm'], ge=0.0, le=1000.0)
    fpp_sample_width_mm: float = Field(default=_M['fpp_sample_width_mm'], ge=0.0, le=1000.0)
    fpp_sample_length_mm: float = Field(default=_M['fpp_sample_length_mm'], ge=0.0, le=1000.0)
    # Only 'warn' exists: whether a position-aware correction ('apply') may be
    # offered at all is an open question of the design, and until it is
    # answered every number in a file is F84 as written.
    fpp_position_correction: Literal['warn'] = _M['fpp_position_correction']
    fpp_edge_warn_pct: float = Field(default=_M['fpp_edge_warn_pct'], ge=0.0, le=100.0)
    fpp_array_angle_deg: float = Field(default=_M['fpp_array_angle_deg'], ge=-360.0, le=360.0)
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


#: A client's name or version: a short token that is safe on one line of a
#: CSV header. Letters, digits, space and ``. _ + -``; no control characters.
CLIENT_TEXT_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}$'


class ClientInfo(SettingsModel):
    """Which program asked for the run, recorded in the file header.

    The backend's own version is always written (``software_version``); this
    says what was driving it -- the desktop app, a script, the MCP layer --
    so a file written through the API can be told from one the PySide6 app
    wrote. Self-reported, so it is provenance and not authentication.
    """

    model_config = ConfigDict(extra='forbid')

    name: str = Field(pattern=CLIENT_TEXT_PATTERN)
    version: str = Field(pattern=CLIENT_TEXT_PATTERN)


class RunRequest(SettingsModel):
    """What a client asks for. Never persisted.

    This is the body ``POST /session/start`` validates, and the model the
    desktop's request type is generated from; unknown fields are refused so
    a misspelt one cannot be silently ignored.

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
    # Which placement of the probe this run is (``spots.SPOT_MODES`` only).
    spot: Optional[SpotRequest] = None
    # Who is asking; absent, the file header says nothing about a client.
    client: Optional[ClientInfo] = None

    @model_validator(mode='after')
    def _only_four_point_has_spots(self):
        if self.spot is not None:
            check_spot_mode(self.mode)
        return self

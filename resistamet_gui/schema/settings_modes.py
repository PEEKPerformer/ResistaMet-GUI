"""One model per measurement mode.

Each model describes the ``measurement`` keys that mode's tab writes today in
``gather_settings_for_mode``; the shared groups live in ``settings_common``.
Bounds are the current widget ranges, and ``test_settings_bounds.py`` reads
``minimum()``/``maximum()`` off the real tabs and compares, so a widget range
edited without the model is a test failure rather than a silent divergence.

Defaults are :data:`~resistamet_gui.constants.DEFAULT_SETTINGS` by reference.
See ``docs/design/tauri_backend_split.md`` section 4.2.
"""
from typing import Annotated, Any, Dict, Literal, Optional

from pydantic import ConfigDict, Field, StringConstraints, field_validator, model_validator

from ..constants import DEFAULT_SETTINGS
from .settings_common import SettingsModel
from .spots import LABEL_PATTERN, SpotRequest, check_spot_mode

_M = DEFAULT_SETTINGS['measurement']


class ResistanceSettings(SettingsModel):
    """Source I, measure R. 2-wire or 4-wire, optional cable null."""

    res_test_current: float = Field(default=_M['res_test_current'], ge=1e-7, le=3.0)
    res_voltage_compliance: float = Field(default=_M['res_voltage_compliance'], ge=0.1, le=200.0)
    res_measurement_type: Literal['2-wire', '4-wire'] = Field(
        default=_M['res_measurement_type'],
        description="4-wire senses the voltage at the sample on separate leads (remote "
                    "sense); 2-wire includes the leads' resistance.")
    res_auto_range: bool = Field(
        default=_M['res_auto_range'],
        description="Auto-ohms: the instrument chooses its own test current and voltage "
                    "limit.")
    # Offset-compensated ohms; halves throughput, tightens sigma_R.
    res_offset_comp: bool = Field(
        default=_M['res_offset_comp'],
        description="Offset-compensated ohms: cancels thermal EMFs, halves the reading "
                    "rate.")
    # Written by the cable-null procedure, not typed by the operator, but it
    # rides in the same dict because the worker subtracts it per reading.
    res_cable_null: float = Field(
        default=_M['res_cable_null'], ge=0.0,
        description="Lead resistance subtracted from every reading, in ohms; set by the "
                    "cable-null procedure.")


class VoltageSourceSettings(SettingsModel):
    """Source V, measure I. ``vsource_duration_hours`` of 0 runs until stopped."""

    vsource_voltage: float = Field(default=_M['vsource_voltage'], ge=-200.0, le=200.0)
    vsource_current_compliance: float = Field(
        default=_M['vsource_current_compliance'], ge=1e-7, le=3.0)
    vsource_current_range_auto: bool = _M['vsource_current_range_auto']
    vsource_duration_hours: float = Field(default=_M['vsource_duration_hours'], ge=0.0, le=168.0,
                                          description="0 = until stopped.")


class CurrentSourceSettings(SettingsModel):
    """Source I, measure V. ``isource_duration_hours`` of 0 runs until stopped."""

    isource_current: float = Field(default=_M['isource_current'], ge=-3.0, le=3.0)
    isource_voltage_compliance: float = Field(
        default=_M['isource_voltage_compliance'], ge=0.1, le=200.0)
    isource_voltage_range_auto: bool = _M['isource_voltage_range_auto']
    isource_duration_hours: float = Field(default=_M['isource_duration_hours'], ge=0.0, le=168.0,
                                          description="0 = until stopped.")


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


#: What sweep_start, sweep_stop and sweep_step are in.
_SOURCED = (f"In the source's unit: V, within +/-{SWEEP_MAX_SOURCE_VOLTAGE_V:g}, when "
            f"sweep_source is voltage; A, within +/-{SWEEP_MAX_SOURCE_CURRENT_A:g}, when it is "
            f"current.")


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
                               le=SWEEP_MAX_SOURCE_VOLTAGE_V, description=_SOURCED)
    sweep_stop: float = Field(default=_M['sweep_stop'], ge=-SWEEP_MAX_SOURCE_VOLTAGE_V,
                              le=SWEEP_MAX_SOURCE_VOLTAGE_V, description=_SOURCED)
    sweep_step: float = Field(default=_M['sweep_step'], gt=0.0, le=SWEEP_MAX_SOURCE_VOLTAGE_V,
                              description=_SOURCED)
    # ``le`` is the larger of the two per-source limits; the validator below
    # applies the one that goes with ``sweep_source``.
    sweep_compliance: float = Field(
        default=_M['sweep_compliance'], ge=1e-7, le=SWEEP_MAX_VOLTAGE_COMPLIANCE_V,
        description=f"Limit on the measured quantity: a current, at most "
                    f"{SWEEP_MAX_CURRENT_COMPLIANCE_A:g} A, when sweep_source is voltage; a "
                    f"voltage, at most {SWEEP_MAX_VOLTAGE_COMPLIANCE_V:g} V, when it is "
                    f"current.")
    sweep_delay: float = Field(default=_M['sweep_delay'], ge=0.0, le=10.0,
                               description="Delay between sourcing and measuring at each "
                                           "point, in s.")
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

    A thickness of 0 is "not given", as for the four-point probe: the run
    reports the sheet resistance and the homogeneity check, which need no
    thickness, and no resistivity. It used to be refused, although nothing
    but the resistivity reads it.
    """

    vdp_current: float = Field(default=_M['vdp_current'], gt=0.0, le=1.0)
    vdp_voltage_compliance: float = Field(default=_M['vdp_voltage_compliance'], gt=0.0, le=200.0)
    vdp_voltage_range_auto: bool = _M['vdp_voltage_range_auto']
    vdp_thickness_cm: float = Field(
        default=_M['vdp_thickness_cm'], ge=0.0, le=10.0,
        description="0 = not given: sheet resistance and the homogeneity check only, no "
                    "resistivity.")
    vdp_settling_s: float = Field(default=_M['vdp_settling_s'], ge=0.0, le=10.0,
                                  description="Wait after each change of current, in s.")
    vdp_readings_per_polarity: int = Field(
        default=_M['vdp_readings_per_polarity'], ge=1, le=100,
        description="Readings averaged at each current direction of each wiring.")


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
    fpp_thickness_um: float = Field(
        default=_M['fpp_thickness_um'], ge=0.0, le=5000.0,
        description="0 = not given: sheet resistance only, no resistivity or conductivity "
                    "(fpp_model semi_infinite does not use it). With the ASTM F84 "
                    "corrections no sheet resistance either: their thickness factor "
                    "needs it.")
    fpp_alpha: float = Field(default=_M['fpp_alpha'], ge=0.0, le=10.0,
                             description="Finite-size correction multiplying K "
                                         "(fpp_model thin_film).")
    fpp_k_factor: float = Field(default=_M['fpp_k_factor'], ge=0.1, le=50.0,
                                description="Geometric factor K: Rs = K x V/I; 4.532 for an "
                                            "infinite thin sheet.")
    # 0 = run until stopped.
    fpp_samples: int = Field(default=_M['fpp_samples'], ge=0, le=1_000_000,
                             description="Readings to take, then stop; 0 = until stopped.")
    fpp_model: Literal['thin_film', 'semi_infinite', 'finite_thin', 'finite_alpha'] = Field(
        default=_M['fpp_model'],
        description="thin_film: Rs = K x alpha x V/I, rho = Rs x t; finite_thin: the same "
                    "without alpha; semi_infinite: rho = 2 pi s x V/I (bulk, t >> s); "
                    "finite_alpha: rho = alpha x 2 pi s x V/I. Not used when an ASTM F84 "
                    "input is set (fpp_diameter_cm > 0, fpp_geometry not circle, or "
                    "fpp_temperature_c with fpp_dopant_type n or p): the F84 corrections "
                    "apply instead.")
    # 0 = treat the specimen as infinite (F2 = 4.5324).
    fpp_diameter_cm: float = Field(
        default=_M['fpp_diameter_cm'], ge=0.0, le=100.0,
        description="Sample diameter (or width) for the ASTM F84 F2 correction; 0 = "
                    "infinite. Above 0 the F84 corrections apply.")
    fpp_geometry: Literal[
        'circle', 'square', 'rectangle_2', 'rectangle_3', 'rectangle_4'
    ] = Field(default=_M['fpp_geometry'],
              description="Sample shape for the F2 correction (rectangle_N: length = N x "
                          "width). Anything but circle applies the F84 corrections.")
    # The sample outline, for the position check of a spot. While the shape is
    # 'unbounded' the two legacy keys above describe the outline instead; see
    # ``spots.sample_geometry_from_settings``. 0 = dimension not entered.
    fpp_sample_shape: Literal['unbounded', 'circle', 'rectangle'] = Field(
        default=_M['fpp_sample_shape'],
        description="Sample outline for a spot's position check only; unbounded: "
                    "fpp_geometry and fpp_diameter_cm describe it.")
    fpp_sample_diameter_mm: float = Field(default=_M['fpp_sample_diameter_mm'], ge=0.0, le=1000.0)
    fpp_sample_width_mm: float = Field(default=_M['fpp_sample_width_mm'], ge=0.0, le=1000.0)
    fpp_sample_length_mm: float = Field(default=_M['fpp_sample_length_mm'], ge=0.0, le=1000.0)
    # Only 'warn' exists: whether a position-aware correction ('apply') may be
    # offered at all is an open question of the design, and until it is
    # answered every number in a file is F84 as written.
    fpp_position_correction: Literal['warn'] = _M['fpp_position_correction']
    fpp_edge_warn_pct: float = Field(default=_M['fpp_edge_warn_pct'], ge=0.0, le=100.0)
    fpp_array_angle_deg: float = Field(default=_M['fpp_array_angle_deg'], ge=-360.0, le=360.0)
    fpp_temperature_c: Optional[float] = Field(
        default=None, ge=-50.0, le=200.0,
        description="Silicon sample temperature for the F84 correction to 23 C, with "
                    "fpp_dopant_type; null = not measured.")
    fpp_dopant_type: Literal['none', 'n', 'p'] = Field(
        default=_M['fpp_dopant_type'],
        description="Silicon dopant type for the F84 temperature correction.")
    fpp_delta_mode: bool = Field(
        default=_M['fpp_delta_mode'],
        description="Current reversal: each reading combines +I and -I, cancelling "
                    "thermal offsets.")
    fpp_delta_settling: float = Field(default=_M['fpp_delta_settling'], ge=0.01, le=5.0,
                                      description="Wait after each reversal, in s.")
    fpp_power_warn_w: float = Field(
        default=_M['fpp_power_warn_w'], ge=1e-4, le=10.0,
        description="The run warns when |fpp_current| x fpp_voltage_compliance is above "
                    "this.")
    fpp_power_stop_w: float = Field(
        default=_M['fpp_power_stop_w'], ge=1e-4, le=22.0,
        description="The run refuses to start when |fpp_current| x fpp_voltage_compliance "
                    "is above this, and with fpp_stop_on_overpower stops when a measured "
                    "V x I is.")
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


#: Text a client sends that ends up on a ``# key: value`` line of the data
#: file: it has to stay on that line, so no control characters (the rule a
#: spot label already follows), and it is stripped because the header reader
#: strips. The PySide6 app gets the same from a single-line edit and
#: ``.strip()``. The sample name also becomes part of the file name, where a
#: component is 255 bytes at most; the user name matches what the profile
#: route accepts.
SAMPLE_NAME_MAX_LENGTH = 120
USERNAME_MAX_LENGTH = 64
SampleName = Annotated[str, StringConstraints(
    strip_whitespace=True, min_length=1, max_length=SAMPLE_NAME_MAX_LENGTH,
    pattern=LABEL_PATTERN)]
Username = Annotated[str, StringConstraints(
    strip_whitespace=True, min_length=1, max_length=USERNAME_MAX_LENGTH,
    pattern=LABEL_PATTERN)]

#: The longest a session run may sit on an unanswered prompt: a day.
PROMPT_TIMEOUT_MAX_S = 86_400.0


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
    username: Username
    sample_name: SampleName
    overrides: Dict[str, Any] = Field(default_factory=dict)
    # Session runs only: how long a prompt may sit unanswered before the run
    # aborts with the output off. The PySide6 path never times out. Finite
    # and bounded: "Infinity" would be a timeout that is not one.
    prompt_timeout_s: float = Field(default=900.0, gt=0.0, le=PROMPT_TIMEOUT_MAX_S,
                                    allow_inf_nan=False)
    # Which placement of the probe this run is (``spots.SPOT_MODES`` only).
    spot: Optional[SpotRequest] = None
    # Who is asking; absent, the file header says nothing about a client.
    client: Optional[ClientInfo] = None

    @model_validator(mode='after')
    def _only_four_point_has_spots(self):
        if self.spot is not None:
            check_spot_mode(self.mode)
        return self

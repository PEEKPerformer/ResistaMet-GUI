"""Turn a stored profile plus a client's overrides into run settings.

This is everything ``ui/main_window.py`` ``gather_settings_for_mode`` does
*except* read Qt widgets: the fallbacks, the run-until-stopped flags, the
profile-owned keys and the accuracy-mode timing overrides. The widget reads
stay in the UI and hand their values in as ``overrides``, so the GUI and a
headless client reach identical settings by the same path.

Pure and Qt-free. Every step below carries the ``main_window.py`` behaviour it
reproduces; the parity test (``tests/test_gather_golden.py``) pins the output
against dicts captured from the pre-refactor GUI.

Two validation modes:

* **lenient** (a stored profile being opened): out-of-range values are
  reported as issues and passed through unchanged. A lab profile that drifted
  out of range must still open.
* **strict** (an API run request): values must have the type they claim --
  a JSON ``"false"`` is not a bool and ``true`` is not a current -- and the
  *validated* values are what the run receives, so nothing reaches a worker
  in a form the models never saw. Then the same issues, plus the checks the
  GUI makes at Start — vdP needs a real thickness, 4PP must not ask for more power
  than its own hard stop, aux co-logging only exists for the continuous modes —
  and unknown or profile-owned override keys are rejected. The touch-safety
  keys are profile-owned here: whoever may not answer the hazardous-voltage
  prompt may not move its threshold or silence it for one run either.
"""
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..constants import MODE_TIMING_OVERRIDES
from ..formatting import format_power
from .settings_common import AuxSensorSettings, InstrumentSettings, SafetySettings
from .settings_modes import MODE_MODELS

logger = logging.getLogger(__name__)

#: Keys the profile always wins on, whatever a client sends (MW gather).
PROFILE_OWNED_KEYS = ('settling_time', 'gpib_address')

#: The touch-safety group. A strict request may not send any of these: the
#: hazardous-voltage prompt can only be answered by a person at the bench
#: (design note D4), and a request that raised the threshold or set the
#: silenced flag would never be asked. They change where the profile is
#: edited -- the Settings dialog, or the profile route -- and nowhere else.
#: The PySide6 gather path never sends them; it is left as it was.
SAFETY_KEYS = tuple(SafetySettings.model_fields)

#: Override keys that are not settings: they select a value rather than be one.
CONTROL_KEYS = ('vsource_run_continuous', 'isource_run_continuous')

#: Modes whose runs can co-log an auxiliary sensor (data_export.AUX_LOG_MODES).
AUX_LOG_MODES = ('resistance', 'source_v', 'source_i', 'four_point')


@dataclass(frozen=True)
class Issue:
    """One problem with the resolved settings.

    ``severity`` is ``'error'`` when the run must not start, ``'warning'`` when
    it may but the client should say so.
    """

    key: str
    message: str
    severity: str = 'error'


@dataclass
class ResolvedRun:
    """Settings a worker can consume, plus what the caller should know."""

    settings: Dict[str, Any]
    issues: List[Issue] = field(default_factory=list)
    derived: Dict[str, Any] = field(default_factory=dict)
    hazard: Optional[Any] = None

    @property
    def ok(self) -> bool:
        return not any(issue.severity == 'error' for issue in self.issues)


def allowed_override_keys(mode: str) -> set:
    """Keys a strict request may send for ``mode``.

    The mode's own fields plus the instrument and aux groups, minus the keys
    the profile owns -- the touch-safety group among them (``SAFETY_KEYS``).
    Clients discover this through the schema endpoint rather than by trial
    and error.
    """
    keys = set(MODE_MODELS[mode].model_fields)
    keys |= set(InstrumentSettings.model_fields)
    keys |= set(AuxSensorSettings.model_fields)
    keys |= set(CONTROL_KEYS)
    return keys - set(PROFILE_OWNED_KEYS) - set(SAFETY_KEYS)


def resolve_run_settings(profile: Dict[str, Any], mode: str,
                          overrides: Optional[Dict[str, Any]] = None,
                          *, strict: bool = False) -> ResolvedRun:
    """Resolve one run's settings. Same output as the GUI's gather step."""
    if mode not in MODE_MODELS:
        raise ValueError(f"Invalid mode specified: {mode}")
    overrides = dict(overrides or {})
    issues: List[Issue] = []

    # 1. Section copies, as gather does — the profile is never mutated.
    settings = {
        'measurement': dict(profile.get('measurement', {})),
        'display': dict(profile.get('display', {})),
        'file': dict(profile.get('file', {})),
        'output': dict(profile.get('output', {})),
    }
    m_cfg = settings['measurement']

    # 2. Strict requests may not invent keys or fight the profile.
    if strict:
        permitted = allowed_override_keys(mode)
        for key in sorted(overrides):
            if key in PROFILE_OWNED_KEYS:
                issues.append(Issue(key, f"'{key}' comes from the profile and cannot be overridden"))
            elif key in SAFETY_KEYS:
                issues.append(Issue(key, f"'{key}' is a touch-safety setting of the profile; "
                                         "a run request cannot change it"))
            elif key not in permitted:
                issues.append(Issue(key, f"'{key}' is not a setting of mode '{mode}'"))

        # The control keys choose a value, so no model sees them. Truthiness
        # is not good enough here: the string 'false' is truthy, and would
        # turn a bounded source-on run into an unbounded one.
        for key in CONTROL_KEYS:
            if key in overrides and not isinstance(overrides[key], bool):
                issues.append(Issue(key, f"'{key}' must be true or false, "
                                         f"not {overrides[key]!r}"))

    # 3. Apply the client's values (the GUI's widget reads).
    for key, value in overrides.items():
        if key not in CONTROL_KEYS:
            m_cfg[key] = value

    # 4. NPLC and sampling rate: the tab wins, else the profile.
    if 'nplc' not in overrides:
        m_cfg['nplc'] = profile['measurement']['nplc']
    if 'sampling_rate' not in overrides:
        m_cfg['sampling_rate'] = profile['measurement']['sampling_rate']

    # 5. auto_zero lives on the sensor tabs only; 'once' is the old default for
    #    profiles written before it existed.
    if 'auto_zero' not in overrides:
        m_cfg['auto_zero'] = profile['measurement'].get('auto_zero', 'once')

    # 6. "Run until stopped" checkboxes mean a duration of zero.
    #    A strict request's flag counts only when it is a real ``true``.
    def asked_to_run_until_stopped(key: str) -> bool:
        return overrides.get(key) is True if strict else bool(overrides.get(key))

    if mode == 'source_v' and asked_to_run_until_stopped('vsource_run_continuous'):
        m_cfg['vsource_duration_hours'] = 0.0
    if mode == 'source_i' and asked_to_run_until_stopped('isource_run_continuous'):
        m_cfg['isource_duration_hours'] = 0.0

    # 7. Profile-owned keys.
    m_cfg['settling_time'] = profile['measurement']['settling_time']
    m_cfg['gpib_address'] = profile['measurement']['gpib_address']
    if strict:
        # The request is already refused above; this makes the settings, the
        # hazard below and the run's own gate read the stored profile even if
        # a caller goes on to use a resolution that is not ``ok``.
        for key in SAFETY_KEYS:
            if key in profile['measurement']:
                m_cfg[key] = profile['measurement'][key]
            else:
                m_cfg.pop(key, None)

    # 8. Accuracy-critical modes force the slow, low-noise timing knobs last,
    #    exactly as gather does before the worker reads the config.
    for key, value in MODE_TIMING_OVERRIDES.get(mode, {}).items():
        m_cfg[key] = value

    # 9. Validate against the models; strict adds the GUI's Start-time checks.
    issues.extend(_validate(m_cfg, mode, strict=strict))

    # 10-11. Arithmetic only on values that validated. A key with an error
    #     has already been reported by name; reading it again would raise
    #     where the caller was promised issues -- and the GUI runs this on
    #     every gather, where a raise is a Start button that does nothing.
    failed = {issue.key for issue in issues if issue.severity == 'error'}
    return ResolvedRun(
        settings=settings,
        issues=issues,
        derived=_derive(m_cfg, mode, failed, issues),
        hazard=_hazard(settings, mode, failed, issues),
    )


def _model_issues(model, values: Dict[str, Any], *, strict: bool = False) -> List[Issue]:
    """Validate one group, reporting rather than raising.

    Lenient validation coerces a copy and leaves ``values`` alone: a stored
    profile passes through as it is. Strict validation refuses a value of the
    wrong JSON type (an int is still a fine float) and, when the group is
    valid, writes the validated values back, so ``1`` reaches the run as
    ``1.0`` and nothing reaches it that the model did not accept.
    """
    from pydantic import ValidationError

    subset = {name: values[name] for name in model.model_fields if name in values}
    # 'not measured' reaches the models as None; NaN stays in the settings dict
    # because that is what the worker and the F84 code read.
    unmeasured = 'fpp_temperature_c' in subset and _is_nan(subset['fpp_temperature_c'])
    if unmeasured:
        subset['fpp_temperature_c'] = None
    try:
        validated = model.model_validate(subset, strict=strict)
    except ValidationError as exc:
        return [
            Issue(str(error['loc'][0]) if error['loc'] else model.__name__, error['msg'])
            for error in exc.errors()
        ]
    if strict:
        for name in subset:
            if name == 'fpp_temperature_c' and unmeasured:
                continue  # stays NaN, as the profile wrote it
            values[name] = getattr(validated, name)
    return []


def _validate(m_cfg: Dict[str, Any], mode: str, *, strict: bool) -> List[Issue]:
    issues: List[Issue] = []
    for model in (MODE_MODELS[mode], InstrumentSettings, AuxSensorSettings, SafetySettings):
        issues.extend(_model_issues(model, m_cfg, strict=strict))
    if mode == 'four_point':
        issues.extend(_sample_geometry_issues(m_cfg))
    if not strict:
        return issues

    # The checks the GUI makes when Start is pressed. Each reads only values
    # the models accepted: strict typing has made those real numbers, and a
    # key that failed is already an issue.
    failed = {issue.key for issue in issues}
    if (mode == 'vdp' and 'vdp_thickness_cm' not in failed
            and not float(m_cfg.get('vdp_thickness_cm', 0.0)) > 0):
        issues.append(Issue('vdp_thickness_cm',
                             'van der Pauw needs a sample thickness greater than 0 cm'))
    if mode == 'four_point' and not failed.intersection(_POWER_KEYS):
        worst_case = _worst_case_power_w(m_cfg)
        stop_w = float(m_cfg.get('fpp_power_stop_w', 0.0))
        if stop_w and worst_case > stop_w:
            issues.append(Issue('fpp_power_stop_w',
                                 f"worst-case power {format_power(worst_case)} exceeds the "
                                 f"probe-safety hard stop {format_power(stop_w)}"))
    if m_cfg.get('aux_log_enabled') and mode not in AUX_LOG_MODES:
        issues.append(Issue('aux_log_enabled',
                             f"auxiliary co-logging is not available for mode '{mode}'"))
    return issues


def _sample_geometry_issues(m_cfg: Dict[str, Any]) -> List[Issue]:
    """The outline must be describable, and must not contradict the look-up.

    The per-sample correction still reads ``fpp_geometry`` and
    ``fpp_diameter_cm``; the ``fpp_sample_*`` keys only feed the position
    check of a spot. When the two describe different samples the run is
    allowed -- nothing about the measurement is wrong -- but the client is
    told, because the rows and the position check would otherwise disagree
    in silence.
    """
    from .spots import legacy_sample_geometry, same_outline, sample_geometry_from_settings

    try:
        outline = sample_geometry_from_settings(m_cfg)
        looked_up = legacy_sample_geometry(m_cfg)
    except (TypeError, ValueError) as exc:
        errors = exc.errors() if hasattr(exc, 'errors') else []
        message = errors[0]['msg'] if errors else str(exc)
        return [Issue('fpp_sample_shape', message)]
    if not same_outline(outline, looked_up):
        return [Issue('fpp_sample_shape',
                      "the per-sample correction uses fpp_geometry / fpp_diameter_cm "
                      f"({_describe(looked_up)}), not the fpp_sample_* outline "
                      f"({_describe(outline)}); only the position check uses the outline",
                      severity='warning')]
    return []


def _describe(outline) -> str:
    if outline.shape == 'circle':
        return f"circle, {outline.diameter_mm:g} mm"
    if outline.shape == 'rectangle':
        return f"rectangle, {outline.width_mm:g} x {outline.length_mm:g} mm"
    return 'unbounded'


#: What each derived value reads. One that names a key with an error is left
#: out of ``derived`` rather than computed from a value nobody accepted.
_TIMING_KEYS = ('nplc', 'auto_zero', 'filter_enabled', 'filter_type', 'filter_count',
                'res_offset_comp')
_SWEEP_POINT_KEYS = ('sweep_step', 'sweep_start', 'sweep_stop', 'sweep_direction')
_POWER_KEYS = ('fpp_current', 'fpp_voltage_compliance', 'fpp_power_stop_w')
_SWEEP_HAZARD_KEYS = ('sweep_source', 'sweep_start', 'sweep_stop')

#: What arithmetic on a settings value can raise.
_ARITHMETIC_ERRORS = (TypeError, ValueError, ArithmeticError)


def _max_rate_hz(m_cfg: Dict[str, Any]) -> float:
    from ..timing import TimingSettings

    return TimingSettings.from_dict(m_cfg).max_rate_hz()


def _sweep_points(m_cfg: Dict[str, Any]) -> Optional[int]:
    step = abs(float(m_cfg.get('sweep_step', 0.0)))
    if not step > 0:
        return None
    span = abs(float(m_cfg.get('sweep_stop', 0.0)) - float(m_cfg.get('sweep_start', 0.0)))
    points = round(span / step) + 1
    return points * 2 if m_cfg.get('sweep_direction') == 'up_down' else points


def _worst_case_power_w(m_cfg: Dict[str, Any]) -> float:
    return abs(float(m_cfg.get('fpp_current', 0.0))) * abs(
        float(m_cfg.get('fpp_voltage_compliance', 0.0)))


def _derive(m_cfg: Dict[str, Any], mode: str, failed: set,
            issues: List[Issue]) -> Dict[str, Any]:
    """Values a client would otherwise recompute: rate ceiling, points, power.

    Never raises. The model issues already cover every key read here, so the
    ``except`` is for a value that validates and still cannot be computed
    with; it is reported under the first key the computation reads.
    """
    wanted = [('max_rate_hz', _TIMING_KEYS, _max_rate_hz)]
    if mode == 'sweep':
        wanted.append(('sweep_points', _SWEEP_POINT_KEYS, _sweep_points))
    if mode == 'four_point':
        wanted.append(('worst_case_power_w', _POWER_KEYS[:2], _worst_case_power_w))

    derived: Dict[str, Any] = {}
    for name, keys, compute in wanted:
        if failed.intersection(keys):
            continue
        try:
            value = compute(m_cfg)
        except _ARITHMETIC_ERRORS as exc:
            issues.append(Issue(keys[0], f"{name} cannot be computed: {exc}"))
            continue
        if value is not None:
            derived[name] = value
    return derived


def _hazard(settings: Dict[str, Any], mode: str, failed: set, issues: List[Issue]):
    """Touch-safety check on the *resolved* values, not the stored profile.

    (A strict request cannot move the threshold or the silenced flag; see
    ``SAFETY_KEYS``.) ``None`` when the voltage it would judge has an error:
    in strict mode that run is refused anyway, and no answer is better than
    one computed from a value that is not a voltage. Never raises.
    """
    from ..safety import _MODE_VOLTAGE_KEYS, is_potentially_hazardous

    keys = (_MODE_VOLTAGE_KEYS[mode][0],) + (_SWEEP_HAZARD_KEYS if mode == 'sweep' else ())
    if failed.intersection(keys):
        return None
    try:
        return is_potentially_hazardous(settings, mode)
    except _ARITHMETIC_ERRORS as exc:
        issues.append(Issue(keys[0], f"the touch-safety check cannot read it: {exc}"))
        return None


def _is_nan(value: Any) -> bool:
    return isinstance(value, float) and math.isnan(value)

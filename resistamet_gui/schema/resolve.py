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
* **strict** (an API run request): the same issues, plus the checks the GUI
  makes at Start — vdP needs a real thickness, 4PP must not ask for more power
  than its own hard stop, aux co-logging only exists for the continuous modes —
  and unknown or profile-owned override keys are rejected.
"""
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..constants import MODE_TIMING_OVERRIDES
from .settings_common import AuxSensorSettings, InstrumentSettings, SafetySettings
from .settings_modes import MODE_MODELS

logger = logging.getLogger(__name__)

#: Keys the profile always wins on, whatever a client sends (MW gather).
PROFILE_OWNED_KEYS = ('settling_time', 'gpib_address')

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

    The mode's own fields plus the shared groups, minus the keys the profile
    owns. Clients discover this through the schema endpoint rather than by
    trial and error.
    """
    keys = set(MODE_MODELS[mode].model_fields)
    keys |= set(InstrumentSettings.model_fields)
    keys |= set(AuxSensorSettings.model_fields)
    keys |= set(SafetySettings.model_fields)
    keys |= set(CONTROL_KEYS)
    return keys - set(PROFILE_OWNED_KEYS)


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
            elif key not in permitted:
                issues.append(Issue(key, f"'{key}' is not a setting of mode '{mode}'"))

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
    if mode == 'source_v' and overrides.get('vsource_run_continuous'):
        m_cfg['vsource_duration_hours'] = 0.0
    if mode == 'source_i' and overrides.get('isource_run_continuous'):
        m_cfg['isource_duration_hours'] = 0.0

    # 7. Profile-owned keys.
    m_cfg['settling_time'] = profile['measurement']['settling_time']
    m_cfg['gpib_address'] = profile['measurement']['gpib_address']

    # 8. Accuracy-critical modes force the slow, low-noise timing knobs last,
    #    exactly as gather does before the worker reads the config.
    for key, value in MODE_TIMING_OVERRIDES.get(mode, {}).items():
        m_cfg[key] = value

    # 9. Validate against the models; strict adds the GUI's Start-time checks.
    issues.extend(_validate(m_cfg, mode, strict=strict))

    return ResolvedRun(
        settings=settings,
        issues=issues,
        derived=_derive(m_cfg, mode),
        hazard=_hazard(settings, mode),
    )


def _model_issues(model, values: Dict[str, Any]) -> List[Issue]:
    """Validate one group, reporting rather than raising."""
    from pydantic import ValidationError

    subset = {name: values[name] for name in model.model_fields if name in values}
    # 'not measured' reaches the models as None; NaN stays in the settings dict
    # because that is what the worker and the F84 code read.
    if 'fpp_temperature_c' in subset and _is_nan(subset['fpp_temperature_c']):
        subset['fpp_temperature_c'] = None
    try:
        model(**subset)
    except ValidationError as exc:
        return [
            Issue(str(error['loc'][0]) if error['loc'] else model.__name__, error['msg'])
            for error in exc.errors()
        ]
    return []


def _validate(m_cfg: Dict[str, Any], mode: str, *, strict: bool) -> List[Issue]:
    issues: List[Issue] = []
    for model in (MODE_MODELS[mode], InstrumentSettings, AuxSensorSettings, SafetySettings):
        issues.extend(_model_issues(model, m_cfg))
    if mode == 'four_point':
        issues.extend(_sample_geometry_issues(m_cfg))
    if not strict:
        return issues

    # The checks the GUI makes when Start is pressed.
    if mode == 'vdp' and not float(m_cfg.get('vdp_thickness_cm', 0.0)) > 0:
        issues.append(Issue('vdp_thickness_cm',
                             'van der Pauw needs a sample thickness greater than 0 cm'))
    if mode == 'four_point':
        worst_case = abs(float(m_cfg.get('fpp_current', 0.0))) * abs(
            float(m_cfg.get('fpp_voltage_compliance', 0.0)))
        stop_w = float(m_cfg.get('fpp_power_stop_w', 0.0))
        if stop_w and worst_case > stop_w:
            issues.append(Issue('fpp_power_stop_w',
                                 f"worst-case power {worst_case * 1e3:.1f} mW exceeds the "
                                 f"probe-safety hard stop {stop_w * 1e3:.0f} mW"))
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


def _derive(m_cfg: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """Values a client would otherwise recompute: rate ceiling, points, power."""
    from ..timing import TimingSettings

    derived: Dict[str, Any] = {
        'max_rate_hz': TimingSettings.from_dict(m_cfg).max_rate_hz(),
    }
    if mode == 'sweep':
        step = abs(float(m_cfg.get('sweep_step', 0.0)))
        if step > 0:
            span = abs(float(m_cfg.get('sweep_stop', 0.0)) - float(m_cfg.get('sweep_start', 0.0)))
            points = round(span / step) + 1
            if m_cfg.get('sweep_direction') == 'up_down':
                points *= 2
            derived['sweep_points'] = points
    if mode == 'four_point':
        derived['worst_case_power_w'] = abs(float(m_cfg.get('fpp_current', 0.0))) * abs(
            float(m_cfg.get('fpp_voltage_compliance', 0.0)))
    return derived


def _hazard(settings: Dict[str, Any], mode: str):
    """Touch-safety check on the *resolved* values, not the stored profile."""
    from ..safety import is_potentially_hazardous

    return is_potentially_hazardous(settings, mode)


def _is_nan(value: Any) -> bool:
    return isinstance(value, float) and math.isnan(value)

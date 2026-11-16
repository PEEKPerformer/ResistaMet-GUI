"""What each key a run accepts is: its type, choices, bounds, default and unit.

``GET /schema/settings`` serves this per mode so that a client -- the MCP
server's ``describe_mode`` above all -- can say what a setting accepts
before a request is refused for it. Everything here is read off the
pydantic models; nothing is typed twice. The unit is the one thing the
models do not carry: it follows from the key's name, by the convention
the keys already keep (``…_cm``, ``…_hours``, ``…_current``), and where
the name does not say, it is left out rather than guessed.

Pure: no Qt, no instrument, no I/O.
"""
from functools import lru_cache
from typing import Any, Dict, Optional

from ..constants import MODE_TIMING_OVERRIDES
from .resolve import CONTROL_KEYS_BY_MODE, allowed_override_keys
from .settings_common import AuxSensorSettings, InstrumentSettings
from .settings_modes import MODE_MODELS

#: Units by the end of the key. Longest suffixes first: ``_voltage_compliance``
#: must win over ``_compliance``.
_SUFFIX_UNITS = (
    ('_voltage_compliance', 'V'),
    ('_current_compliance', 'A'),
    ('_duration_hours', 'h'),
    ('_test_current', 'A'),
    ('_current', 'A'),
    ('_voltage', 'V'),
    ('_hours', 'h'),
    ('_settling', 's'),
    ('_delay', 's'),
    ('_pct', '%'),
    ('_deg', 'deg'),
    ('_cm', 'cm'),
    ('_um', 'um'),
    ('_mm', 'mm'),
    ('_w', 'W'),
    ('_s', 's'),
    ('_c', 'degC'),
)

#: Keys whose name does not end in their unit.
_KEY_UNITS = {
    'nplc': 'PLC',
    'sampling_rate': 'Hz',
    'res_cable_null': 'ohm',
    # A sweep's values are in the unit of what it sources or measures.
    'sweep_start': 'V or A (sweep_source)',
    'sweep_stop': 'V or A (sweep_source)',
    'sweep_step': 'V or A (sweep_source)',
    'sweep_compliance': 'A or V (the measured quantity)',
}

#: The control keys are not fields of a model; they are described here.
_CONTROL_FRAGMENTS = {
    'vsource_run_continuous': {
        'type': 'boolean', 'default': False,
        'description': "true: run until stopped, as vsource_duration_hours 0 does; "
                       "false changes nothing. Redundant for a request; it is the "
                       "window's \"Run until stopped\" box, which keeps the hours it "
                       "greys out."},
    'isource_run_continuous': {
        'type': 'boolean', 'default': False,
        'description': "true: run until stopped, as isource_duration_hours 0 does; "
                       "false changes nothing. Redundant for a request; it is the "
                       "window's \"Run until stopped\" box, which keeps the hours it "
                       "greys out."},
}

#: Fragment keys a client has no use for: the title is the key in title case.
_DROPPED = ('title',)


def unit_of(key: str) -> Optional[str]:
    """The unit a key's value is in, from its name; None when the name does not say."""
    if key in _KEY_UNITS:
        return _KEY_UNITS[key]
    for suffix, unit in _SUFFIX_UNITS:
        if key.endswith(suffix):
            return unit
    return None


def _flatten(fragment: Dict[str, Any]) -> Dict[str, Any]:
    """One JSON-schema property as a flat dict.

    ``Optional[float]`` arrives as ``anyOf: [{number, bounds}, {null}]``:
    the bounds move up and ``nullable`` says null is allowed. A single
    allowed value (``const``) is given as a one-item ``enum``, as the
    other choices are.
    """
    flat = {key: value for key, value in fragment.items()
            if key not in _DROPPED and key != 'anyOf'}
    options = fragment.get('anyOf') or []
    if options:
        for option in options:
            if option.get('type') == 'null':
                flat['nullable'] = True
            else:
                flat.update({k: v for k, v in option.items() if k not in _DROPPED})
    if 'const' in flat and 'enum' not in flat:
        flat['enum'] = [flat.pop('const')]
    return flat


@lru_cache(maxsize=None)
def _properties(model) -> Dict[str, Dict[str, Any]]:
    return model.model_json_schema()['properties']


def key_descriptions(mode: str) -> Dict[str, Dict[str, Any]]:
    """Each override key of ``mode``: its JSON-schema fragment, flattened, and its unit."""
    models = (MODE_MODELS[mode], InstrumentSettings, AuxSensorSettings)
    described: Dict[str, Dict[str, Any]] = {}
    for key in sorted(allowed_override_keys(mode)):
        if key in CONTROL_KEYS_BY_MODE.get(mode, ()):
            fragment = dict(_CONTROL_FRAGMENTS[key])
        else:
            owner = next(model for model in models if key in model.model_fields)
            fragment = _flatten(_properties(owner)[key])
        unit = unit_of(key)
        if unit:
            fragment['unit'] = unit
        described[key] = fragment
    return described


def fixed_values(mode: str) -> Dict[str, Any]:
    """Keys a run of ``mode`` always runs with, whatever the profile or request says."""
    return dict(MODE_TIMING_OVERRIDES.get(mode, {}))

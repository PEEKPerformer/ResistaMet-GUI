"""One line per setting: what ``describe_mode`` tells an agent about a key.

The backend's schema route gives each key of a mode as a JSON-schema
fragment (type, choices, bounds, default, description, unit) and the
values the mode fixes; the resolve route gives the value a user's run
would have. This turns the three into one short line per key, e.g.::

    res_test_current: 0.001 A, from the profile; default 0.0001; 1e-07 <= x <= 3
    auto_zero: "on", fixed by the mode (the profile's "once" is not used); ...

so an agent learns what a key accepts and where its value comes from
before a request is refused for it. Pure functions over the replies; no
HTTP here and nothing from the backend's packages.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Optional

#: Said once with the lines, so each line can stay short.
HOW_TO_READ = (
    "Each key: the value a run would have (in its unit) and where it comes from, the "
    "default, what it accepts, and what it means. 'from the profile': the user's stored "
    "value, which is the default where the profile never set it. 'fixed by the mode': "
    "every run of this mode uses it, whatever the profile or overrides say. Change a "
    "value for one run with overrides in check_settings and start_run.")

_MISSING = object()


def show(value: Any) -> str:
    """A value as JSON: strings quoted, true/false/null as JSON spells them.

    A stored NaN (a temperature not measured) is null, as on the wire.
    """
    return json.dumps(_plain(value), ensure_ascii=False)


def _plain(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _number(value: Any) -> str:
    """A bound: an integer as written, a float in its shortest form."""
    return str(value) if isinstance(value, int) else f"{value:g}"


def accepts(fragment: Dict[str, Any]) -> Optional[str]:
    """What a key accepts, from its schema fragment: choices or bounds."""
    kind = fragment.get('type')
    if 'enum' in fragment:
        text = 'one of ' + '|'.join(str(choice) for choice in fragment['enum'])
    elif kind == 'boolean':
        text = 'true|false'
    elif kind in ('number', 'integer'):
        low = high = None
        if 'minimum' in fragment:
            low = f"{_number(fragment['minimum'])} <= "
        elif 'exclusiveMinimum' in fragment:
            low = f"{_number(fragment['exclusiveMinimum'])} < "
        if 'maximum' in fragment:
            high = f" <= {_number(fragment['maximum'])}"
        elif 'exclusiveMaximum' in fragment:
            high = f" < {_number(fragment['exclusiveMaximum'])}"
        text = f"{low or ''}x{high or ''}" if (low or high) else 'a number'
        if kind == 'integer':
            text = 'integer ' + text
    elif kind == 'string':
        text = 'text'
    else:
        return None
    if fragment.get('nullable'):
        text += ', or null'
    return text


def line(fragment: Dict[str, Any], value: Any = _MISSING, *, fixed: bool = False,
         profile_value: Any = _MISSING) -> str:
    """One key's line. ``value``: the resolved value, if a user was given.

    ``fixed``: the mode always runs with ``value``; ``profile_value`` is what
    the profile holds, named when it differs, so the agent sees why the
    profile and the run disagree.
    """
    if value is not _MISSING:
        value = _plain(value)
    unit = fragment.get('unit')
    plain_unit = unit if unit and ' ' not in unit else None
    parts = []
    if value is not _MISSING:
        text = show(value)
        if plain_unit and value is not None:
            text += f" {plain_unit}"
        if fixed:
            text += ", fixed by the mode"
            if profile_value is not _MISSING and profile_value != value:
                text += f" (the profile's {show(profile_value)} is not used)"
        else:
            text += ", from the profile"
            if 'default' in fragment and fragment['default'] == value:
                text += " (the default)"
        parts.append(text)
    elif plain_unit:
        parts.append(f"in {plain_unit}")
    if unit and not plain_unit:
        parts.append(f"in {unit}")
    if not fixed and 'default' in fragment and (value is _MISSING
                                                or fragment['default'] != value):
        parts.append(f"default {show(fragment['default'])}")
    allowed = accepts(fragment)
    if allowed and not fixed:
        parts.append(allowed)
    if fragment.get('description'):
        parts.append(str(fragment['description']))
    return '; '.join(parts)


def describe(entry: Dict[str, Any], measurement: Optional[Dict[str, Any]] = None,
             profile: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, str]]:
    """The lines of one mode: its own keys, then the keys every mode shares.

    ``entry`` is the schema route's entry for the mode; ``measurement`` the
    resolved measurement settings of a user's run, or None for no user;
    ``profile`` the user's stored measurement section, for fixed keys.
    """
    keys: Dict[str, Dict[str, Any]] = entry.get('keys', {})
    fixed: Dict[str, Any] = entry.get('fixed', {})
    own = set(entry.get('fields', []))
    lines: Dict[str, Dict[str, str]] = {'mode_keys': {}, 'shared_keys': {}}
    for key in entry.get('override_keys', sorted(keys)):
        fragment = keys.get(key, {})
        kwargs: Dict[str, Any] = {'fixed': key in fixed}
        if measurement is not None and key in measurement:
            kwargs['value'] = measurement[key]
        elif key in fixed:
            kwargs['value'] = fixed[key]
        if key in fixed and profile is not None and key in profile:
            kwargs['profile_value'] = _plain(profile[key])
        group = 'mode_keys' if key in own or key.endswith('_run_continuous') else 'shared_keys'
        lines[group][key] = line(fragment, **kwargs)
    return lines

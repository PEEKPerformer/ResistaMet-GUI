"""The most a run could put on the device, and whether an agent may start it.

``docs/design/mcp_layer.md`` M4: a run started by the ``agent`` role is held
to the profile's ``agent_limits`` and, when the backend knows which model is
connected, to that model's ``ModelSpec``. Both checks read the worst case
computed here from the *resolved* settings -- the values the run will use,
the same ones the touch-safety check (``safety.py``) judges.

The worst case is per quantity, from the settings alone:

* the largest |V| the run could put on the device: what it sources when it
  sources voltage, the compliance when it sources current (an open circuit
  drives a current source up to its compliance);
* the largest |I|, the other way round;
* the largest power, |V| x |I| at that corner. Conservative: the device
  cannot be at both limits at once unless its resistance is exactly V/I,
  and the check does not assume anything about the device.

Pure: no Qt, no pyvisa, no API. The route decides who is checked; this only
says what a run could do and how that compares.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple

from pydantic import BaseModel, ValidationError

from ..safety import _MODE_VOLTAGE_KEYS
from .settings_common import AgentLimitSettings
from .settings_modes import MODE_MODELS

#: The largest voltage auto-ohms may put on the device, whatever the request.
#: Auto-ohms (``res_auto_range``) sets its own voltage limit and moves it with
#: the ohms range: its top ranges measure up to 20 V (Keithley 2400 user's
#: manual, Table 4-1; see ``session/configure.py``), and 21 V is that range
#: with the family's 105 % overrange.
AUTO_OHMS_MAX_V = 21.0

#: The setting that names the current on the leads, per mode: the sourced
#: current, or the current compliance of a voltage source. The voltage side
#: is ``safety._MODE_VOLTAGE_KEYS``, so the two checks read the same key.
#: A sweep's sourced side is its range, handled in ``worst_case``.
_MODE_CURRENT_KEYS = {
    'resistance': 'res_test_current',
    'source_v': 'vsource_current_compliance',
    'source_i': 'isource_current',
    'four_point': 'fpp_current',
    'vdp': 'vdp_current',
}

LimitName = Literal['max_voltage_v', 'max_current_a', 'max_power_w',
                    'max_source_v', 'max_source_i']


@dataclass(frozen=True)
class WorstCase:
    """The largest |V|, |I| and V x I a run could put on the device.

    ``None`` is a quantity the instrument chooses rather than the settings:
    the test current of auto-ohms. Each quantity carries the setting keys it
    comes from, so a refusal can name what to change.
    """

    voltage_v: Optional[float]
    current_a: Optional[float]
    voltage_keys: Tuple[str, ...]
    current_keys: Tuple[str, ...]

    @property
    def power_w(self) -> Optional[float]:
        if self.voltage_v is None or self.current_a is None:
            return None
        return self.voltage_v * self.current_a

    @property
    def power_keys(self) -> Tuple[str, ...]:
        return tuple(dict.fromkeys(self.voltage_keys + self.current_keys))


def worst_case(settings: Mapping[str, Any], mode: str) -> WorstCase:
    """The worst case of a run of ``mode`` with these resolved settings.

    ``settings`` is the run's settings dict, with a ``measurement`` section,
    as the resolver returns it. A key the section lacks reads as the mode
    model's default, which is what validation assumed for it.
    """
    if mode not in MODE_MODELS:
        raise ValueError(f"unknown mode '{mode}'")
    m = settings.get('measurement', {})

    def magnitude(key: str) -> float:
        if key in m:
            return abs(float(m[key]))
        return abs(float(MODE_MODELS[mode].model_fields[key].default))

    if mode == 'sweep':
        # Up, down or up-and-down, the sweep engine only steps between start
        # and stop (``Keithley2400.setup_sweep``), so the range is bounded by
        # whichever end is further from zero. The compliance is the other
        # quantity: a current when sourcing voltage, a voltage when sourcing
        # current (``SweepSettings``). Which is which is read as the
        # touch-safety check reads it.
        start, stop = magnitude('sweep_start'), magnitude('sweep_stop')
        ends = (('sweep_start',) if start > stop else
                ('sweep_stop',) if stop > start else ('sweep_start', 'sweep_stop'))
        sourced, compliance = max(start, stop), magnitude('sweep_compliance')
        if str(m.get('sweep_source', 'voltage')).lower().startswith('v'):
            return WorstCase(sourced, compliance, ends, ('sweep_compliance',))
        return WorstCase(compliance, sourced, ('sweep_compliance',), ends)

    voltage_key = _MODE_VOLTAGE_KEYS[mode][0]
    current_key = _MODE_CURRENT_KEYS[mode]
    voltage, current = magnitude(voltage_key), magnitude(current_key)

    if mode == 'resistance' and bool(m.get(
            'res_auto_range', MODE_MODELS[mode].model_fields['res_auto_range'].default)):
        # Auto-ohms replaces both numbers the request set: it chooses the
        # test current per range (100 mA was seen on a 2420's 20 ohm range
        # for a 1 mA request) and its own voltage limit. The voltage still
        # has a ceiling; the current has none the settings can give.
        if voltage < AUTO_OHMS_MAX_V:
            return WorstCase(AUTO_OHMS_MAX_V, None, ('res_auto_range',), ('res_auto_range',))
        return WorstCase(voltage, None, (voltage_key,), ('res_auto_range',))

    # Four-point probe: fpp_power_stop_w does not lower this. It acts on a
    # measured reading, after the power has been delivered, and the resolver
    # already refuses a request whose V_comp x I is above it; the corner is
    # what the probe could see before the stop fires.
    return WorstCase(voltage, current, (voltage_key,), (current_key,))


class AgentLimitViolation(BaseModel):
    """One way a run goes beyond what an agent may start.

    ``limit`` names the bound: one of the profile's ``agent_limits`` keys
    when ``source`` is ``'agent_limits'``, a ``ModelSpec`` field when it is
    ``'model'`` (and ``model`` says which). ``keys`` are the run's setting
    keys that produce ``value``; ``value`` is None when the instrument
    chooses the quantity itself, and ``allowed`` is None when the profile's
    limit is itself not a valid number.
    """

    limit: LimitName
    source: Literal['agent_limits', 'model']
    model: Optional[str] = None
    keys: List[str]
    value: Optional[float]
    allowed: Optional[float]
    message: str


class AgentLimitCheck(BaseModel):
    """The verdict on a run an agent would start. ``ok`` when nothing is beyond."""

    ok: bool
    violations: List[AgentLimitViolation]


#: Each quantity: the agent limit, the model's limit, the unit, the word.
_QUANTITIES = (
    ('max_voltage_v', 'max_source_v', 'V', 'voltage'),
    ('max_current_a', 'max_source_i', 'A', 'current'),
    ('max_power_w', 'max_power_w', 'W', 'power'),
)


def check_agent_limits(settings: Mapping[str, Any], mode: str,
                       limits: Optional[Mapping[str, Any]] = None,
                       instrument: Optional[Mapping[str, Any]] = None) -> AgentLimitCheck:
    """Whether a run with these resolved settings is within an agent's reach.

    ``limits`` is the profile's ``agent_limits`` section; a missing key, or a
    missing section, is the default. ``instrument`` is what the backend knows
    of the connected model (``InstrumentInfo``), or None when it knows
    nothing; then only the profile's limits are checked, and the instrument
    enforces its own. A value exactly at a limit is within it.
    """
    case = worst_case(settings, mode)
    values = {'voltage': (case.voltage_v, case.voltage_keys),
              'current': (case.current_a, case.current_keys),
              'power': (case.power_w, case.power_keys)}
    caps, violations = _read_limits(limits)

    for limit, _model_limit, unit, quantity in _QUANTITIES:
        cap = caps.get(limit)
        if cap is None:
            continue
        value, keys = values[quantity]
        if value is None:
            # Only a current is ever the instrument's choice (auto-ohms).
            violations.append(AgentLimitViolation(
                limit=limit, source='agent_limits', keys=list(keys), value=None, allowed=cap,
                message=f"the instrument chooses the current while "
                        f"{_names(case.current_keys)} is on, so the run cannot be held "
                        f"to {limit} = {cap:g} {unit}"))
        elif value > cap:
            violations.append(AgentLimitViolation(
                limit=limit, source='agent_limits', keys=list(keys), value=value, allowed=cap,
                message=f"{quantity} {value:g} {unit} ({_names(keys)}) is above the agent "
                        f"limit {limit} = {cap:g} {unit}"))

    model = (instrument or {}).get('model')
    if model is not None:
        for _limit, model_limit, unit, quantity in _QUANTITIES:
            ceiling = instrument.get(model_limit)
            value, keys = values[quantity]
            # A quantity the instrument chooses, it chooses within its range.
            if ceiling is None or value is None or not value > ceiling:
                continue
            violations.append(AgentLimitViolation(
                limit=model_limit, source='model', model=model, keys=list(keys),
                value=value, allowed=ceiling,
                message=f"{quantity} {value:g} {unit} ({_names(keys)}) is above the "
                        f"Keithley {model}'s {model_limit} = {ceiling:g} {unit}"))

    return AgentLimitCheck(ok=not violations, violations=violations)


def _read_limits(limits: Optional[Mapping[str, Any]]):
    """The profile's caps, and a violation for each that is not a valid one.

    A stored profile is not refused for a bad value elsewhere, but a cap that
    is not a number cannot be honoured, and reading it as "no cap" would
    widen what the operator set. Such a cap refuses every agent run until it
    is fixed in Settings.
    """
    defaults = AgentLimitSettings()
    caps: Dict[str, Optional[float]] = {}
    violations: List[AgentLimitViolation] = []
    for name in AgentLimitSettings.model_fields:
        raw = (limits or {}).get(name, getattr(defaults, name))
        try:
            caps[name] = getattr(AgentLimitSettings.model_validate({name: raw}), name)
        except ValidationError as exc:
            violations.append(AgentLimitViolation(
                limit=name, source='agent_limits', keys=[], value=None, allowed=None,
                message=f"the profile's agent limit {name} = {raw!r} is not valid "
                        f"({exc.errors()[0]['msg']}); no agent run can start until it "
                        f"is fixed"))
            caps[name] = None
    return caps, violations


def _names(keys) -> str:
    return ', '.join(keys)

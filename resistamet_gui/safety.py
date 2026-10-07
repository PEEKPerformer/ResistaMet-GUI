"""Human-touch-safety voltage warnings for ResistaMet GUI.

Keithley 2400-series instruments can compliance-clamp at 60–1100 V
depending on model — well above the IEC 61010-1 SELV upper bound of
30 V DC ("safe to touch under any skin condition"). Existing
``fpp_power_stop_w`` protects the sample and probe from Joule heating;
nothing in the GUI protects a person from shock if they grab a lead
with a hot output.

This module exposes a pure-data check the GUI uses to decide whether
to warn before a measurement starts. Threshold is per-user-profile
(``safety_voltage_warn_v``, default 30 V; ``0`` disables). A sticky
``safety_voltage_warn_silenced`` lets power users acknowledge once
and never see the modal again — the Settings dialog has a re-enable
toggle so paranoid users / new technicians can flip warnings back on
without JSON editing. ``safety_voltage_warn_silenced_until`` silences
it for a while instead: a Unix time after which the warning is back on
its own. :func:`warning_silenced` is the one place either is read.

The *compliance* voltage matters, not the sourced — an open-circuit
current source swings up to compliance, so even a 1 mA test current
can put 200 V on the leads if compliance is set there.

Pure module: no Qt, no pyvisa. Safe to import from anywhere.

Out of scope: AC voltage handling (Keithley 2400 family is DC-only
here), arc-flash / burn warnings from low-V high-I configurations,
hardware interlock integration. Flagged for later if needed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Optional


# Default human-touch-safety threshold. IEC 61010-1 calls 30 V DC the
# SELV upper bound — below this, touch hazard is negligible under any
# realistic skin-resistance condition. The 2400's lowest-V model (2401)
# tops out at 21 V so its compliance ceiling won't trip this. Everything
# else in the family can exceed it.
DEFAULT_THRESHOLD_V = 30.0

#: The longest a timed silence may run. A year is long enough for anyone who
#: means "for now"; anyone who means "for ever" has the sticky flag.
MAX_SILENCE_DAYS = 365.0

SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class HazardCheck:
    """Result of :func:`is_potentially_hazardous`.

    Attributes:
        hazardous: ``True`` when the configured compliance / source
            voltage could reach the touch-safety threshold.
        voltage_v: The voltage value used in the check (compliance or
            sourced, whichever was the gating factor). NaN if the mode
            doesn't put a voltage on the leads.
        threshold_v: The threshold the value was compared against.
        reason: Short human-readable label for the gating quantity
            ("V compliance", "Source V", etc.) — used in the dialog.
    """
    hazardous: bool
    voltage_v: float
    threshold_v: float
    reason: str


# Settings keys (subkeys of measurement_settings) that name the voltage
# that ends up on the leads for each mode. We pull these from the
# settings dict so the check stays portable across UI / worker / tests.
_MODE_VOLTAGE_KEYS = {
    'resistance': ('res_voltage_compliance', 'V compliance'),
    'source_v':   ('vsource_voltage',         'Source V'),
    'source_i':   ('isource_voltage_compliance', 'V compliance'),
    'four_point': ('fpp_voltage_compliance',  'V compliance'),
    'vdp':        ('vdp_voltage_compliance',  'V compliance'),
    # I-V sweep: the upper bound of the sweep matters for source-V
    # sweeps; we approximate as max(|start|, |stop|).
    'sweep':      ('sweep_compliance',        'V compliance'),
}


def is_potentially_hazardous(
    settings: Mapping,
    mode: str,
    threshold_v: Optional[float] = None,
) -> HazardCheck:
    """Decide whether a configured run could exceed touch-safe voltage.

    Args:
        settings: The full settings mapping (with a 'measurement'
            sub-mapping) — typically ``self.user_settings`` in the GUI.
        mode: One of 'resistance', 'source_v', 'source_i', 'four_point',
            'vdp', 'sweep'. Unknown modes return a not-hazardous result
            with NaN voltage.
        threshold_v: Override the threshold. Falls through to the
            per-user ``safety_voltage_warn_v`` from settings, then
            :data:`DEFAULT_THRESHOLD_V`. A threshold of 0 disables the
            check entirely (returns ``hazardous=False``).

    Returns:
        :class:`HazardCheck` describing the result. Always returns a
        valid object — NaN / missing settings yield not-hazardous.
    """
    m = settings.get('measurement', {}) if isinstance(settings, Mapping) else {}
    if threshold_v is None:
        try:
            threshold_v = float(m.get('safety_voltage_warn_v', DEFAULT_THRESHOLD_V))
        except (TypeError, ValueError):
            threshold_v = DEFAULT_THRESHOLD_V

    # 0 disables the check entirely (per the design memo).
    if not threshold_v or threshold_v <= 0:
        return HazardCheck(False, float('nan'), threshold_v or 0.0, 'disabled')

    info = _MODE_VOLTAGE_KEYS.get(mode)
    if info is None:
        return HazardCheck(False, float('nan'), threshold_v, 'unknown mode')
    key, reason = info

    # I-V sweep: take the maximum |start|, |stop| when source is voltage;
    # otherwise the compliance is the gating value.
    if mode == 'sweep':
        if str(m.get('sweep_source', 'voltage')).lower().startswith('v'):
            start = abs(float(m.get('sweep_start', 0.0) or 0.0))
            stop = abs(float(m.get('sweep_stop', 0.0) or 0.0))
            value = max(start, stop)
            reason = 'Sweep V range'
        else:
            value = abs(float(m.get(key, 0.0) or 0.0))
    else:
        try:
            value = abs(float(m.get(key, 0.0) or 0.0))
        except (TypeError, ValueError):
            value = float('nan')

    if not math.isfinite(value):
        return HazardCheck(False, value, threshold_v, reason)
    return HazardCheck(value >= threshold_v, value, threshold_v, reason)


def warning_silenced(settings: Mapping, now: float) -> bool:
    """Whether the profile has silenced the touch-safety warning at ``now``.

    Silenced when ``safety_voltage_warn_silenced`` is set, or while
    ``safety_voltage_warn_silenced_until`` (Unix time) is still ahead of
    ``now``. An expired time is simply ignored rather than cleared, so
    nothing has to write the profile for the warning to come back.

    The session runs and the PySide6 window both ask here, so the two
    cannot disagree about whether a person is warned. A run an agent
    started never asks at all: the silence is a person's choice for their
    own runs (``docs/design/mcp_layer.md`` M5).

    Args:
        settings: The full settings mapping, with a 'measurement'
            sub-mapping, as for :func:`is_potentially_hazardous`.
        now: The current Unix time, passed in so callers and tests decide
            the clock.
    """
    m = settings.get('measurement', {}) if isinstance(settings, Mapping) else {}
    if bool(m.get('safety_voltage_warn_silenced', False)):
        return True
    until = m.get('safety_voltage_warn_silenced_until')
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        return False
    return math.isfinite(until) and until > now


def silence_note(fields: Mapping) -> Optional[str]:
    """The log line for a touch-safety answer that asked for a silence.

    None when it asked for none. The run records the request on its event
    stream; saving it to the profile is for whoever owns the profile file
    (the API, as it takes the answer).
    """
    if fields.get('silence_for_profile') is True:
        return "Touch-safety warning silenced for this profile."
    days = fields.get('silence_for_days')
    if isinstance(days, (int, float)) and not isinstance(days, bool) \
            and math.isfinite(days) and days > 0:
        return f"Touch-safety warning silenced for this profile for {days:g} days."
    return None


def warning_message(check: HazardCheck) -> str:
    """Plain-text body for a warning dialog. Caller wraps in a QMessageBox."""
    return (
        f"{check.reason} = {check.voltage_v:g} V is at or above the "
        f"{check.threshold_v:g} V touch-safety threshold (IEC 61010-1 SELV).\n\n"
        "If you grab a probe lead while the output is on, you could be shocked.\n"
        "Make sure the DUT is properly enclosed or that you and any observers "
        "stay clear of the leads while the run is active.\n\n"
        "This warning is informational; the measurement will proceed."
    )

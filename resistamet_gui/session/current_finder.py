"""The 4PP pre-run current finder: probe the sample, choose the source current.

Runs between the configure step and the data file, so the current it chooses
is the one in the file name and the header. It delta-probes at the requested
current, takes R and the scatter of the reversal pairs, and asks
``calculations.select_four_point_current`` for the smallest current that
reaches the target significant figures. Anything short of that is a question
for whoever started the run, asked through the session prompt with the output
off.

Two rules about what the probe reads:

- The current is always the programmed one. With only VOLT sensed, the CURR
  element of ``:FORM:ELEM VOLT,CURR,STAT`` is not a measurement and is not a
  reliable echo after the level or the range changes (see
  ``samples._commanded``). A source in compliance therefore has an unknown
  current: that is reported as "in compliance at this current", a bound, and
  never divided into a resistance.
- The reversal pair that follows a ``:SOUR:CURR:RANG`` is read and thrown
  away.

No Qt here.
"""
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..calculations import (
    calculate_four_point_probe_bound, delta_probe_statistics, four_point_current_ceiling,
    four_point_method, four_point_power_level, four_point_worst_case_power_w,
    select_four_point_current,
)
from ..constants import (
    FPP_AUTOSELECT_MIN_CURRENT, FPP_AUTOSELECT_MIN_SETTLE_S, FPP_AUTOSELECT_PROBE_CYCLES,
    FPP_AUTOSELECT_PROBE_DISCARD_CYCLES, FPP_AUTOSELECT_RESOLUTION_FLOOR,
    KEITHLEY_STAT_BIT_COMPLIANCE,
)
from ..data_export import effective_filter_type
from ..formatting import format_power, four_point_power_warning

PROMPT_KIND = 'fpp_current_decision'


@dataclass(frozen=True)
class Probe:
    """One delta probe at one programmed current."""

    current: float
    resistance: float
    sigma_v: float
    in_compliance: bool


@dataclass(frozen=True)
class Finding:
    """What the finder concluded: the current the run would use, and why.

    ``bound`` marks a sample still in compliance at the smallest current:
    ``resistance`` is then a lower bound and nothing else was measured.
    """

    verdict: str
    current: float
    reason: str
    resistance: float
    expected_voltage: float
    noise_floor_v: Optional[float] = None
    snr: Optional[float] = None
    sig_figs: Optional[float] = None
    bound: bool = False


def enabled(mode: str, measurement_settings) -> bool:
    return mode == 'four_point' and bool(measurement_settings.get('fpp_autoselect_current', False))


def _read_v_stat(keithley):
    """One reading: the sensed voltage and the status word. Never the CURR element."""
    parts = [p.strip() for p in keithley.query(":READ?").strip().split(',')]
    return float(parts[0]), int(float(parts[-1]))


def probe_delta(keithley, control, current: float, settle_s: float) -> Probe:
    """Delta-probe at ``current``, on that current's source range.

    The output must be on. Leaves the source at +current. A stop during a
    settle raises RunStopped; the caller turns the output off.
    """
    i_mag = abs(current)
    settle_s = max(settle_s, FPP_AUTOSELECT_MIN_SETTLE_S)
    keithley.write(f":SOUR:CURR:RANG {i_mag}")
    v_deltas = []
    in_compliance = False
    for cycle in range(FPP_AUTOSELECT_PROBE_DISCARD_CYCLES + FPP_AUTOSELECT_PROBE_CYCLES):
        keithley.write(f":SOUR:CURR {i_mag}")
        control.sleep(settle_s)
        v_plus, stat_plus = _read_v_stat(keithley)
        keithley.write(f":SOUR:CURR {-i_mag}")
        control.sleep(settle_s)
        v_minus, stat_minus = _read_v_stat(keithley)
        if cycle < FPP_AUTOSELECT_PROBE_DISCARD_CYCLES:
            continue
        v_deltas.append((v_plus - v_minus) / 2.0)
        if (stat_plus | stat_minus) & KEITHLEY_STAT_BIT_COMPLIANCE:
            in_compliance = True
    keithley.write(f":SOUR:CURR {i_mag}")
    resistance, sigma_v = delta_probe_statistics(v_deltas, i_mag)
    return Probe(i_mag, resistance, sigma_v, in_compliance)


def _moving_filter_armed(measurement_settings) -> bool:
    return (bool(measurement_settings.get('filter_enabled', False))
            and effective_filter_type('four_point', measurement_settings).upper()[:3] == 'MOV')


def _bound_finding(measurement_settings, compliance_v: float) -> Finding:
    """Still in compliance at the smallest current: a lower bound on V/I.

    The same bound a compliance row logs: the compliance voltage over the
    programmed current, which is the most that can have flowed.
    """
    i_min = FPP_AUTOSELECT_MIN_CURRENT
    thickness_um = float(measurement_settings.get('fpp_thickness_um') or 0.0)
    bound = calculate_four_point_probe_bound(
        v_compliance=compliance_v, measured_current=i_min, source_current=i_min,
        spacing_cm=float(measurement_settings.get('fpp_spacing_cm') or 0.1016),
        thickness_um=thickness_um,
        k_factor=float(measurement_settings.get('fpp_k_factor') or 4.532),
        alpha=float(measurement_settings.get('fpp_alpha') or 1.0),
        model=str(measurement_settings.get('fpp_model') or 'thin_film'),
    )
    reason = (f"In compliance ({compliance_v:.3g} V) at {i_min*1e6:.3g} µA: "
              f"V/I > {bound.ratio:.3g} Ω")
    # The K x alpha conductivity is the one the rows carry only on the legacy
    # method; an F84 run reads its bound from the rows.
    if (four_point_method(measurement_settings) == 'legacy' and thickness_um > 0
            and math.isfinite(bound.conductivity) and bound.conductivity > 0):
        reason += f", σ < {bound.conductivity * 100.0:.2g} S/m"
    return Finding(verdict='too_resistive', current=i_min, reason=reason + ".",
                   resistance=bound.ratio, expected_voltage=compliance_v, bound=True)


def assess(keithley, control, measurement_settings, state, model_max_current: float,
           settling_time: float) -> Finding:
    """Probe the sample and conclude. The output is off again on return or raise."""
    seed = abs(state.source_current) or FPP_AUTOSELECT_MIN_CURRENT
    compliance_v = abs(float(measurement_settings.get('fpp_voltage_compliance') or 0.0))
    sig_figs = float(measurement_settings.get('fpp_autoselect_sigfigs') or 4)

    try:
        keithley.write(":OUTP ON")
        control.sleep(settling_time)
        probe = probe_delta(keithley, control, seed, state.delta_settling)
        if probe.in_compliance and seed > FPP_AUTOSELECT_MIN_CURRENT:
            # More resistive than V_compliance / seed. The smallest current
            # either measures it or shows it cannot be sourced at all.
            probe = probe_delta(keithley, control, FPP_AUTOSELECT_MIN_CURRENT,
                                state.delta_settling)
    finally:
        keithley.write(":OUTP OFF")

    if probe.in_compliance:
        return _bound_finding(measurement_settings, compliance_v)

    floor = probe.sigma_v if math.isfinite(probe.sigma_v) else 0.0
    floor = max(floor, FPP_AUTOSELECT_RESOLUTION_FLOOR)
    ceiling = four_point_current_ceiling(
        model_max_current, state.power_stop_w, compliance_v, FPP_AUTOSELECT_MIN_CURRENT)
    selection = select_four_point_current(
        probe.resistance, 10.0 ** sig_figs, ceiling, lambda _v: floor,
        compliance_v=compliance_v or None)
    return Finding(
        verdict=selection.verdict, current=selection.current, reason=selection.reason,
        resistance=abs(probe.resistance), expected_voltage=selection.expected_voltage,
        noise_floor_v=floor, snr=selection.snr, sig_figs=selection.sig_figs)


def _finite(value: Optional[float]) -> Optional[float]:
    return value if value is not None and math.isfinite(value) else None


def describe(finding: Finding, requested_current: float, target_sig_figs: float) -> Dict[str, Any]:
    """The finding as the prompt's detail and the selection event carry it."""
    return {
        'verdict': finding.verdict,
        'message': finding.reason,
        'current_a': math.copysign(finding.current, requested_current or 1.0),
        'requested_current_a': requested_current,
        'resistance_ohm': _finite(finding.resistance),
        'bound': finding.bound,
        'expected_voltage_v': _finite(finding.expected_voltage),
        'noise_floor_v': _finite(finding.noise_floor_v),
        'snr': _finite(finding.snr),
        'sig_figs': _finite(finding.sig_figs),
        'target_sig_figs': target_sig_figs,
    }


def _proceed(control, events, detail: Dict[str, Any], prompt_timeout_s: Optional[float]) -> bool:
    """Ask whether to run at the suggested current. The output is off."""
    if control.stopped():
        return False
    # Not requires_human: it is a judgement about data quality, inside the
    # power and compliance limits that hold whoever answers.
    prompt = control.raise_prompt(PROMPT_KIND, ['proceed', 'abort'],
                                  requires_human=False, detail=detail)
    events.emit('prompt', {
        'prompt_id': prompt.prompt_id, 'kind': prompt.kind, 'options': prompt.options,
        'requires_human': prompt.requires_human, 'detail': prompt.detail,
    })
    choice, _fields = control.wait_for_prompt(prompt_timeout_s)
    events.emit('prompt_resolved', {'prompt_id': prompt.prompt_id, 'choice': choice})
    if choice is None and not control.stopped():
        control.finish('prompt_timeout')
        events.warn('prompt_timeout',
                    "No answer to the auto-select question; abandoning the run.")
        return False
    if choice != 'proceed':
        events.log('autoselect_declined',
                   f"Auto-select: run not started ({detail['verdict']}).")
        return False
    return True


def find_current(keithley, control, events, measurement_settings, state,
                 model_max_current: float, settling_time: float,
                 prompt_timeout_s: Optional[float] = None) -> Optional[float]:
    """Choose the run's source current and program it. None = do not run.

    On return the output is off, the source range and level are the chosen
    ones, and a moving filter that was armed has been turned off for the
    probe and on again after it. That the 2400 starts an empty stack when the
    filter is turned on is assumed, not bench-checked. The caller turns the
    output on, settles, and discards FPP_AUTOSELECT_DISCARD_READINGS readings
    before the first row.
    """
    requested = state.source_current
    target_sig_figs = float(measurement_settings.get('fpp_autoselect_sigfigs') or 4)
    moving_filter = _moving_filter_armed(measurement_settings)
    if moving_filter:
        # A moving average over +I and -I readings is neither.
        keithley.write(":SENS:AVER OFF")
    events.log('autoselect_probing',
               f"Auto-select: probing at {abs(requested)*1e3:.4g} mA...")
    finding = assess(keithley, control, measurement_settings, state,
                     model_max_current, settling_time)
    detail = describe(finding, requested, target_sig_figs)
    events.log('autoselect', f"Auto-select: {finding.reason}")
    if finding.verdict != 'ok' and not _proceed(control, events, detail, prompt_timeout_s):
        return None
    if control.stopped():
        return None
    chosen = detail['current_a']
    compliance_v = abs(float(measurement_settings.get('fpp_voltage_compliance') or 0.0))
    worst_case = four_point_worst_case_power_w(chosen, compliance_v)
    if four_point_power_level(worst_case, state.power_warn_w, state.power_stop_w) == 'stop':
        # The smallest current the finder may choose can still exceed the
        # power stop at a high compliance (1 uA at 200 V against 0.1 mW). The
        # configure step refuses such a request; the finder's own choice went
        # ahead and the run tripped on its first row.
        events.error('power_envelope', 'run',
            f"Selected 4PP power ({format_power(worst_case)} = "
            f"{abs(chosen)*1e6:.3g} µA × {compliance_v:.3g} V) exceeds the "
            f"probe-safety hard stop ({format_power(state.power_stop_w)}). "
            f"Lower the voltage compliance or raise fpp_power_stop_w.")
        control.finish('power_envelope')
        return None
    keithley.write(f":SOUR:CURR:RANG {abs(chosen)}")
    keithley.write(f":SOUR:CURR {chosen}")
    if moving_filter:
        keithley.write(":SENS:AVER ON")
    if four_point_power_level(worst_case, state.power_warn_w, state.power_stop_w) == 'warn':
        events.warn('power_envelope', "Warning: 4PP power envelope: " +
                    four_point_power_warning(worst_case, state.power_warn_w, chosen))
    events.emit('fpp_current_selected', {k: v for k, v in detail.items() if k != 'message'})
    return chosen

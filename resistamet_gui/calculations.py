"""
Four-Point Probe and Electrical Calculations Module

This module provides pure functions for calculating electrical properties
from four-point probe measurements. All formulas are centralized here to
avoid duplication and ensure consistency.

Models supported:
- thin_film: For thin conductive films (Rs = K * V/I, rho = K * t * V/I)
- semi_infinite: For bulk materials (rho = 2*pi*s * V/I)
- finite_thin: Same as thin_film but without alpha correction

Reference:
    F.M. Smits, "Measurement of Sheet Resistivities with the Four-Point Probe",
    Bell System Technical Journal, vol. 37, pp. 711-718, 1958.
"""

import math
from typing import Callable, NamedTuple, Optional, Sequence, Tuple

import numpy as np

from .constants import (
    FPP_AUTOSELECT_COMPLIANCE_HEADROOM, FPP_AUTOSELECT_MIN_CURRENT, FPP_AUTOSELECT_MIN_SNR,
)


class FourPointProbeResult(NamedTuple):
    """Results from a four-point probe measurement calculation.

    Attributes:
        ratio: V/I ratio in Ohms
        sheet_resistance: Sheet resistance Rs in Ohms/square
        resistivity: Resistivity rho in Ohm*cm
        conductivity: Conductivity sigma in S/cm
    """
    ratio: float
    sheet_resistance: float
    resistivity: float
    conductivity: float


# Default correction factor for linear 4-point probe with semi-infinite sample
DEFAULT_K_FACTOR = 4.532


def calculate_ratio(voltage: float, current: float) -> float:
    """Calculate V/I ratio with proper handling of edge cases.

    Args:
        voltage: Measured voltage in Volts
        current: Source current in Amps

    Returns:
        V/I ratio in Ohms, or NaN if inputs are invalid
    """
    if not (np.isfinite(voltage) and np.isfinite(current)):
        return float('nan')
    if current == 0:
        return float('nan')
    return voltage / current


def calculate_sheet_resistance(
    ratio: float,
    k_factor: float = DEFAULT_K_FACTOR,
    alpha: float = 1.0,
    model: str = 'thin_film'
) -> float:
    """Calculate sheet resistance from V/I ratio.

    For a four-point probe measurement:
        Rs = K * alpha * (V/I)  for thin_film model with finite sample correction
        Rs = K * (V/I)          for other models

    Args:
        ratio: V/I ratio in Ohms
        k_factor: Geometric correction factor (default: 4.532 for linear probe)
        alpha: Finite sample size correction factor (applied for thin_film only)
        model: Measurement model ('thin_film', 'semi_infinite', 'finite_thin')

    Returns:
        Sheet resistance in Ohms/square, or NaN if ratio is invalid
    """
    if not np.isfinite(ratio):
        return float('nan')

    # Apply alpha correction only for thin_film model when alpha != 1
    if model == 'thin_film' and alpha and alpha != 1.0:
        k_effective = k_factor * alpha
    else:
        k_effective = k_factor

    return k_effective * ratio


def calculate_resistivity(
    ratio: float,
    spacing_cm: float,
    thickness_cm: float,
    k_factor: float = DEFAULT_K_FACTOR,
    alpha: float = 1.0,
    model: str = 'thin_film'
) -> float:
    """Calculate resistivity from V/I ratio based on model.

    Models:
        - semi_infinite: rho = 2*pi*s * (V/I)
            For bulk materials where thickness >> probe spacing
        - thin_film/finite_thin: rho = K * alpha * t * (V/I)
            For thin films where thickness << probe spacing
        - default: rho = alpha * 2*pi*s * (V/I)
            General case with alpha correction

    Args:
        ratio: V/I ratio in Ohms
        spacing_cm: Probe spacing 's' in cm
        thickness_cm: Film thickness 't' in cm (for thin_film models)
        k_factor: Geometric correction factor
        alpha: Finite sample size correction factor
        model: Measurement model ('thin_film', 'semi_infinite', 'finite_thin', etc.)

    Returns:
        Resistivity in Ohm*cm, or NaN if ratio is invalid, or if a thin-film
        model has no thickness (``thickness_cm`` <= 0 or not finite)

    A thickness of 0 is how the profile and both UIs say "not entered".
    The thin-film formulas multiply by it, and a resistivity of exactly 0
    is a wrong number in a data file that looks like a measurement; NaN is
    what the file already writes for the conductivity of such a row. The
    semi-infinite and alpha*2*pi*s forms do not read the thickness, so
    they still give a resistivity without one.
    """
    if not np.isfinite(ratio):
        return float('nan')

    if model == 'semi_infinite':
        # Bulk material: rho = 2*pi*s * (V/I)
        return 2 * np.pi * spacing_cm * ratio
    elif model in ('thin_film', 'finite_thin'):
        if not (np.isfinite(thickness_cm) and thickness_cm > 0):
            return float('nan')
        # Thin film: rho = K * alpha * t * (V/I)
        k_effective = k_factor
        if model == 'thin_film' and alpha and alpha != 1.0:
            k_effective = k_factor * alpha
        return k_effective * thickness_cm * ratio
    else:
        # Default/unknown model: use alpha correction with 2*pi*s
        return alpha * 2 * np.pi * spacing_cm * ratio


# Keithley 2400-series current measurement ranges
_KEITHLEY_2400_CURRENT_RANGES = (1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 1.0)
# Conservative noise floor as fraction of full-scale on the active range
_KEITHLEY_2400_FLOOR_FRACTION = 1e-3


def estimate_current_floor(source_current: float) -> float:
    """Conservative noise floor of the 2400-series current meter on the active range.

    The 2400's measurement range tracks the source range. Returns roughly 0.1%
    of the full-scale of the smallest range that contains source_current.

    Args:
        source_current: Programmed source current in Amps (sign ignored)

    Returns:
        Estimated current measurement noise floor in Amps
    """
    fallback = _KEITHLEY_2400_CURRENT_RANGES[-1] * _KEITHLEY_2400_FLOOR_FRACTION
    if not np.isfinite(source_current) or source_current == 0:
        return fallback
    abs_i = abs(source_current)
    for r in _KEITHLEY_2400_CURRENT_RANGES:
        if abs_i <= r:
            return r * _KEITHLEY_2400_FLOOR_FRACTION
    return fallback


def calculate_four_point_probe_bound(
    v_compliance: float,
    measured_current: float,
    source_current: float,
    spacing_cm: float,
    thickness_um: float,
    k_factor: float = DEFAULT_K_FACTOR,
    alpha: float = 1.0,
    model: str = 'thin_film',
) -> 'FourPointProbeResult':
    """Compute defensible bounds when the source is in voltage compliance.

    With V pinned at v_compliance, the actual current that flowed is bounded
    above by max(|I_measured|, current_floor). The returned values are LOWER
    bounds for ratio, sheet_resistance, resistivity, and an UPPER bound for
    conductivity. Display them with ≤ / ≥ — never as bare measurements.
    """
    i_floor = estimate_current_floor(source_current)
    if np.isfinite(measured_current):
        i_eff = max(abs(measured_current), i_floor)
    else:
        i_eff = i_floor
    if i_eff == 0:
        nan = float('nan')
        return FourPointProbeResult(nan, nan, nan, nan)
    bounded_ratio = abs(v_compliance) / i_eff
    rs_min = calculate_sheet_resistance(bounded_ratio, k_factor, alpha, model)
    thickness_cm = thickness_um * 1e-4
    rho_min = calculate_resistivity(
        bounded_ratio, spacing_cm, thickness_cm, k_factor, alpha, model
    )
    sigma_max = calculate_conductivity(rho_min)
    return FourPointProbeResult(
        ratio=bounded_ratio,
        sheet_resistance=rs_min,
        resistivity=rho_min,
        conductivity=sigma_max,
    )


class CurrentSelection(NamedTuple):
    """Result of :func:`select_four_point_current`.

    Attributes:
        current: Recommended source-current magnitude, A.
        expected_voltage: |I * R| the sense probes should see at ``current``, V.
        snr: expected_voltage over the noise floor at that voltage.
        sig_figs: significant figures the SNR supports (log10 of it).
        verdict: 'ok' (the target is reached), 'marginal' (the ceilings stop
            short of the target, with at least ``min_snr``), 'too_conductive'
            (under ``min_snr`` at the ceiling) or 'too_resistive' (the minimum
            current already exceeds the voltage headroom). Only 'ok' may run
            without asking.
        reason: one line for the log and the decision prompt.
    """
    current: float
    expected_voltage: float
    snr: float
    sig_figs: float
    verdict: str
    reason: str


def _snr_to_sig_figs(snr: float) -> float:
    """Significant figures an SNR supports: log10(SNR), floored at 0."""
    if not np.isfinite(snr):
        return float('inf')
    if snr <= 1.0:
        return 0.0
    return math.log10(snr)


def delta_probe_statistics(v_deltas: Sequence[float], current: float) -> Tuple[float, float]:
    """R and the noise floor from the reversal pairs of a probe.

    ``v_deltas`` are the per-pair (V+ - V-) / 2 values and ``current`` the
    programmed magnitude both polarities were sourced at. Returns
    ``(resistance, sigma_v)``: the mean over the pairs divided by the
    programmed current, and the sample standard deviation of the pairs. One
    pair has no scatter to estimate, so its own magnitude stands in: nothing
    is then known to be resolved.
    """
    n = len(v_deltas)
    i_mag = abs(current)
    if n == 0 or not i_mag > 0:
        return float('nan'), float('nan')
    mean = sum(v_deltas) / n
    if n > 1:
        sigma = math.sqrt(sum((v - mean) ** 2 for v in v_deltas) / (n - 1))
    else:
        sigma = abs(mean)
    return mean / i_mag, sigma


def four_point_current_ceiling(model_max_current: float, power_stop_w: float,
                               compliance_v: float, min_current: float) -> float:
    """The most current the finder may select, A.

    The smaller of the instrument's limit and the current at which the worst
    case I x V_compliance reaches the power stop -- the same envelope the
    pre-flight check refuses a run on. Never below ``min_current``.
    """
    ceiling = abs(model_max_current)
    if compliance_v and abs(compliance_v) > 0:
        ceiling = min(ceiling, abs(power_stop_w) / abs(compliance_v))
    return max(ceiling, min_current)


def select_four_point_current(
    resistance_ohms: float,
    target_snr: float,
    max_current: float,
    sigma_v: Callable[[float], float],
    min_current: float = FPP_AUTOSELECT_MIN_CURRENT,
    min_snr: float = FPP_AUTOSELECT_MIN_SNR,
    compliance_v: Optional[float] = None,
    compliance_headroom: float = FPP_AUTOSELECT_COMPLIANCE_HEADROOM,
) -> 'CurrentSelection':
    """Choose a 4PP source current for a target number of significant figures.

    The smallest current whose sense voltage reaches ``target_snr`` times the
    noise floor, under the current ceiling and the voltage headroom. The
    smallest, not the largest: it gives the asked-for quality with the least
    Joule heating.

    ``sigma_v`` maps a voltage to its 1-sigma noise floor. For a
    current-reversal measurement pass the measured scatter of the reversal
    pairs, not the datasheet accuracy: the datasheet offset is systematic and
    cancels on reversal.

    A target the ceilings do not allow is 'marginal', never 'ok': the current
    returned is then the ceiling, and whoever runs at it has to be asked.

    Args:
        resistance_ohms: Measured 4-point V/I (sign ignored), Ohm.
        target_snr: 10**significant figures wanted.
        max_current: Ceiling on the source current, A (model limit and
            power stop; see :func:`four_point_current_ceiling`).
        sigma_v: V -> noise floor at that voltage, V.
        min_current: Lower clamp on the chosen current, A.
        min_snr: SNR under which the sample is 'too_conductive'.
        compliance_v: Voltage compliance, V (None = no voltage limit).
        compliance_headroom: Fraction of the compliance the chosen sense
            voltage may reach.
    """
    def _floor(v: float) -> float:
        try:
            return float(sigma_v(v))
        except Exception:
            return float('nan')

    r = abs(resistance_ohms)
    if not np.isfinite(r) or r <= 0.0:
        return CurrentSelection(
            current=max_current, expected_voltage=0.0, snr=0.0, sig_figs=0.0,
            verdict='too_conductive',
            reason="No measurable resistance: the sense voltage is below the noise floor.",
        )

    i_ceiling = max_current
    limit = 'the current ceiling'
    has_comp = compliance_v is not None and np.isfinite(compliance_v) and compliance_v > 0
    if has_comp:
        v_ceiling = compliance_headroom * compliance_v
        if v_ceiling / r < i_ceiling:
            i_ceiling = v_ceiling / r
            limit = f"{compliance_headroom:.0%} of the {compliance_v:.3g} V compliance"
        if min_current * r > v_ceiling:
            v_at_min = min_current * r
            return CurrentSelection(
                current=min_current, expected_voltage=v_at_min, snr=float('inf'),
                sig_figs=float('inf'), verdict='too_resistive',
                reason=(f"R≈{r:.3g} Ω: the minimum {min_current*1e6:.3g} µA needs "
                        f"{v_at_min:.3g} V, above {limit}."),
            )

    # Smallest current whose voltage is target_snr x the floor at that
    # voltage; iterated because the floor may depend on the voltage.
    i_ideal = min_current
    for _ in range(3):
        sv = _floor(i_ideal * r)
        if np.isfinite(sv) and sv > 0:
            i_ideal = (target_snr * sv) / r
        else:
            i_ideal = min_current  # no floor known: the minimum current suffices
            break

    i_chosen = min(max(i_ideal, min_current), i_ceiling)
    # To six figures, more than the source can be programmed to. The
    # arithmetic above ends a few units in the last place off a round value,
    # differently from one platform to the next, and that reached the
    # instrument, the file name and the header as 0.00010000000000000002.
    i_chosen = float(f"{i_chosen:.6g}")
    v_chosen = i_chosen * r
    sv_chosen = _floor(v_chosen)
    snr = v_chosen / sv_chosen if (np.isfinite(sv_chosen) and sv_chosen > 0) else float('inf')
    figs = _snr_to_sig_figs(snr)
    at = f"{i_chosen*1e3:.4g} mA (V≈{v_chosen*1e3:.3g} mV, SNR≈{snr:.3g})"

    if snr < min_snr:
        return CurrentSelection(
            current=i_chosen, expected_voltage=v_chosen, snr=snr, sig_figs=figs,
            verdict='too_conductive',
            reason=(f"R≈{r:.3g} Ω: under 1 significant figure at {at}, "
                    f"limited by {limit}."),
        )
    # A relative tolerance: the current that reaches the target must not be
    # called short of it by its own rounding to six figures.
    if snr < target_snr * (1.0 - 1e-5):
        return CurrentSelection(
            current=i_chosen, expected_voltage=v_chosen, snr=snr, sig_figs=figs,
            verdict='marginal',
            reason=(f"R≈{r:.3g} Ω: {figs:.1f} significant figures at {at}, not the "
                    f"{_snr_to_sig_figs(target_snr):.1f} asked for; limited by {limit}."),
        )
    return CurrentSelection(
        current=i_chosen, expected_voltage=v_chosen, snr=snr, sig_figs=figs,
        verdict='ok',
        reason=f"R≈{r:.3g} Ω: {figs:.1f} significant figures at {at}.",
    )


def calculate_conductivity(resistivity: float) -> float:
    """Calculate conductivity from resistivity.

    Args:
        resistivity: Resistivity in Ohm*cm

    Returns:
        Conductivity in S/cm, or NaN if resistivity is invalid or zero
    """
    with np.errstate(divide='ignore', invalid='ignore'):
        if not np.isfinite(resistivity) or resistivity == 0:
            return float('nan')
        return 1.0 / resistivity


def calculate_four_point_probe(
    voltage: float,
    current: float,
    spacing_cm: float,
    thickness_um: float,
    k_factor: float = DEFAULT_K_FACTOR,
    alpha: float = 1.0,
    model: str = 'thin_film'
) -> FourPointProbeResult:
    """Calculate all four-point probe derived values.

    This is the main entry point for 4PP calculations. It computes:
    - V/I ratio
    - Sheet resistance (Rs)
    - Resistivity (rho)
    - Conductivity (sigma)

    Args:
        voltage: Measured voltage in Volts
        current: Source current in Amps
        spacing_cm: Probe spacing 's' in cm
        thickness_um: Film thickness 't' in micrometers; 0 = not given,
            which leaves a thin-film resistivity and conductivity NaN
        k_factor: Geometric correction factor (default: 4.532)
        alpha: Finite sample size correction factor (default: 1.0)
        model: Measurement model ('thin_film', 'semi_infinite', 'finite_thin')

    Returns:
        FourPointProbeResult with ratio, sheet_resistance, resistivity, conductivity

    Example:
        >>> result = calculate_four_point_probe(
        ...     voltage=0.001,  # 1 mV
        ...     current=0.001,  # 1 mA
        ...     spacing_cm=0.1016,  # Standard probe spacing
        ...     thickness_um=100,   # 100 um film
        ...     model='thin_film'
        ... )
        >>> print(f"Rs = {result.sheet_resistance:.2f} Ohms/sq")
    """
    # Convert thickness from micrometers to centimeters
    thickness_cm = thickness_um * 1e-4

    # Calculate V/I ratio
    ratio = calculate_ratio(voltage, current)

    # Calculate sheet resistance
    sheet_resistance = calculate_sheet_resistance(
        ratio, k_factor, alpha, model
    )

    # Calculate resistivity
    resistivity = calculate_resistivity(
        ratio, spacing_cm, thickness_cm, k_factor, alpha, model
    )

    # Calculate conductivity
    conductivity = calculate_conductivity(resistivity)

    return FourPointProbeResult(
        ratio=ratio,
        sheet_resistance=sheet_resistance,
        resistivity=resistivity,
        conductivity=conductivity
    )


def four_point_method(measurement_settings) -> str:
    """Which calculation a four-point run uses: ``'f84'`` or ``'legacy'``.

    The F84 decomposition is selected by any input only it uses: a finite
    sample dimension, a geometry other than a circle, or a temperature with a
    dopant type. Otherwise the K * alpha path is used. On the F84 path
    ``fpp_k_factor``, ``fpp_alpha`` and ``fpp_model`` play no part.

    One function, because the rows are built by it and the file header says
    which path built them.
    """
    try:
        diameter_cm = float(measurement_settings.get('fpp_diameter_cm') or 0.0)
    except (TypeError, ValueError):
        diameter_cm = 0.0
    geometry = str(measurement_settings.get('fpp_geometry') or 'circle')
    try:
        temperature = float(measurement_settings.get('fpp_temperature_c'))
    except (TypeError, ValueError):
        temperature = float('nan')
    dopant = str(measurement_settings.get('fpp_dopant_type') or 'none').lower()
    if (diameter_cm > 0 or geometry != 'circle'
            or (math.isfinite(temperature) and dopant in ('n', 'p'))):
        return 'f84'
    return 'legacy'


def calculate_four_point_probe_f84(
    voltage: float,
    current: float,
    spacing_cm: float,
    thickness_um: float,
    diameter_cm: Optional[float] = None,
    geometry: str = 'circle',
    f_sp: float = 1.0,
    temperature_c: Optional[float] = None,
    dopant_type: Optional[str] = None,
) -> 'F84ResistivityResult':
    """F84-aligned 4PP calculation from raw V and I.

    Convenience wrapper that converts thickness um → cm and computes V/I,
    then delegates to `calculate_resistivity_f84`. The legacy
    `calculate_four_point_probe` path collapses corrections into K and
    alpha; this one applies the explicit F84 F2 · F(w/S) · F_sp · F_T
    decomposition.

    Returns:
        F84ResistivityResult. Use `result.rho_T` as the measured resistivity
        and `result.rho_23` for the temperature-corrected value if T and
        dopant were supplied.
    """
    thickness_cm = thickness_um * 1e-4
    ratio = calculate_ratio(voltage, current)
    return calculate_resistivity_f84(
        resistance=ratio,
        spacing_cm=spacing_cm,
        thickness_cm=thickness_cm,
        diameter_cm=diameter_cm,
        f_sp=f_sp,
        temperature_c=temperature_c,
        dopant_type=dopant_type,
        geometry=geometry,
    )


# ============================================================================
# ASTM F84-02 correction factors
# ----------------------------------------------------------------------------
# Reference: ASTM F84-02 "Standard Test Method for Measuring Resistivity of
# Silicon Wafers With an In-Line Four-Point Probe".
#
# F84 §13.5–13.6 expresses resistivity as:
#     rho(T) = R_m * F2 * w * F(w/S) * F_sp
# where
#     R_m   = mean of forward/reverse resistance, Ohms
#     F2    = finite-slice diameter correction (Table 3), function of S/D
#     w     = specimen thickness, cm
#     F(w/S)= thickness correction (Table 4 / Appendix X1), function of w/S
#     F_sp  = probe-tip spacing correction (Eq. 5), passed in by caller
#
# F_sp is left to the caller because it requires the microscope-qualification
# protocol of F84 §11.1.2 (measured S1, S2, S3); software cannot compute it
# from a single nominal spacing.
#
# Temperature correction (§13.6–13.8):
#     rho(23) = rho(T) * (1 - C_T * (T - 23))
# with C_T interpolated from Table 5 by resistivity and dopant type.
# ============================================================================

# Table 3 — F2 as a function of S/D (finite slice diameter correction).
# Pairs of (S/D, F2). F2 at S/D=0 is 4.5324 = pi/ln(2), the Smits infinite-slice
# value. The table is monotonic in S/D and we linearly interpolate between
# tabulated points; for S/D > 0.10 we clamp to the last tabulated value.
_F84_TABLE3_F2 = (
    (0.000, 4.532), (0.005, 4.531), (0.010, 4.528), (0.015, 4.524),
    (0.020, 4.517), (0.025, 4.508), (0.030, 4.497), (0.035, 4.485),
    (0.040, 4.470), (0.045, 4.454), (0.050, 4.436), (0.055, 4.417),
    (0.060, 4.395), (0.065, 4.372), (0.070, 4.348), (0.075, 4.322),
    (0.080, 4.294), (0.085, 4.265), (0.090, 4.235), (0.095, 4.204),
    (0.100, 4.171),
)

# Table 5 — C_T (temperature coefficient of resistivity) for Si at 18–28 C.
# Tuples of (rho [Ohm*cm], C_T n-type, C_T p-type). Linear interpolation in
# log-rho is used per the standard's spirit (Table 5 spans ~6 decades).
_F84_TABLE5_CT = (
    (0.0006, 0.00200, 0.00160), (0.0008, 0.00200, 0.00160),
    (0.0010, 0.00200, 0.00158), (0.0012, 0.00184, 0.00151),
    (0.0014, 0.00169, 0.00149), (0.0016, 0.00161, 0.00148),
    (0.0020, 0.00158, 0.00148), (0.0025, 0.00159, 0.00145),
    (0.0030, 0.00156, 0.00137), (0.0035, 0.00146, 0.00127),
    (0.0040, 0.00131, 0.00116), (0.0050, 0.00096, 0.00094),
    (0.0060, 0.00060, 0.00074), (0.0080, 0.00006, 0.00046),
    (0.010, -0.00022, 0.00031), (0.012, -0.00031, 0.00025),
    (0.014, -0.00026, 0.00025), (0.016, -0.00013, 0.00029),
    (0.020, 0.00025, 0.00045), (0.025, 0.00083, 0.00073),
    (0.030, 0.00139, 0.00102), (0.035, 0.00190, 0.00131),
    (0.040, 0.00235, 0.00158), (0.050, 0.00309, 0.00208),
    (0.060, 0.00364, 0.00251), (0.080, 0.00439, 0.00320),
    (0.10, 0.00486, 0.00372), (0.12, 0.00517, 0.00412),
    (0.14, 0.00540, 0.00444), (0.16, 0.00558, 0.00471),
    (0.20, 0.00585, 0.00512), (0.25, 0.00609, 0.00548),
    (0.30, 0.00627, 0.00575), (0.35, 0.00643, 0.00596),
    (0.40, 0.00656, 0.00613), (0.50, 0.00678, 0.00639),
    (0.60, 0.00696, 0.00659), (0.80, 0.00720, 0.00687),
    (1.0, 0.00736, 0.00707), (1.2, 0.00747, 0.00722),
    (1.4, 0.00755, 0.00734), (1.6, 0.00761, 0.00744),
    (2.0, 0.00768, 0.00759), (2.5, 0.00774, 0.00773),
    (3.0, 0.00778, 0.00783), (3.5, 0.00782, 0.00791),
    (4.0, 0.00785, 0.00797), (5.0, 0.00791, 0.00805),
    (6.0, 0.00797, 0.00811), (8.0, 0.00806, 0.00819),
    (10.0, 0.00813, 0.00825), (12.0, 0.00818, 0.00829),
    (14.0, 0.00822, 0.00832), (16.0, 0.00824, 0.00835),
    (20.0, 0.00826, 0.00840), (25.0, 0.00827, 0.00845),
    (30.0, 0.00829, 0.00849), (35.0, 0.00829, 0.00853),
    (40.0, 0.00830, 0.00857), (50.0, 0.00830, 0.00862),
    (60.0, 0.00830, 0.00867), (80.0, 0.00830, 0.00872),
    (100.0, 0.00830, 0.00876), (200.0, 0.00830, 0.00882),
    (500.0, 0.00830, 0.00897), (1000.0, 0.00830, 0.00900),
)


def _linear_interp(x: float, table: tuple, col: int) -> float:
    """Linear interpolation in a sorted (x, y0, y1, ...) table. Clamps at ends."""
    xs = [row[0] for row in table]
    if x <= xs[0]:
        return table[0][col]
    if x >= xs[-1]:
        return table[-1][col]
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            x0, x1 = xs[i], xs[i + 1]
            y0, y1 = table[i][col], table[i + 1][col]
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return table[-1][col]  # unreachable, but mypy-safe


# Geometry correction factors for non-circular samples from Smits 1958,
# Table I (Bell Syst. Tech. J. 37, 711, p. 714), checked against the
# original. Indexed by D/s (sample lateral dimension / probe spacing). For
# rectangles, D is the WIDTH (the dimension perpendicular to the probe array)
# and L/W is the aspect ratio (Smits's a/d; his last column is a/d >= 4).
#
# The Circle column reproduces F84 Table 3 in inverted form: S/D = 0.10 ↔
# D/s = 10. We keep the dense F84 table (_F84_TABLE3_F2) for the circle case
# and use this table only for non-circular geometries.
#
# Rows are sparse near D/s = 1–2: the standard tabulation doesn't define some
# geometries below D/s = 3 (Circle, Square) or D/s = 1.5 (Rect L/W=2). Below
# those points _linear_interp will clamp to the lowest tabulated value.
_SMITS_GEOMETRY_CF: tuple = (
    # (D/s, Square, Rect L/W=2, Rect L/W=3, Rect L/W=4)
    (3.0,    2.4575, 2.7000, 2.7005, 2.7005),
    (4.0,    3.1137, 3.2246, 3.2248, 3.2248),
    (5.0,    3.5098, 3.5749, 3.5750, 3.5750),
    (7.5,    4.0095, 4.0361, 4.0362, 4.0362),
    (10.0,   4.2209, 4.2357, 4.2357, 4.2357),
    (15.0,   4.3882, 4.3947, 4.3947, 4.3947),
    (20.0,   4.4516, 4.4553, 4.4553, 4.4553),
    (40.0,   4.5120, 4.5129, 4.5129, 4.5129),
    (1e9,    4.5324, 4.5324, 4.5324, 4.5324),
)

# Lower-D/s extension for rectangles only (L/W >= 3 is defined down to D/s = 1;
# L/W = 2 only to D/s = 1.5). These rows feed _linear_interp via a separate
# lookup path so the Square column isn't extrapolated below D/s = 3.
_SMITS_RECT_LOW_DS: tuple = (
    # (D/s, Rect L/W=2, Rect L/W=3, Rect L/W=4)
    (1.0,    float('nan'), 0.9988, 0.9994),
    # Smits prints 1.2248 for L/W >= 4 here, below its own L/W = 3 entry;
    # the image series (calculations_geometry) gives 1.2468.
    (1.25,   float('nan'), 1.2467, 1.2468),
    (1.5,    1.4788, 1.4893, 1.4893),
    (1.75,   1.7196, 1.7238, 1.7238),
    (2.0,    1.9454, 1.9475, 1.9475),
    (2.5,    2.3532, 2.3541, 2.3541),
    (3.0,    2.7000, 2.7005, 2.7005),
)

_GEOMETRIES = ('circle', 'square', 'rectangle_2', 'rectangle_3', 'rectangle_4')


def f2_finite_diameter(
    spacing_cm: float,
    diameter_cm: Optional[float] = None,
    geometry: str = 'circle',
) -> float:
    """Geometry correction factor for a finite-size specimen, probe centred.

    For `geometry='circle'` this is F2 from ASTM F84-02 Table 3, a function of
    S/D (probe spacing / wafer diameter), up to the table's last row at
    S/D = 0.10 (4.171). For a smaller disc the table has no value and the
    closed-form image solution it was computed from is used
    (`calculations_geometry.circle_factor`).

    For `geometry in ('square', 'rectangle_2', 'rectangle_3', 'rectangle_4')`
    this is the image series for a square or a rectangle of aspect ratio
    L/W in {2, 3, 4} (`calculations_geometry.rectangle_factor`), with D the
    sample width, perpendicular to the probe array. Smits 1958 Table I prints
    the same factor at a few D/s; its rows agree to 6 parts in 10 000.

    A sample the probe does not fit on (three spacings long) has no factor.

    Args:
        spacing_cm: Probe-tip spacing S, cm.
        diameter_cm: Specimen lateral dimension D, cm. For circles this is the
            diameter; for rectangles it is the width. Pass None or a
            non-positive value to treat the sample as effectively infinite
            (returns 4.5324).
        geometry: One of 'circle', 'square', 'rectangle_2', 'rectangle_3',
            'rectangle_4'. Default 'circle' preserves prior behavior.

    Returns:
        Correction factor (dimensionless). NaN if `geometry` is unrecognized,
        if `spacing_cm` is non-finite/non-positive, or if the probe does not
        fit on the sample.
    """
    if diameter_cm is None or diameter_cm <= 0 or not np.isfinite(diameter_cm):
        return _F84_TABLE3_F2[0][1]
    if not np.isfinite(spacing_cm) or spacing_cm <= 0:
        return float('nan')

    geom = geometry.lower().strip()
    if geom == 'circle':
        s_over_d = spacing_cm / diameter_cm
        if s_over_d <= _F84_TABLE3_F2[-1][0]:
            return _linear_interp(s_over_d, _F84_TABLE3_F2, 1)
        # Past the end of Table 3 (S/D > 0.10) the table has no value. It was
        # clamped at its last entry, 4.171, which is 25 % high for a 5 mm disc
        # on a 1.016 mm probe. The closed form the table was computed from
        # covers every disc the probe fits on.
        return _centred_factor('circle', diameter_cm, spacing_cm)

    if geom not in _GEOMETRIES:
        return float('nan')

    d_over_s = diameter_cm / spacing_cm
    col_idx = {'square': 1, 'rectangle_2': 2,
               'rectangle_3': 3, 'rectangle_4': 4}[geom]

    # The image sum the Smits table was computed from, at every D/s. Straight
    # lines between rows as sparse as the table's were up to 1.6 % low (a
    # square at D/s = 3.5), and everything above D/s = 40 got the D/s = 40
    # value. The printed rows agree with it to 6 parts in 10 000.
    if d_over_s >= 1e6:
        # The unbounded sheet, pi / ln 2: the edges take less than one part
        # in 1e10 off it here, and the series cannot resolve tips this close
        # together against the sample.
        return math.pi / math.log(2.0)
    factor = _centred_factor(geom, diameter_cm, spacing_cm)
    if np.isfinite(factor):
        return factor
    # The probe does not fit. Smits still prints a value where the end tips
    # sit exactly on the edge (L/W = 3 at D/s = 1); that row is returned as
    # printed, and anything else has no factor.
    for table, col in ((_SMITS_GEOMETRY_CF, col_idx),
                       (_SMITS_RECT_LOW_DS, col_idx - 1)):
        if col < 1:
            continue  # the low-D/s rows have no square column
        for row in table:
            if math.isclose(d_over_s, row[0], rel_tol=1e-9) and np.isfinite(row[col]):
                return row[col]
    return float('nan')


def _centred_factor(geom: str, dimension_cm: float, spacing_cm: float) -> float:
    """The geometry factor for a probe at the centre, from the closed forms.

    NaN when the probe does not fit on the sample: there is then no
    four-point measurement to correct.
    """
    from .calculations_geometry import circle_factor, rectangle_factor
    try:
        if geom == 'circle':
            return circle_factor(dimension_cm, spacing_cm)
        aspect = {'square': 1.0, 'rectangle_2': 2.0,
                  'rectangle_3': 3.0, 'rectangle_4': 4.0}[geom]
        # Smits's d is the width, across the probe line; the probe runs along
        # the length a = aspect * d.
        return rectangle_factor(dimension_cm, aspect * dimension_cm, spacing_cm)
    except ValueError:
        return float('nan')


def f_thickness_correction(thickness_cm: float, spacing_cm: float) -> float:
    """F(w/S) thickness correction per ASTM F84-02 Appendix X1, Eq. X1.1.

    Implements the closed-form series:
        F(w/S) = 1.3863 * S / (w * D)
    where D is the series defined in X1.1 — exact terms for n = 1..M and an
    asymptotic expansion for n > M, summed until the increment falls below
    1e-5 (per the standard's convergence criterion).

    Per F84 §X1.3, for w/S < 0.4 the correction is unity to four decimals
    and no computation is necessary.

    Args:
        thickness_cm: Specimen thickness w, cm.
        spacing_cm:   Probe-tip spacing S, cm.

    Returns:
        F(w/S) (dimensionless), ~1 for thin samples, monotonically decreasing
        toward ~0.35 as w/S increases. NaN if inputs are non-finite or
        non-positive.
    """
    if not (np.isfinite(thickness_cm) and np.isfinite(spacing_cm)):
        return float('nan')
    if thickness_cm <= 0 or spacing_cm <= 0:
        return float('nan')

    w_over_s = thickness_cm / spacing_cm
    if w_over_s < 0.4:
        return 1.000  # F84 §X1.3

    # M = int(2 S/w) + 1; first sum uses exact terms, second the asymptotic form.
    s_over_w = 1.0 / w_over_s
    M = int(2 * s_over_w) + 1

    sum1 = 0.0
    for n in range(1, M + 1):
        # A product, not `** 2`: float `**` raises OverflowError where the
        # product gives inf, and an infinite x contributes the correct 0.
        nw_over_s = n * w_over_s
        x = nw_over_s * nw_over_s
        sum1 += (0.25 + x) ** -0.5 - (1.0 + x) ** -0.5

    sum2 = 0.0
    n = M + 1
    while True:
        u = s_over_w / n
        u3 = u ** 3
        term = 0.75 * u3 - (45.0 / 64.0) * (u ** 5) + (315.0 / 512.0) * (u ** 7)
        sum2 += term
        if abs(term) < 1e-5:
            break
        n += 1
        if n > 100_000:  # defensive guard; never triggers for w/S in [0.4, 5]
            break

    big_d = 1.0 + 2.0 * sum1 + sum2
    return 1.3863 * s_over_w / big_d


def f_temperature_correction(
    rho_at_temperature: float,
    temperature_c: float,
    dopant_type: str = 'p',
) -> float:
    """F_T such that rho(23) = rho(T) * F_T, per F84 §13.6–13.8 and Table 5.

    F_T = 1 - C_T * (T - 23), where C_T is the silicon temperature coefficient
    of resistivity (interpolated from F84 Table 5 by rho and dopant type).

    Args:
        rho_at_temperature: Measured resistivity at temperature T, Ohm*cm.
            Used solely to look up C_T from Table 5.
        temperature_c: Measurement temperature T, degrees Celsius. Per F84
            §6.1.6, the correction is valid for 18 <= T <= 28.
        dopant_type: 'n' or 'p'. Only silicon is tabulated by the standard.

    Returns:
        F_T (dimensionless multiplier near unity). NaN if inputs are invalid.
    """
    if not (np.isfinite(rho_at_temperature) and np.isfinite(temperature_c)):
        return float('nan')
    if rho_at_temperature <= 0:
        return float('nan')

    dtype = dopant_type.lower().strip()
    if dtype in ('n', 'n-type', 'ntype'):
        col = 1
    elif dtype in ('p', 'p-type', 'ptype'):
        col = 2
    else:
        return float('nan')

    c_t = _linear_interp(rho_at_temperature, _F84_TABLE5_CT, col)
    return 1.0 - c_t * (temperature_c - 23.0)


class F84ResistivityResult(NamedTuple):
    """F84-aligned resistivity calculation result.

    Attributes:
        rho_T: Resistivity at measurement temperature T, Ohm*cm.
        rho_23: Resistivity corrected to 23 C, Ohm*cm. None if no temperature
            / dopant information was supplied.
        f2: F2 from Table 3 (finite-diameter correction).
        f_w_s: F(w/S) from Appendix X1 (thickness correction).
        f_T: Temperature correction F_T = 1 - C_T(T-23), or None.
        geometric_factor: F = F2 * w * F(w/S) * F_sp, in cm. ρ(T) = R * F.
    """
    rho_T: float
    rho_23: Optional[float]
    f2: float
    f_w_s: float
    f_T: Optional[float]
    geometric_factor: float


def calculate_resistivity_f84(
    resistance: float,
    spacing_cm: float,
    thickness_cm: float,
    diameter_cm: Optional[float] = None,
    f_sp: float = 1.0,
    temperature_c: Optional[float] = None,
    dopant_type: Optional[str] = None,
    geometry: str = 'circle',
) -> F84ResistivityResult:
    """Compute resistivity per ASTM F84-02 §13.5–13.8.

        rho(T)  = R * F2 * w * F(w/S) * F_sp
        rho(23) = rho(T) * (1 - C_T * (T - 23))    [if T, dopant supplied]

    Unlike `calculate_resistivity`, this function applies the full F84
    correction-factor decomposition rather than collapsing it into a single
    K factor. Use this for any F84-aligned reporting. The legacy function
    remains for the simple Smits thin-film and semi-infinite shortcuts.

    F_sp must be supplied by the caller (defaults to 1.0). F84 §11.1.2
    specifies its measurement via toolmaker's microscope and is outside the
    scope of this software.

    Args:
        resistance: V/I ratio R_m, Ohms. Use the forward/reverse mean per
            F84 §13.2 when delta mode is available.
        spacing_cm: Probe-tip spacing S, cm.
        thickness_cm: Specimen thickness w, cm.
        diameter_cm: Specimen diameter (or rectangle width) D, cm. None or
            non-positive treats the specimen as infinite (F2 = 4.5324).
        f_sp: Probe-tip spacing correction factor (default 1.0).
        temperature_c: Measurement temperature, deg C (optional).
        dopant_type: 'n' or 'p' for silicon (optional). Required together
            with temperature_c to populate rho_23.
        geometry: Sample shape, one of 'circle' (default), 'square',
            'rectangle_2', 'rectangle_3', 'rectangle_4'.

    Returns:
        F84ResistivityResult. rho_23 is None unless both temperature_c and
        dopant_type are supplied.
    """
    if not (np.isfinite(resistance) and np.isfinite(spacing_cm)
            and np.isfinite(thickness_cm)):
        nan = float('nan')
        return F84ResistivityResult(nan, None, nan, nan, None, nan)
    if spacing_cm <= 0 or thickness_cm <= 0:
        nan = float('nan')
        return F84ResistivityResult(nan, None, nan, nan, None, nan)

    f2 = f2_finite_diameter(spacing_cm, diameter_cm, geometry=geometry)
    f_w_s = f_thickness_correction(thickness_cm, spacing_cm)
    geometric_factor = f2 * thickness_cm * f_w_s * f_sp
    rho_T = resistance * geometric_factor

    rho_23: Optional[float] = None
    f_T: Optional[float] = None
    if temperature_c is not None and dopant_type is not None:
        f_T = f_temperature_correction(rho_T, temperature_c, dopant_type)
        if np.isfinite(f_T):
            rho_23 = rho_T * f_T

    return F84ResistivityResult(
        rho_T=rho_T,
        rho_23=rho_23,
        f2=f2,
        f_w_s=f_w_s,
        f_T=f_T,
        geometric_factor=geometric_factor,
    )


def format_resistivity_formula(
    spacing_cm: float,
    model: str,
    k_factor: float = DEFAULT_K_FACTOR,
    alpha: float = 1.0,
    thickness_um: Optional[float] = None
) -> str:
    """Generate a human-readable formula string for resistivity calculation.

    Used for displaying the formula in the UI with actual parameter values.

    Args:
        spacing_cm: Probe spacing in cm
        model: Measurement model
        k_factor: Geometric correction factor
        alpha: Finite sample size correction
        thickness_um: Film thickness in micrometers (for thin film models)

    Returns:
        Formatted string showing the formula with values
    """
    if model == 'semi_infinite':
        coeff = 2 * np.pi * spacing_cm
        return f"rho = 2*pi*s*(V/I) = {coeff:.4g}*(V/I) Ohm*cm"
    elif model in ('thin_film', 'finite_thin'):
        if thickness_um is not None:
            thickness_cm = thickness_um * 1e-4
            k_eff = k_factor * (alpha if (model == 'thin_film' and alpha != 1.0) else 1.0)
            coeff = k_eff * thickness_cm
            return f"rho = K*t*(V/I) = {coeff:.4g}*(V/I) Ohm*cm"
        else:
            return f"rho = K*t*(V/I) Ohm*cm (thickness not specified)"
    else:
        coeff = alpha * 2 * np.pi * spacing_cm
        return f"rho = alpha*2*pi*s*(V/I) = {coeff:.4g}*(V/I) Ohm*cm"


# ---------------------------------------------------------------------------
# Combined uncertainty for 4PP per-spot statistics
# ---------------------------------------------------------------------------


class FourPointCombinedUncertainty(NamedTuple):
    """Combined (statistical + instrument) uncertainty for a 4PP statistic.

    All quantities share the same units as the input statistic — typically
    Ω/sq, Ω·cm, or S/cm. The instrument floor is a relative number
    (mean per-reading σ_R / R), so the same factor scales to Rs, ρ, σ.
    """
    mean: float
    rsd_pct: float          # dispersion of the data (std / mean × 100)
    u_stat: float           # std(values) / √N — random component of σ_mean
    u_inst: float           # |mean| × mean(σ_R/R) — systematic instrument floor
    u_total: float          # √(u_stat² + u_inst²)


def four_point_combined_uncertainty(
    values,
    v_readings,
    i_readings,
    model: str = "2400",
    nplc: float = 1.0,
    v_fixed_range: Optional[float] = None,
) -> Optional[FourPointCombinedUncertainty]:
    """Combine statistical and instrument uncertainty for a 4PP statistic.

    ``values`` is the list of per-reading derived quantities (Rs, ρ, or σ).
    ``v_readings`` / ``i_readings`` are the parallel raw V and I per row,
    used to compute the relative instrument floor σ_R/R: the measurement
    accuracy of V and the source accuracy of I, the same two the rows'
    ``V_unc_V`` and ``I_unc_A`` columns carry. Lists must have matching
    length. ``v_fixed_range`` is the voltage range value the run was pinned
    to when auto range was off (``accuracy._measure_range``).

    The instrument contribution is treated as a *systematic* per-reading
    floor (mean of σ_R/R across readings) that does NOT reduce with N —
    matching how the Keithley datasheet specifies accuracy. The
    statistical contribution is the standard uncertainty of the mean
    (std/√N), which DOES reduce with N. Combined in quadrature per
    GUM §5.1.2 Eq. 10 (uncorrelated sources).

    Returns ``None`` if no finite values are provided. NaN-safe on inputs.
    """
    from .accuracy import current_source_uncertainty, voltage_uncertainty
    if not values:
        return None
    finite_values = [v for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    if not finite_values:
        return None
    n = len(finite_values)
    mean_val = sum(finite_values) / n
    if n > 1:
        # Sample std with Bessel correction.
        var = sum((x - mean_val) ** 2 for x in finite_values) / (n - 1)
        std = math.sqrt(var)
    else:
        std = 0.0
    rsd = (std / mean_val * 100.0) if mean_val != 0 else 0.0
    u_stat = std / math.sqrt(n) if n > 1 else 0.0

    # Per-row relative instrument floor σ_R / R. Skip rows with bad V/I/R.
    rel_floors = []
    for v, i in zip(v_readings, i_readings):
        try:
            v = float(v); i = float(i)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(v) and math.isfinite(i)) or i == 0:
            continue
        r = v / i
        if r == 0 or not math.isfinite(r):
            continue
        # V is measured and I is sourced, so the measure specification for V
        # and the source specification for I, in quadrature (GUM 5.1.6 Eq. 12
        # in the form accuracy.resistance_uncertainty uses).
        sigma_v = voltage_uncertainty(v, model, nplc, fixed_range=v_fixed_range)
        sigma_i = current_source_uncertainty(i, model)
        sigma_r = math.hypot(sigma_v, abs(r) * sigma_i) / abs(i)
        if math.isfinite(sigma_r) and sigma_r > 0:
            rel_floors.append(sigma_r / abs(r))
    mean_rel_inst = sum(rel_floors) / len(rel_floors) if rel_floors else 0.0
    u_inst = abs(mean_val) * mean_rel_inst
    u_total = math.sqrt(u_stat ** 2 + u_inst ** 2)

    return FourPointCombinedUncertainty(
        mean=mean_val,
        rsd_pct=rsd,
        u_stat=u_stat,
        u_inst=u_inst,
        u_total=u_total,
    )


# ---------------------------------------------------------------------------
# Four-point probe power envelope
# ---------------------------------------------------------------------------


def four_point_worst_case_power_w(source_current: float, voltage_compliance: float) -> float:
    """|I| x |V_compliance|: the most the probe can dissipate, in W.

    The run's pre-flight (``session/configure.py``) and the settings preview
    (``schema/resolve.py``) both judge this number, so it is worked out here
    once and the preview cannot say something the run will not.
    """
    return abs(float(source_current)) * abs(float(voltage_compliance))


def four_point_power_level(worst_case_w: float, warn_w: float, stop_w: float) -> Optional[str]:
    """``'stop'`` above the hard stop, ``'warn'`` above the warning threshold, else None.

    ``'stop'``: the run refuses to turn the output on. ``'warn'``: it warns
    and goes on.
    """
    if worst_case_w > float(stop_w):
        return 'stop'
    if worst_case_w > float(warn_w):
        return 'warn'
    return None

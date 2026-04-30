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
from typing import NamedTuple, Optional

import numpy as np


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
        Resistivity in Ohm*cm, or NaN if ratio is invalid
    """
    if not np.isfinite(ratio):
        return float('nan')

    if model == 'semi_infinite':
        # Bulk material: rho = 2*pi*s * (V/I)
        return 2 * np.pi * spacing_cm * ratio
    elif model in ('thin_film', 'finite_thin'):
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
        thickness_um: Film thickness 't' in micrometers
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

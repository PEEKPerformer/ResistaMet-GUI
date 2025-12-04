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

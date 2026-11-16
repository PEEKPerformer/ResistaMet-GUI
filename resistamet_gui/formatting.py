"""Numbers as text, for messages an operator reads.

Pure and Qt-free, so the schema's validation messages and the run's log can
share it: a threshold quoted before the run and the same threshold quoted
during it must read the same.
"""
import math

#: Largest first. Probe-safety thresholds sit between microwatts and watts.
_POWER_UNITS = ((1.0, 'W'), (1e-3, 'mW'), (1e-6, 'µW'), (1e-9, 'nW'))


def format_power(watts: float) -> str:
    """A power with the SI prefix that suits it, to three figures.

    ``1e-4`` -> ``"100 µW"``, ``2e-3`` -> ``"2 mW"``, ``1.5`` -> ``"1.5 W"``.

    The power messages used to print everything in whole or tenth milliwatts,
    so a 0.1 mW hard stop read "0 mW" and the message contradicted itself
    ("0.5 mW exceeds the hard stop 0 mW").
    """
    if not math.isfinite(watts):
        return f"{watts} W"
    # Round first and pick the unit from the rounded value, so 0.9996 mW
    # reads "1 mW" and not "1e+03 µW".
    rounded = float(f"{watts:.3g}")
    if rounded == 0:
        return "0 W"
    scale, unit = _POWER_UNITS[-1]
    for candidate_scale, candidate_unit in _POWER_UNITS:
        if abs(rounded) >= candidate_scale:
            scale, unit = candidate_scale, candidate_unit
            break
    scaled = rounded / scale
    if abs(scaled) >= 1000:
        # Kilowatts are beyond any SourceMeter; plain digits, no exponent.
        return f"{scaled:.0f} {unit}"
    return f"{scaled:.3g} {unit}"


def four_point_power_warning(worst_case_w: float, warn_w: float, source_current: float) -> str:
    """The four-point power warning, with what lowers the power.

    Said the same before the run (``schema/resolve.py``) and in its log
    (``session/configure.py``). The power is |I| x V_compliance, so the
    remedy is a lower compliance or a lower current; the warning used to
    name neither, and an agent told only "above the warning threshold"
    had to work out which setting it came from. The compliance comes
    first: it is the one that can usually drop without changing the
    measurement, since the probe needs only the voltage the sample gives.
    """
    current = abs(float(source_current))
    if current > 0:
        # Warned above the threshold, not at it.
        lower = (f"lower fpp_voltage_compliance (to {warn_w / current:.3g} V or less at "
                 f"this current) or fpp_current")
    else:
        lower = "lower fpp_voltage_compliance or fpp_current"
    return (f"Worst-case power {format_power(worst_case_w)} (source current × voltage "
            f"compliance) is above the {format_power(warn_w)} warning threshold; {lower} "
            f"to bring it under.")

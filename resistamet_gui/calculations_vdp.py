"""
Van der Pauw resistivity calculations per ASTM F76-08 Method A.

Implements F76 Method A for measuring resistivity (and sheet resistance) of
an arbitrarily shaped, hole-free, homogeneous, isotropic specimen with four
contacts on its periphery, numbered counter-clockwise.

Per F76 sec. 11.1, resistivity is computed from 8 voltage readings
(4 source-sense geometries x 2 current polarities) using:

    rho_A = (pi / (4 ln 2)) * f_A * t / I
            * [V_21,34 - V_12,34 + V_32,41 - V_23,41]
    rho_B = (pi / (4 ln 2)) * f_B * t / I
            * [V_43,12 - V_34,12 + V_14,23 - V_41,23]

where t is thickness (cm), I is the current magnitude (A), and f is the F76
Fig. 5 geometric factor accounting for voltage-pair asymmetry Q:

    Q_A = (V_21,34 - V_12,34) / (V_32,41 - V_23,41)
    Q_B = (V_43,12 - V_34,12) / (V_14,23 - V_41,23)

If Q < 1, take its reciprocal. f(Q) solves the implicit relation in F76
Fig. 5:

    (Q - 1) / (Q + 1) = (f / ln 2) * arccosh{(1/2) * exp(ln 2 / f)}

Sample averaged rho_av = (rho_A + rho_B) / 2. Per F76 sec. 11.1 the
homogeneity criterion is |rho_A - rho_B| / rho_av <= 10 percent.

The thickness only scales: R_s = rho / t is the same expression without
it, and the homogeneity ratio does not depend on it. So the sheet
resistance and the homogeneity check are computed with no thickness, and
a thickness of 0 (not given) leaves only the resistivities undefined
(NaN), as a four-point run with no thickness does.

Notation: V_AB,CD denotes V_C - V_D when current I enters contact A and
exits contact B. Contacts numbered 1-4 counter-clockwise around the
periphery.

Reference:
    ASTM F76-08 (Reapproved 2016) e1, Standard Test Methods for Measuring
    Resistivity and Hall Coefficient and Determining Hall Mobility in
    Single-Crystal Semiconductors. Method A.
"""
from __future__ import annotations

import math
from typing import List, Mapping, NamedTuple, Optional, Tuple

from .constants import F76_HOMOGENEITY_TOLERANCE_PCT, KEITHLEY_COMPLIANCE_MAGIC_NUMBER

# pi / (4 ln 2) ~= 1.1331 -- the constant in F76 eqs. (1) and (2).
F76_CONSTANT = math.pi / (4.0 * math.log(2.0))

_LN2 = math.log(2.0)

# What a 2400 returns for a reading it could not make (overflow): 9.9e37,
# constants.KEITHLEY_COMPLIANCE_MAGIC_NUMBER. No voltage is this large.
_OVERFLOW_READING = KEITHLEY_COMPLIANCE_MAGIC_NUMBER

_GENERIC_READING_LABELS = (
    "first geometry +I", "first geometry -I",
    "second geometry +I", "second geometry -I",
)


class VdpConfiguration(NamedTuple):
    """One measurement configuration in the F76 protocol.

    label: F76 voltage label, e.g. "V_21,34".
    source_high / source_low: contact numbers (1-4) for source current
        entry / exit. F76's leading subscript pair AB means I enters A,
        exits B.
    sense_high / sense_low: contact numbers (1-4) for the voltmeter +/-
        terminals. F76's trailing subscript pair CD means V_C - V_D.
    group: "A" or "B" for the F76 base protocol; "A_recip" / "B_recip"
        for the extended-reciprocity protocol's dual geometries.
    """
    label: str
    source_high: int
    source_low: int
    sense_high: int
    sense_low: int
    group: str


class VdpGeometry(NamedTuple):
    """One physical cabling configuration for a vdP measurement.

    The user wires the 4 leads once per geometry; the worker handles
    current reversal automatically. Each geometry yields two F76 voltage
    labels: label_pos at +I, label_neg at -I.

    name: user-facing label, e.g. "Geometry 1 of 4".
    source_high / source_low: contacts the Force HI / Force LO leads
        attach to for the +I polarity (at -I they swap roles in F76
        notation, but the cabling does not move).
    sense_high / sense_low: contacts the Sense HI / Sense LO leads
        attach to.
    label_pos / label_neg: F76 voltage labels produced by this geometry.
    group: "A" (used by rho_A, F76 eq. 1) or "B" (used by rho_B, eq. 2).
    """
    name: str
    source_high: int
    source_low: int
    sense_high: int
    sense_low: int
    label_pos: str
    label_neg: str
    group: str


class VdpResult(NamedTuple):
    """Resistivity result from a vdP measurement per F76 Method A.

    rho_a, rho_b: redundant resistivity values (Ohm cm); NaN when no
        thickness was given.
    rho_avg: averaged resistivity (Ohm cm); NaN when no thickness was given.
    sheet_resistance: (R_s,A + R_s,B) / 2 = rho_avg / thickness (Ohm/square).
    q_a, q_b: voltage asymmetry ratios (normalized to >= 1).
    f_a, f_b: F76 Fig. 5 geometric factors derived from Q.
    homogeneous: True iff |rho_a - rho_b| / rho_avg <= 10 percent. Never
        True when ``valid`` is False.
    asymmetry_pct: 100 * |rho_a - rho_b| / rho_avg.
    valid: False when the readings cannot be one sample's F76 set: the
        V(+I) - V(-I) differences of the four geometries do not all have
        the positive sign the F76 wiring gives. Then sheet_resistance and
        the resistivities are NaN and asymmetry_pct is inf; Q and f are
        still those of the readings, for diagnosis.
    invalid_reason: why, naming the F76 labels; "" when valid.
    """
    rho_a: float
    rho_b: float
    rho_avg: float
    sheet_resistance: float
    q_a: float
    q_b: float
    f_a: float
    f_b: float
    homogeneous: bool
    asymmetry_pct: float
    valid: bool = True
    invalid_reason: str = ""


def f76_geometries() -> List[VdpGeometry]:
    """Return the 4 physical cabling configurations of F76 Method A.

    Each VdpGeometry corresponds to one set of lead positions that the
    user wires manually. The measurement worker automates current
    reversal at each geometry, so 4 manual reconnections yield F76's
    full 8 voltage readings.
    """
    return [
        VdpGeometry("Geometry 1 of 4", 2, 1, 3, 4, "V_21,34", "V_12,34", "A"),
        VdpGeometry("Geometry 2 of 4", 3, 2, 4, 1, "V_32,41", "V_23,41", "A"),
        VdpGeometry("Geometry 3 of 4", 4, 3, 1, 2, "V_43,12", "V_34,12", "B"),
        VdpGeometry("Geometry 4 of 4", 1, 4, 2, 3, "V_14,23", "V_41,23", "B"),
    ]


def f76_configurations() -> List[VdpConfiguration]:
    """Return the F76 protocol as 8 ordered measurement configurations.

    F76 sec. 10.4 lists 8 voltages = 4 source-sense geometries x 2 current
    polarities. rho_A (F76 eq. 1) is computed from the first 4 (group "A");
    rho_B (F76 eq. 2) from the last 4 (group "B").

    Note: at the level of unsigned adjacent-contact pairs, the base 8
    already covers every (source pair, sense pair) Onsager-reciprocity
    pairing with both current polarities. A future "cross-pair" protocol
    using diagonal pairs (e.g. source {1,3}, sense {2,4}) would add
    genuinely new physics; that is out of scope for v1.
    """
    return [
        # F76 sec. 11.1: rho_A is computed from these four readings.
        VdpConfiguration("V_21,34", 2, 1, 3, 4, "A"),
        VdpConfiguration("V_12,34", 1, 2, 3, 4, "A"),
        VdpConfiguration("V_32,41", 3, 2, 4, 1, "A"),
        VdpConfiguration("V_23,41", 2, 3, 4, 1, "A"),
        # rho_B from these four.
        VdpConfiguration("V_43,12", 4, 3, 1, 2, "B"),
        VdpConfiguration("V_34,12", 3, 4, 1, 2, "B"),
        VdpConfiguration("V_14,23", 1, 4, 2, 3, "B"),
        VdpConfiguration("V_41,23", 4, 1, 2, 3, "B"),
    ]


def vdp_geometric_factor(q: float) -> float:
    """F76 Fig. 5 geometric factor f as a function of asymmetry ratio Q.

    Solves
        (Q - 1) / (Q + 1) = (f / ln 2) * arccosh{(1/2) * exp(ln 2 / f)}
    for f on (0, 1]. f(1) = 1; f -> 0 as Q -> infinity.

    The RHS is monotonically decreasing in f over (0, 1], so bisection
    converges unconditionally. Hand-rolled rather than scipy.optimize to
    avoid a heavy dependency for a 25-line root-finder on a smooth
    monotone function.

    Args:
        q: asymmetry ratio (>= 1). Pass 1/Q if your measured value is < 1.

    Returns:
        f in (0, 1].
    """
    if not math.isfinite(q):
        raise ValueError("Q must be finite; got %r" % (q,))
    if q < 1.0:
        raise ValueError(
            "Q must be >= 1 (take reciprocal if measured Q < 1); got %r" % (q,)
        )
    if q == 1.0:
        return 1.0

    target = (q - 1.0) / (q + 1.0)

    def rhs(f: float) -> float:
        # math.exp overflows for f below about ln 2 / 700 ~= 1e-3. In that
        # limit the formula collapses to (f / ln 2) * (ln 2 / f) = 1, so
        # rhs -> 1 from below. Returning 1.0 here keeps the bisection
        # search well-defined past the floor.
        if f < _LN2 / 700.0:
            return 1.0
        argument = 0.5 * math.exp(_LN2 / f)
        # acosh requires argument >= 1. At f = 1, argument == 1 exactly.
        # Floating-point near f = 1 can yield argument slightly below 1.
        if argument < 1.0:
            argument = 1.0
        return (f / _LN2) * math.acosh(argument)

    # F76 Fig. 5 spans f in [~0.4, 1.0] (Q in [1, 100]). The floor 1e-3
    # extends usable range to Q ~= 10^4, far past any practical sample.
    lo, hi = 1e-3, 1.0
    if target >= rhs(lo):
        return lo

    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if hi - lo < 1e-12:
            return mid
        if rhs(mid) > target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def vdp_resistivity_pair(
    v_pos: float,
    v_neg: float,
    v_perp_pos: float,
    v_perp_neg: float,
    current: float,
    thickness_cm: float,
    labels: Tuple[str, str, str, str] = _GENERIC_READING_LABELS,
) -> Tuple[float, float, float]:
    """Compute one of (rho_A, rho_B) per F76 eq. (1) or (2).

    ``vdp_sheet_resistance_pair`` times the thickness; see it for the
    readings and the checks on them.

    Each group consists of two source-sense geometries, each measured at
    +I and -I polarity. F76 eq. (1):

        rho_A = (1.1331 * f_A * t / I)
                * [V_21,34 - V_12,34 + V_32,41 - V_23,41]

    with
        Q_A = (V_21,34 - V_12,34) / (V_32,41 - V_23,41)

    Args:
        v_pos: first geometry, +I polarity (e.g. V_21,34 for group A).
        v_neg: first geometry, -I polarity (e.g. V_12,34 for group A).
        v_perp_pos: second geometry, +I polarity (e.g. V_32,41).
        v_perp_neg: second geometry, -I polarity (e.g. V_23,41).
        current: source current magnitude (A, positive).
        thickness_cm: sample thickness (cm, positive).
        labels: names of the four readings, in argument order, for the
            error messages.

    Returns:
        (rho, Q, f). Q is normalized to >= 1.

    Raises:
        ValueError: if current or thickness is non-positive, if a reading
            is not finite or is the instrument's overflow value, or if
            either geometry reads the same at both polarities (Q is then
            undefined). The message names the readings.
    """
    if thickness_cm <= 0:
        raise ValueError("thickness must be > 0 cm; got %r" % (thickness_cm,))
    rs, q, f = vdp_sheet_resistance_pair(v_pos, v_neg, v_perp_pos, v_perp_neg,
                                         current, labels=labels)
    return rs * thickness_cm, q, f


def vdp_sheet_resistance_pair(
    v_pos: float,
    v_neg: float,
    v_perp_pos: float,
    v_perp_neg: float,
    current: float,
    labels: Tuple[str, str, str, str] = _GENERIC_READING_LABELS,
) -> Tuple[float, float, float]:
    """One of (R_s,A, R_s,B): F76 eq. (1) or (2) divided by the thickness.

        R_s,A = (1.1331 * f_A / I) * [V_21,34 - V_12,34 + V_32,41 - V_23,41]

    in Ohm/square, which needs no thickness. Arguments as
    ``vdp_resistivity_pair`` without ``thickness_cm``.

    Returns:
        (R_s, Q, f). Q is normalized to >= 1.

    Raises:
        ValueError: if current is non-positive, if a reading is not finite
            or is the instrument's overflow value, or if either geometry
            reads the same at both polarities (Q is then undefined). The
            message names the readings.

    The sum is signed and Q is a magnitude, so two differences of opposite
    sign partly cancel here without any error: R_s comes out too small and
    Q and f look ordinary. ``vdp_sign_problem`` is the check for that;
    ``calculate_van_der_pauw`` applies it and reports no R_s when it fails.
    """
    if current <= 0:
        raise ValueError("current must be > 0 A; got %r" % (current,))

    for label, value in zip(labels, (v_pos, v_neg, v_perp_pos, v_perp_neg)):
        if not math.isfinite(value) or abs(value) >= _OVERFLOW_READING:
            raise ValueError(
                "%s is not a usable voltage (%r): the reading overflowed or "
                "is not a number. Check the connections and the voltage "
                "range for that geometry and measure it again."
                % (label, value)
            )

    # Both deltas are positive on a sample wired as F76 numbers it (current
    # reversal flips V; the subtraction doubles the signal and cancels
    # thermal offset). Q is taken as a magnitude; the signs are checked by
    # vdp_sign_problem, not here.
    delta_first = v_pos - v_neg
    delta_second = v_perp_pos - v_perp_neg

    for delta, pos_label, neg_label, value in (
        (delta_first, labels[0], labels[1], v_pos),
        (delta_second, labels[2], labels[3], v_perp_pos),
    ):
        if delta == 0.0:
            raise ValueError(
                "Cannot compute Q: %s and %s read the same (%r V) at both "
                "current polarities, so their difference is zero. An open "
                "or shorted lead reads like this; check the connections "
                "for that geometry and measure it again."
                % (pos_label, neg_label, value)
            )

    q = abs(delta_first / delta_second)
    if q < 1.0:
        q = 1.0 / q

    f = vdp_geometric_factor(q)

    rs = F76_CONSTANT * f / current * (delta_first + delta_second)
    return rs, q, f


_BASE_LABELS_A = ("V_21,34", "V_12,34", "V_32,41", "V_23,41")
_BASE_LABELS_B = ("V_43,12", "V_34,12", "V_14,23", "V_41,23")
_REQUIRED_BASE_LABELS = _BASE_LABELS_A + _BASE_LABELS_B

# (+I label, -I label) of each physical geometry, in protocol order.
_GEOMETRY_PAIRS = (
    ("V_21,34", "V_12,34"),
    ("V_32,41", "V_23,41"),
    ("V_43,12", "V_34,12"),
    ("V_14,23", "V_41,23"),
)


def vdp_sign_problem(voltages: Mapping[str, float]) -> str:
    """Why the signs of the eight readings rule out a result, or "".

    With the contacts numbered counter-clockwise and the leads where each
    F76 label puts them, V(+I) - V(-I) is positive at all four geometries.
    F76 eqs. (1) and (2) add those differences with their signs, so one
    that is negative does not raise anything: it subtracts. Sense leads
    swapped at geometries 2 and 4 of a square give R_s,A = R_s,B = 0 or,
    on a sample with Q != 1, a plausible number that is far too small,
    with Q, f and the 10 percent gate all passing. A negative difference
    therefore invalidates the result whichever geometries it is at:

    * some negative, some positive: the leads were reversed at the
      negative ones (or the contacts are not in order round the edge);
    * all four negative: reversed throughout. The magnitude would be
      right, but a negative sheet resistance is not reported.

    Returns the reason, naming the labels; "" when all four are positive.
    A difference of exactly zero is not this function's business
    (``vdp_sheet_resistance_pair`` raises for it).
    """
    negative = ["%s - %s" % (pos, neg) for pos, neg in _GEOMETRY_PAIRS
                if voltages[pos] - voltages[neg] < 0]
    if not negative:
        return ""
    if len(negative) == len(_GEOMETRY_PAIRS):
        return ("V(+I) - V(-I) is negative at all four geometries: the sense "
                "leads or the current leads are reversed throughout, and a "
                "negative sheet resistance is not a result.")
    return ("sign inconsistency: %s %s negative while the other geometries are "
            "positive. The leads are reversed at %s (or the contacts are not "
            "numbered in order round the edge); the F76 sums would partly "
            "cancel, so no sheet resistance is reported."
            % (", ".join(negative), "is" if len(negative) == 1 else "are",
               "that geometry" if len(negative) == 1 else "those geometries"))


def calculate_van_der_pauw(
    voltages: Mapping[str, float],
    current: float,
    thickness_cm: Optional[float],
) -> VdpResult:
    """Compute resistivity, sheet resistance, and homogeneity per F76 Method A.

    Args:
        voltages: dict keyed by F76 label. Must contain all 8 base labels
            (V_21,34, V_12,34, V_32,41, V_23,41, V_43,12, V_34,12,
            V_14,23, V_41,23). Extra labels (from the extended-reciprocity
            protocol) are ignored at this level; callers that want to use
            those should average them into the base labels before calling.
        current: source current magnitude (A, positive).
        thickness_cm: sample thickness (cm, positive), or 0 or None when it
            was not given: then the resistivities are NaN and everything
            else is computed as usual.

    Returns:
        VdpResult. When the signs of the readings rule a result out
        (``vdp_sign_problem``), it comes back with ``valid`` False, the
        reason in ``invalid_reason``, and NaN for the sheet resistance and
        the resistivities: nothing is raised, so the caller still has Q,
        f and its own raw voltages to diagnose the wiring with.

    Raises:
        KeyError: if a required label is missing from voltages.
        ValueError: if current is non-positive, if a thickness is given
            and is negative or not finite, if a reading is not finite or is
            the instrument's overflow value, or if Q is undefined (a
            geometry reads the same at both polarities). The message names
            the F76 labels concerned.
    """
    missing = [label for label in _REQUIRED_BASE_LABELS if label not in voltages]
    if missing:
        raise KeyError(
            "Missing required F76 voltage labels: %s" % ", ".join(missing)
        )
    given = thickness_cm is not None and thickness_cm != 0
    if given and not (math.isfinite(thickness_cm) and thickness_cm > 0):
        raise ValueError("thickness must be > 0 cm, or 0 when not given; got %r"
                         % (thickness_cm,))

    rs_a, q_a, f_a = vdp_sheet_resistance_pair(
        voltages["V_21,34"], voltages["V_12,34"],
        voltages["V_32,41"], voltages["V_23,41"],
        current, labels=_BASE_LABELS_A,
    )
    rs_b, q_b, f_b = vdp_sheet_resistance_pair(
        voltages["V_43,12"], voltages["V_34,12"],
        voltages["V_14,23"], voltages["V_41,23"],
        current, labels=_BASE_LABELS_B,
    )

    # F76's check is |rho_A - rho_B| / rho_av; t cancels, so it is the same
    # on the sheet resistances, and holds whether or not t was given.
    rs_avg = 0.5 * (rs_a + rs_b)
    invalid_reason = vdp_sign_problem(voltages)
    if not invalid_reason and not rs_avg > 0:
        # Not reachable with four positive differences; kept so that no
        # non-positive R_s can leave here whatever the arithmetic did.
        invalid_reason = "the sheet resistance did not come out positive (%r)" % (rs_avg,)
    valid = not invalid_reason
    if valid:
        sheet_resistance = rs_avg
        asymmetry_pct = 100.0 * abs(rs_a - rs_b) / rs_avg
    else:
        sheet_resistance = float("nan")
        asymmetry_pct = float("inf")
    homogeneous = valid and asymmetry_pct <= F76_HOMOGENEITY_TOLERANCE_PCT
    # No resistivity without a valid R_s: rho = R_s * t, and a negative or
    # half-cancelled R_s times a thickness is not a resistivity.
    if given and valid:
        rho_a, rho_b = rs_a * thickness_cm, rs_b * thickness_cm
        rho_avg = 0.5 * (rho_a + rho_b)
    else:
        rho_a = rho_b = rho_avg = float("nan")

    return VdpResult(
        rho_a=rho_a,
        rho_b=rho_b,
        rho_avg=rho_avg,
        sheet_resistance=sheet_resistance,
        q_a=q_a,
        q_b=q_b,
        f_a=f_a,
        f_b=f_b,
        homogeneous=homogeneous,
        asymmetry_pct=asymmetry_pct,
        valid=valid,
        invalid_reason=invalid_reason,
    )




# ---------------------------------------------------------------------------
# Combined uncertainty on vdP sheet resistance and resistivity
# ---------------------------------------------------------------------------


class VdpCombinedUncertainty(NamedTuple):
    """Combined uncertainty for a vdP result.

    ``u_inst_R`` and ``u_stat_R`` are intermediate (standard uncertainties
    of the mean of the four per-geometry resistances, in ohms); ``u_rs``
    and ``u_rho`` are the final per-Rs / per-ρ uncertainties propagated
    through σ_X/X = σ_R/R (treating the F76 prefactor and thickness as
    exact). See ``vdp_combined_uncertainty`` for the model.
    """
    u_inst_R: float
    u_stat_R: float
    u_total_R: float
    u_rs: float
    u_rho: float


def vdp_combined_uncertainty(
    voltages,
    current,
    sheet_resistance,
    rho_avg,
    model: str = "2400",
    nplc: float = 1.0,
    v_fixed_range: Optional[float] = None,
) -> VdpCombinedUncertainty:
    """Combine statistical and instrument uncertainty for a vdP result.

    Inputs mirror the worker's ``_compute_and_emit_result`` arguments:
    ``voltages`` is the 8-key F76 dict (V_21,34 etc.); ``current`` is
    |I| in amps. All uncertainties are standard uncertainties (k = 1).
    ``v_fixed_range`` is the voltage range value that was sent when auto
    range was off (the run sends the compliance voltage); the accuracy
    lookup then uses the range the instrument was pinned to instead of
    the lowest one the reading would fit. None: auto range.

    Measurement model. Each geometry k gives a current-reversed
    resistance R_k = (V_k(+I) - V_k(-I)) / (2 I). The four are NOT four
    repeats of one quantity. A van der Pauw sample has two characteristic
    resistances, and by the reciprocity theorem (R_ab,cd = R_cd,ab) each
    is measured twice:

        R_1 = R_21,34  and  R_3 = R_43,12   estimate the same quantity,
        R_2 = R_32,41  and  R_4 = R_14,23   estimate the other.

    The two quantities differ by the factor Q whenever the sample is not
    square (Q = 3 means one is three times the other), and that
    difference is the sample's shape, carried by f(Q); it is not scatter.
    So the standard deviation of the four R_k is not used.

      - u_stat_R (Type A, GUM 4.2). The statistical evidence in the eight
        readings is the disagreement of each reciprocal pair. A mean of
        n = 2 observations has experimental standard deviation
        s / sqrt(2) = |x_1 - x_2| / 2 (GUM 4.2.3), so

            u(R_13) = |R_1 - R_3| / 2,    u(R_24) = |R_2 - R_4| / 2,

        and for R_avg = (R_13 + R_24) / 2, taking the two as uncorrelated
        (GUM 5.1.2),

            u_stat_R = (1/2) sqrt(u(R_13)^2 + u(R_24)^2)
                     = (1/4) sqrt((R_1 - R_3)^2 + (R_2 - R_4)^2).

        A noiseless sample of any Q gives exactly 0. Each pair has one
        degree of freedom, so this is a coarse estimate; it also takes
        in real non-reciprocity (an inhomogeneous sample, contacts of
        finite size), which is what F76's 10 percent gate is about, and
        is not removed here. The scatter between repeated reads at one
        polarity (``vdp_readings_per_polarity`` > 1) is averaged away
        before this function sees the voltages and is not a separate
        term: it reaches the result through the pair differences.

      - u_inst_R (Type B, GUM 4.3). For each geometry
        σ_R = √(σ_V_p² + σ_V_n²) / (2|I|), with σ_V_p and σ_V_n from
        accuracy.voltage_uncertainty on the +I and −I readings (the
        Keithley 1-year accuracy, ±(% of reading + range offset), taken
        as one standard deviation). u_inst_R is the mean of the four σ_R:
        the four are not reduced by √4, as errors of one instrument on one
        range are not independent from geometry to geometry.

        Assumption, stated because it is one: the full specification,
        gain term and offset term both, is applied to V(+I) and to V(−I)
        as if the two errors were independent. The datasheet gives one
        figure per range and no split into a part common to consecutive
        readings and a part that is not, so no correlation is assumed.
        The two extremes bracket it: an offset common to both readings
        cancels in V(+I) − V(−I), which makes the quadrature sum too
        large for the offset term; a gain error common to both adds
        linearly, which makes it too small for the gain term by up to
        √2. Which dominates depends on the reading as a fraction of the
        range. The sourced current is treated as exact.

      - u_total_R = √(u_inst_R² + u_stat_R²) (GUM 5.1.2).
      - Relative uncertainty propagates equally to Rs and ρ:
            σ_Rs / Rs = σ_ρ / ρ = u_total_R / |R_avg|
        with R_avg the mean of the four R_k, treating the F76 prefactor
        (π / ln2) · f and t as exact. f depends on Q and so on the same
        readings; its sensitivity (zero at Q = 1, small for moderate Q)
        is neglected.

    Returns NaN-filled tuple on missing keys / non-finite inputs.
    """
    from .accuracy import voltage_uncertainty
    try:
        r_values: list[float] = []
        per_geom_sigma_r: list[float] = []
        for p_label, n_label in _GEOMETRY_PAIRS:
            v_p = float(voltages[p_label]); v_n = float(voltages[n_label])
            sig_p = voltage_uncertainty(v_p, model=model, nplc=nplc,
                                        fixed_range=v_fixed_range)
            sig_n = voltage_uncertainty(v_n, model=model, nplc=nplc,
                                        fixed_range=v_fixed_range)
            per_geom_sigma_r.append(
                math.sqrt(sig_p ** 2 + sig_n ** 2) / (2.0 * abs(current))
            )
            r_values.append((v_p - v_n) / (2.0 * current))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        nan = float("nan")
        return VdpCombinedUncertainty(nan, nan, nan, nan, nan)

    r_avg = sum(r_values) / len(r_values)
    u_inst_R = sum(per_geom_sigma_r) / len(per_geom_sigma_r)
    # Reciprocal partners: geometries 1 and 3, geometries 2 and 4.
    r_1, r_2, r_3, r_4 = r_values
    u_stat_R = 0.25 * math.hypot(r_1 - r_3, r_2 - r_4)
    u_total_R = math.sqrt(u_inst_R ** 2 + u_stat_R ** 2)

    if r_avg == 0 or not math.isfinite(r_avg):
        rel = float("nan")
    else:
        rel = u_total_R / abs(r_avg)
    u_rs = abs(sheet_resistance) * rel if math.isfinite(rel) else float("nan")
    u_rho = abs(rho_avg) * rel if math.isfinite(rel) else float("nan")

    return VdpCombinedUncertainty(
        u_inst_R=u_inst_R,
        u_stat_R=u_stat_R,
        u_total_R=u_total_R,
        u_rs=u_rs,
        u_rho=u_rho,
    )

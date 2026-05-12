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
from typing import List, Mapping, NamedTuple, Tuple


F76_HOMOGENEITY_TOLERANCE_PCT = 10.0

# pi / (4 ln 2) ~= 1.1331 -- the constant in F76 eqs. (1) and (2).
F76_CONSTANT = math.pi / (4.0 * math.log(2.0))

_LN2 = math.log(2.0)


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

    rho_a, rho_b: redundant resistivity values (Ohm cm).
    rho_avg: averaged resistivity (Ohm cm).
    sheet_resistance: rho_avg / thickness (Ohm/square).
    q_a, q_b: voltage asymmetry ratios (normalized to >= 1).
    f_a, f_b: F76 Fig. 5 geometric factors derived from Q.
    homogeneous: True iff |rho_a - rho_b| / rho_avg <= 10 percent.
    asymmetry_pct: 100 * |rho_a - rho_b| / rho_avg.
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
) -> Tuple[float, float, float]:
    """Compute one of (rho_A, rho_B) per F76 eq. (1) or (2).

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

    Returns:
        (rho, Q, f). Q is normalized to >= 1.
    """
    if current <= 0:
        raise ValueError("current must be > 0 A; got %r" % (current,))
    if thickness_cm <= 0:
        raise ValueError("thickness must be > 0 cm; got %r" % (thickness_cm,))

    # Both deltas should be same-signed on a physical sample (current
    # reversal flips V; the subtraction doubles the signal and cancels
    # thermal offset). We compute |Q| and let the user inspect raw V if
    # the sign disagrees -- it usually means a wiring error.
    delta_first = v_pos - v_neg
    delta_second = v_perp_pos - v_perp_neg

    if delta_second == 0.0:
        raise ValueError(
            "Cannot compute Q: orthogonal voltage difference is exactly zero "
            "(degenerate sample or non-physical readings)."
        )

    q = abs(delta_first / delta_second)
    if q < 1.0:
        q = 1.0 / q

    f = vdp_geometric_factor(q)

    rho = (
        F76_CONSTANT * f * thickness_cm / current * (delta_first + delta_second)
    )
    return rho, q, f


_BASE_LABELS_A = ("V_21,34", "V_12,34", "V_32,41", "V_23,41")
_BASE_LABELS_B = ("V_43,12", "V_34,12", "V_14,23", "V_41,23")
_REQUIRED_BASE_LABELS = _BASE_LABELS_A + _BASE_LABELS_B


def calculate_van_der_pauw(
    voltages: Mapping[str, float],
    current: float,
    thickness_cm: float,
) -> VdpResult:
    """Compute resistivity, sheet resistance, and homogeneity per F76 Method A.

    Args:
        voltages: dict keyed by F76 label. Must contain all 8 base labels
            (V_21,34, V_12,34, V_32,41, V_23,41, V_43,12, V_34,12,
            V_14,23, V_41,23). Extra labels (from the extended-reciprocity
            protocol) are ignored at this level; callers that want to use
            those should average them into the base labels before calling.
        current: source current magnitude (A, positive).
        thickness_cm: sample thickness (cm, positive).

    Returns:
        VdpResult.

    Raises:
        KeyError: if a required label is missing from voltages.
        ValueError: if current or thickness is non-positive, or if Q is
            undefined (zero orthogonal voltage difference).
    """
    missing = [label for label in _REQUIRED_BASE_LABELS if label not in voltages]
    if missing:
        raise KeyError(
            "Missing required F76 voltage labels: %s" % ", ".join(missing)
        )

    rho_a, q_a, f_a = vdp_resistivity_pair(
        voltages["V_21,34"], voltages["V_12,34"],
        voltages["V_32,41"], voltages["V_23,41"],
        current, thickness_cm,
    )
    rho_b, q_b, f_b = vdp_resistivity_pair(
        voltages["V_43,12"], voltages["V_34,12"],
        voltages["V_14,23"], voltages["V_41,23"],
        current, thickness_cm,
    )

    rho_avg = 0.5 * (rho_a + rho_b)
    if rho_avg > 0:
        sheet_resistance = rho_avg / thickness_cm
        asymmetry_pct = 100.0 * abs(rho_a - rho_b) / rho_avg
    else:
        sheet_resistance = float("nan")
        asymmetry_pct = float("inf")
    homogeneous = asymmetry_pct <= F76_HOMOGENEITY_TOLERANCE_PCT

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
    )



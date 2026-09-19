"""Four-point-probe geometry factor at any position on a finite thin sample.

The correction tables in :mod:`calculations` (ASTM F84 Table 3 for a circle,
Smits 1958 for squares and rectangles) hold for a collinear probe at the
centre of the sample. The physics behind them has a closed form for a thin
sheet with insulating edges, and the closed form holds at any position and
orientation. This module evaluates it, so that

* a spot measured away from the centre can be told how wrong the centred
  factor is there (the basis of an edge warning), and
* a rectangle can have any aspect ratio, not the three that are tabulated.

Evaluated at the centre, :func:`circle_factor` agrees with every row of F84
Table 3 to within 0.0006 (20 of the 21 rows round to the printed value; the
row at S/D = 0.085 gives 4.2656 against a printed 4.265) and
:func:`rectangle_factor` agrees with the Smits table to within 0.05 %
(``tests/test_calculations_geometry.py`` pins both, and documents three table
entries that disagree with the series by more).

Conventions: lengths in any one unit, used consistently; positions are
``(x, y)`` measured from the centre of the sample; ``angle`` is the direction
of the probe array in radians, anticlockwise from +x. The probe is four
equally spaced collinear tips; current enters at the first and leaves at the
last, the voltage is read between the middle two.

The factor ``F`` is the one in ``Rs = F * V / I`` and tends to
``pi / ln 2 = 4.5324`` as the sample grows. Thickness is a separate
multiplicative correction, exactly as in F84, and is not handled here.

Pure functions: no Qt, no pyvisa, no I/O.
"""

from __future__ import annotations

import cmath
import math
from typing import NamedTuple, Tuple

#: pi / ln 2 -- the factor for an unbounded thin sheet.
UNBOUNDED_FACTOR = math.pi / math.log(2.0)

#: Image rows summed on each side of the rectangle. The closed-form sum runs
#: along the shorter side, so row n contributes a correction of order
#: exp(-2 * pi * n * longer / shorter) <= exp(-2 * pi * n): eight rows leave
#: 1e-22 for a square and less for anything longer.
_IMAGE_ROWS = 8

#: Beyond this imaginary part, ln|sin u| equals |Im u| - ln 2 to better than
#: 1e-26, and cmath.sin itself overflows a little past 700.
_LOG_SIN_ASYMPTOTE = 30.0

Point = Tuple[float, float]


class PositionEffect(NamedTuple):
    """What assuming a centred probe costs at one spot.

    ``relative_error`` is ``factor_centre / factor_here - 1``: the fractional
    error in a sheet resistance computed with the centred factor. Positive
    means the centred factor overstates Rs, which is what an edge does.
    """

    factor_here: float
    factor_centre: float
    relative_error: float


def probe_tips(centre: Point, angle: float, spacing: float) -> Tuple[complex, complex, complex, complex]:
    """The four tip positions as complex numbers, first current tip first."""
    if not (math.isfinite(spacing) and spacing > 0):
        raise ValueError("probe spacing must be positive and finite")
    origin = complex(centre[0], centre[1])
    direction = cmath.exp(1j * angle)
    return tuple(origin + (k - 1.5) * spacing * direction for k in range(4))  # type: ignore[return-value]


def _factor_from(kernel, tips) -> float:
    """``2*pi / bracket`` for a kernel ``k(P, Q)`` = potential at P per unit
    sink at Q, up to terms that cancel between the source and the sink."""
    source, v_high, v_low, sink = tips
    bracket = (kernel(v_high, sink) - kernel(v_high, source)
               - kernel(v_low, sink) + kernel(v_low, source))
    return 2.0 * math.pi / bracket


def circle_factor(diameter: float, spacing: float,
                  centre: Point = (0.0, 0.0), angle: float = 0.0) -> float:
    """Geometry factor for a thin disc with an insulating rim.

    A point source in such a disc is the free-space logarithm plus one image
    of the same sign at the inverse point ``R**2 / conj(Q)``; written without
    the division it stays finite for a source at the centre.

    Raises ``ValueError`` when a tip is not inside the disc.
    """
    _require_positive(diameter, "diameter")
    radius = diameter / 2.0
    tips = probe_tips(centre, angle, spacing)
    if circle_edge_clearance(diameter, spacing, centre, angle) <= 0:
        raise ValueError("a probe tip is on or outside the edge of the sample")

    def kernel(p: complex, q: complex) -> float:
        return math.log(abs(p - q) * abs(radius * radius - q.conjugate() * p))

    return _factor_from(kernel, tips)


def rectangle_factor(width: float, length: float, spacing: float,
                     centre: Point = (0.0, 0.0), angle: float = 0.0) -> float:
    """Geometry factor for a thin rectangle with insulating edges.

    ``length`` lies along x and ``width`` along y, so a probe at ``angle = 0``
    runs along the length -- the arrangement of the Smits table, whose ``D``
    is this ``width``.

    Reflections in the four edges put images on a doubly periodic lattice.
    The sum along one axis has the closed form
    ``ln|sin(pi (z - a) / (2 * side))|``; what remains is a sum over rows
    along the other axis that converges exponentially, because the terms
    linear in the row index cancel between the source and the sink.

    Raises ``ValueError`` when a tip is not inside the rectangle.
    """
    _require_positive(width, "width")
    _require_positive(length, "length")
    tips = probe_tips(centre, angle, spacing)
    if rectangle_edge_clearance(width, length, spacing, centre, angle) <= 0:
        raise ValueError("a probe tip is on or outside the edge of the sample")

    # The image sum has a closed form along one axis and converges
    # exponentially along the other, at a rate set by (row spacing) /
    # (closed-form period). Putting the closed form along the shorter side
    # makes that ratio at least one for any sample, so a long strip costs no
    # more rows than a square. Swapping x and y is a mirror, which leaves the
    # factor unchanged.
    if length > width:
        period_side, row_side = width, length
        tips = tuple(complex(t.imag, t.real) for t in tips)
    else:
        period_side, row_side = length, width

    # Origin at a corner, where the reflections are x -> -x and y -> -y plus
    # the lattice translations.
    shift = complex(period_side / 2.0, row_side / 2.0)
    shifted = tuple(t + shift for t in tips)

    def kernel(p: complex, q: complex) -> float:
        total = 0.0
        for row in range(-_IMAGE_ROWS, _IMAGE_ROWS + 1):
            for sign_y in (1.0, -1.0):
                image_y = sign_y * q.imag + 2.0 * row * row_side
                for sign_x in (1.0, -1.0):
                    image = complex(sign_x * q.real, image_y)
                    total += _log_abs_sin(math.pi * (p - image) / (2.0 * period_side))
        return total

    return _factor_from(kernel, shifted)


def circle_edge_clearance(diameter: float, spacing: float,
                          centre: Point = (0.0, 0.0), angle: float = 0.0) -> float:
    """Smallest distance from any tip to the rim, in units of the spacing.

    Zero or negative means a tip is on or beyond the edge.
    """
    _require_positive(diameter, "diameter")
    tips = probe_tips(centre, angle, spacing)
    return min(diameter / 2.0 - abs(t) for t in tips) / spacing


def rectangle_edge_clearance(width: float, length: float, spacing: float,
                             centre: Point = (0.0, 0.0), angle: float = 0.0) -> float:
    """Smallest distance from any tip to any edge, in units of the spacing."""
    _require_positive(width, "width")
    _require_positive(length, "length")
    tips = probe_tips(centre, angle, spacing)
    return min(min(length / 2.0 - abs(t.real), width / 2.0 - abs(t.imag)) for t in tips) / spacing


def circle_position_effect(diameter: float, spacing: float,
                           centre: Point, angle: float = 0.0) -> PositionEffect:
    """The factor at ``centre`` against the factor at the middle of the disc."""
    here = circle_factor(diameter, spacing, centre, angle)
    middle = circle_factor(diameter, spacing, (0.0, 0.0), angle)
    return PositionEffect(here, middle, middle / here - 1.0)


def rectangle_position_effect(width: float, length: float, spacing: float,
                              centre: Point, angle: float = 0.0) -> PositionEffect:
    """The factor at ``centre`` against the factor at the middle of the
    rectangle, with the probe pointing the same way in both."""
    here = rectangle_factor(width, length, spacing, centre, angle)
    middle = rectangle_factor(width, length, spacing, (0.0, 0.0), angle)
    return PositionEffect(here, middle, middle / here - 1.0)


def _log_abs_sin(u: complex) -> float:
    """``ln|sin u|`` without the overflow of ``sin`` far from the real axis."""
    if abs(u.imag) > _LOG_SIN_ASYMPTOTE:
        return abs(u.imag) - math.log(2.0)
    return math.log(abs(cmath.sin(u)))


def _require_positive(value: float, name: str) -> None:
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be positive and finite")

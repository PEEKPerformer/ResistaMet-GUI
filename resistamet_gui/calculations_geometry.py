"""Four-point-probe geometry factor at any position on a finite thin sample.

The correction tables in :mod:`calculations` (ASTM F84 Table 3 for a circle,
Smits 1958 for squares and rectangles) hold for a collinear probe at the
centre of the sample. The physics behind them has a closed form for a thin
sheet with insulating edges, and the closed form holds at any position and
orientation. This module evaluates it, so that

* a spot measured away from the centre can be told how wrong the centred
  factor is there (the basis of an edge warning), and
* a rectangle can have any aspect ratio, not the three that are tabulated.

Evaluated at the centre, :func:`circle_factor` reproduces every row of F84
Table 3 to the table's last printed digit and :func:`rectangle_factor`
reproduces the Smits table to four digits (``tests/
test_calculations_geometry.py`` pins both, and documents three table entries
that disagree with the series).

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

#: Image rows summed on each side along the rectangle's second axis. The
#: terms fall off as exp(-pi * n * L / W); forty rows are far beyond double
#: precision for any aspect ratio a sample can have.
_IMAGE_ROWS = 40

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
    The sum along x has the closed form ``ln|sin(pi (z - a) / (2 * length))|``;
    what remains is a sum over rows along y that converges exponentially,
    because the terms linear in the row index cancel between the source and
    the sink.

    Raises ``ValueError`` when a tip is not inside the rectangle.
    """
    _require_positive(width, "width")
    _require_positive(length, "length")
    tips = probe_tips(centre, angle, spacing)
    if rectangle_edge_clearance(width, length, spacing, centre, angle) <= 0:
        raise ValueError("a probe tip is on or outside the edge of the sample")

    # Work in coordinates with the origin at a corner, where the reflections
    # are x -> -x and y -> -y plus the lattice translations.
    shift = complex(length / 2.0, width / 2.0)
    shifted = tuple(t + shift for t in tips)

    def kernel(p: complex, q: complex) -> float:
        total = 0.0
        for row in range(-_IMAGE_ROWS, _IMAGE_ROWS + 1):
            for sign_y in (1.0, -1.0):
                image_y = sign_y * q.imag + 2.0 * row * width
                for sign_x in (1.0, -1.0):
                    image = complex(sign_x * q.real, image_y)
                    total += math.log(abs(cmath.sin(math.pi * (p - image) / (2.0 * length))))
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


def _require_positive(value: float, name: str) -> None:
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be positive and finite")

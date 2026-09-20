"""Properties of the position-aware 4PP geometry factor.

``test_calculations_geometry.py`` pins the factor against the published
tables at chosen points. This module states what must hold at *every* point:
the symmetries of the sample, the bound set by the unbounded sheet, the way
an edge lowers the factor, and the error contract for a probe that is not on
the sample. Inputs are drawn by hypothesis; the run is derandomised and keeps
no example database, so it is the same on every machine and writes nothing.

Tolerances are a decade or two above the worst disagreement found over
tens of thousands of draws: the image sum subtracts large near-equal terms,
which leaves about 1e-10 relative noise for a sample 1000 spacings across and
about 1e-7 at the 1e9 ratio the dimension limits allow.
"""

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from resistamet_gui import calculations_geometry as geo

# No deadline: the properties bound numbers, not wall-clock time, and a shared
# CI runner makes per-example timing flaky. max_examples caps the run instead.
PROPERTY = settings(max_examples=150, deadline=None, database=None, derandomize=True)

U = geo.UNBOUNDED_FACTOR


def _log_uniform(low: float, high: float):
    return st.floats(math.log10(low), math.log10(high)).map(lambda e: 10.0 ** e)


unit = st.floats(-1.0, 1.0)
angles = st.floats(-2.0 * math.pi, 2.0 * math.pi)
fraction = st.floats(0.0, 1.0)


@st.composite
def probes_on_a_rectangle(draw, low=1.0, high=1e3, fill=0.98):
    """(width, length, spacing, centre, angle) with all four tips inside."""
    width = draw(_log_uniform(low, high))
    length = draw(_log_uniform(low, high))
    angle = draw(angles)
    reach_x = 1.5 * abs(math.cos(angle))
    reach_y = 1.5 * abs(math.sin(angle))
    # Largest spacing that fits at the centre, then some fraction of it.
    limit = min(length / 2.0 / reach_x if reach_x > 1e-12 else math.inf,
                width / 2.0 / reach_y if reach_y > 1e-12 else math.inf)
    spacing = limit * draw(st.floats(0.01, 0.9))
    room_x = (length / 2.0 - reach_x * spacing) * fill
    room_y = (width / 2.0 - reach_y * spacing) * fill
    centre = (draw(unit) * room_x, draw(unit) * room_y)
    return width, length, spacing, centre, angle


@st.composite
def probes_on_a_disc(draw, low=1.0, high=1e3, fill=0.98):
    """(diameter, spacing, (r, phi) of the centre, angle), tips inside."""
    diameter = draw(_log_uniform(low, high))
    spacing = diameter / 3.0 * draw(st.floats(0.01, 0.9))
    # Whatever the orientation, a probe whose centre is within
    # R - 1.5 s of the middle has every tip inside.
    r = (diameter / 2.0 - 1.5 * spacing) * fill * draw(fraction)
    return diameter, spacing, r, draw(angles), draw(angles)


def _xy(r: float, phi: float):
    return (r * math.cos(phi), r * math.sin(phi))


class TestRectangleSymmetry:
    """The rectangle's symmetry group leaves the factor alone."""

    @PROPERTY
    @given(probes_on_a_rectangle())
    def test_mirror_in_x(self, case):
        w, l, s, (x, y), a = case
        assert geo.rectangle_factor(w, l, s, (-x, y), math.pi - a) == pytest.approx(
            geo.rectangle_factor(w, l, s, (x, y), a), rel=1e-8)

    @PROPERTY
    @given(probes_on_a_rectangle())
    def test_mirror_in_y(self, case):
        w, l, s, (x, y), a = case
        assert geo.rectangle_factor(w, l, s, (x, -y), -a) == pytest.approx(
            geo.rectangle_factor(w, l, s, (x, y), a), rel=1e-8)

    @PROPERTY
    @given(probes_on_a_rectangle())
    def test_half_turn(self, case):
        w, l, s, (x, y), a = case
        here = geo.rectangle_factor(w, l, s, (x, y), a)
        assert geo.rectangle_factor(w, l, s, (-x, -y), a + math.pi) == pytest.approx(here, rel=1e-8)
        # A half turn of the probe alone swaps source with sink and the two
        # voltage tips, which changes nothing.
        assert geo.rectangle_factor(w, l, s, (-x, -y), a) == pytest.approx(here, rel=1e-8)

    @PROPERTY
    @given(probes_on_a_rectangle())
    def test_quarter_turn_swaps_the_axes(self, case):
        w, l, s, (x, y), a = case
        turned = geo.rectangle_factor(l, w, s, (-y, x), a + math.pi / 2.0)
        assert turned == pytest.approx(geo.rectangle_factor(w, l, s, (x, y), a), rel=1e-8)

    @PROPERTY
    @given(probes_on_a_rectangle())
    def test_clearance_has_the_same_symmetries(self, case):
        w, l, s, (x, y), a = case
        here = geo.rectangle_edge_clearance(w, l, s, (x, y), a)
        assert here > 0
        for other in (geo.rectangle_edge_clearance(w, l, s, (-x, y), math.pi - a),
                      geo.rectangle_edge_clearance(w, l, s, (x, -y), -a),
                      geo.rectangle_edge_clearance(l, w, s, (-y, x), a + math.pi / 2.0)):
            assert other == pytest.approx(here, rel=1e-9, abs=1e-9)


class TestDiscSymmetry:
    @PROPERTY
    @given(probes_on_a_disc(), angles)
    def test_any_rotation_about_the_centre(self, case, turn):
        d, s, r, phi, a = case
        assert geo.circle_factor(d, s, _xy(r, phi + turn), a + turn) == pytest.approx(
            geo.circle_factor(d, s, _xy(r, phi), a), rel=1e-10)

    @PROPERTY
    @given(probes_on_a_disc())
    def test_mirror_and_probe_reversal(self, case):
        d, s, r, phi, a = case
        here = geo.circle_factor(d, s, _xy(r, phi), a)
        assert geo.circle_factor(d, s, _xy(r, -phi), -a) == pytest.approx(here, rel=1e-10)
        assert geo.circle_factor(d, s, _xy(r, phi), a + math.pi) == pytest.approx(here, rel=1e-10)


class TestBounds:
    """0 < F <= pi / ln 2 wherever the probe fits, over the whole range of
    dimensions the settings allow (1e-3 to 1e6, in any unit)."""

    @PROPERTY
    @given(probes_on_a_rectangle(low=1e-3, high=1e6, fill=0.999999))
    def test_rectangle(self, case):
        w, l, s, centre, a = case
        factor = geo.rectangle_factor(w, l, s, centre, a)
        assert 0.0 < factor <= U * (1.0 + 1e-6)

    @PROPERTY
    @given(probes_on_a_disc(low=1e-3, high=1e6, fill=0.999999))
    def test_disc(self, case):
        d, s, r, phi, a = case
        factor = geo.circle_factor(d, s, _xy(r, phi), a)
        assert 0.0 < factor <= U * (1.0 + 1e-6)

    @PROPERTY
    @given(_log_uniform(1e-3, 1e6), _log_uniform(1.0, 1e4), st.booleans(), st.floats(1e-6, 0.3))
    def test_extreme_aspect_ratios(self, short, ratio, along_x, spacing_fraction):
        long_side = min(short * ratio, 1e6)
        width, length = (short, long_side) if along_x else (long_side, short)
        spacing = short * spacing_fraction
        for angle in (0.0, math.pi / 2.0, 0.7):
            factor = geo.rectangle_factor(width, length, spacing, angle=angle)
            assert 0.0 < factor <= U * (1.0 + 1e-6)


def _no_higher(nearer: float, farther: float) -> bool:
    return nearer <= farther * (1.0 + 1e-8)


def _wide(*sides: float) -> bool:
    """On a strip narrower than a few spacings the current between the outer
    tips is one-dimensional and an end changes the factor by less than the
    rounding, so only "no higher" can be asserted there, not "lower"."""
    return min(sides) >= 10.0


class TestApproachingAnEdge:
    """An insulating edge raises the voltage, so it lowers the factor, and
    more the nearer it is. Spacing is 1; distances are in spacings."""

    @PROPERTY
    @given(_log_uniform(1.0, 1e4), _log_uniform(3.2, 1e4), unit, fraction, fraction)
    def test_rectangle_probe_parallel_to_the_edge(self, w, l, ux, f1, f2):
        x = ux * (l / 2.0 - 1.5) * 0.999
        near, far = sorted((f1 * w / 2.0 * 0.9999, f2 * w / 2.0 * 0.9999), reverse=True)
        f_near = geo.rectangle_factor(w, l, 1.0, (x, -near))
        f_far = geo.rectangle_factor(w, l, 1.0, (x, -far))
        assert _no_higher(f_near, f_far)
        if _wide(w, l) and near - far > 0.5 and w / 2.0 - near < 5.0:
            assert f_near < f_far

    @PROPERTY
    @given(_log_uniform(3.2, 1e4), _log_uniform(1.0, 1e4), fraction, fraction)
    def test_rectangle_probe_pointing_at_the_edge(self, w, l, f1, f2):
        room = (w / 2.0 - 1.5) * 0.9999
        near, far = sorted((f1 * room, f2 * room), reverse=True)
        f_near = geo.rectangle_factor(w, l, 1.0, (0.0, -near), math.pi / 2.0)
        f_far = geo.rectangle_factor(w, l, 1.0, (0.0, -far), math.pi / 2.0)
        assert _no_higher(f_near, f_far)
        if _wide(w, l) and near - far > 0.5 and room - near < 5.0:
            assert f_near < f_far

    @PROPERTY
    @given(_log_uniform(3.2, 1e4), fraction, fraction, angles, st.booleans())
    def test_disc(self, d, f1, f2, phi, tangential):
        if tangential:
            room = math.sqrt((d / 2.0) ** 2 - 1.5 ** 2) * 0.9999
            angle = phi + math.pi / 2.0
        else:
            room = (d / 2.0 - 1.5) * 0.9999
            angle = phi
        near, far = sorted((f1 * room, f2 * room), reverse=True)
        f_near = geo.circle_factor(d, 1.0, _xy(near, phi), angle)
        f_far = geo.circle_factor(d, 1.0, _xy(far, phi), angle)
        assert _no_higher(f_near, f_far)
        if _wide(d) and near - far > 0.5 and room - near < 5.0:
            assert f_near < f_far


class TestPositionEffect:
    """``relative_error`` is centre / here - 1. On a sample wider than the
    probe is long it is never negative: for a fixed orientation the middle
    has the largest factor. A strip narrower than the probe, which the probe
    only fits on at a slant, is the exception, and is pinned as one."""

    @PROPERTY
    @given(probes_on_a_rectangle())
    def test_rectangle(self, case):
        w, l, s, centre, a = case
        effect = geo.rectangle_position_effect(w, l, s, centre, a)
        assert effect.factor_here == geo.rectangle_factor(w, l, s, centre, a)
        assert effect.factor_centre == geo.rectangle_factor(w, l, s, (0.0, 0.0), a)
        assert effect.relative_error == pytest.approx(effect.factor_centre / effect.factor_here - 1.0, abs=1e-15)
        assert (effect.relative_error > 0) == (effect.factor_here < effect.factor_centre)
        if min(w, l) >= 3.5 * s:
            assert effect.relative_error >= -1e-8

    def test_a_slanted_probe_on_a_narrow_strip_can_gain_from_moving_off_centre(self):
        # 1.28 s x 3.24 s: the factor off-centre is 0.24 % above the centre's.
        # Consumers must judge |relative_error|, as the edge warning does.
        effect = geo.rectangle_position_effect(1.276, 3.244, 1.0, (-0.1403, -0.1001), 2.7757)
        assert -3e-3 < effect.relative_error < -2e-3

    @PROPERTY
    @given(probes_on_a_disc())
    def test_disc(self, case):
        d, s, r, phi, a = case
        effect = geo.circle_position_effect(d, s, _xy(r, phi), a)
        assert effect.relative_error == pytest.approx(effect.factor_centre / effect.factor_here - 1.0, abs=1e-15)
        assert effect.relative_error >= -1e-10

    def test_the_sign_means_the_centred_factor_overstates_rs(self):
        effect = geo.rectangle_position_effect(20.0, 20.0, 1.0, (0.0, -8.0))
        true_rs = 1.0
        ratio_v_over_i = true_rs / effect.factor_here
        assumed_rs = effect.factor_centre * ratio_v_over_i
        assert assumed_rs / true_rs - 1.0 == pytest.approx(effect.relative_error, rel=1e-12)
        assert effect.relative_error > 0


class TestContinuity:
    """Away from the edge a small move is a small change. With every tip at
    least 0.1 spacing inside, the slope stays under 1.5 per spacing (and per
    radian); 5 leaves room without admitting a jump."""

    SLOPE = 5.0

    @PROPERTY
    @given(probes_on_a_rectangle(high=300.0, fill=0.9), st.floats(1e-5, 1e-2), angles)
    def test_rectangle(self, case, step, heading):
        w, l, s, (x, y), a = case
        moved = (x + step * s * math.cos(heading), y + step * s * math.sin(heading))
        if min(geo.rectangle_edge_clearance(w, l, s, (x, y), a),
               geo.rectangle_edge_clearance(w, l, s, moved, a),
               geo.rectangle_edge_clearance(w, l, s, (x, y), a + step)) < 0.1:
            return
        here = geo.rectangle_factor(w, l, s, (x, y), a)
        assert abs(geo.rectangle_factor(w, l, s, moved, a) - here) <= self.SLOPE * step + 1e-8
        assert abs(geo.rectangle_factor(w, l, s, (x, y), a + step) - here) <= self.SLOPE * step + 1e-8

    @PROPERTY
    @given(probes_on_a_disc(high=300.0, fill=0.9), st.floats(1e-5, 1e-2), angles)
    def test_disc(self, case, step, heading):
        d, s, r, phi, a = case
        x, y = _xy(r, phi)
        moved = (x + step * s * math.cos(heading), y + step * s * math.sin(heading))
        if min(geo.circle_edge_clearance(d, s, (x, y), a),
               geo.circle_edge_clearance(d, s, moved, a)) < 0.1:
            return
        here = geo.circle_factor(d, s, (x, y), a)
        assert abs(geo.circle_factor(d, s, moved, a) - here) <= self.SLOPE * step + 1e-8
        assert abs(geo.circle_factor(d, s, (x, y), a + step) - here) <= self.SLOPE * step + 1e-8


dimension = _log_uniform(1e-3, 1e6)
anywhere = st.floats(-1e300, 1e300)


class TestProbeOffTheSample:
    """A tip on or past the edge is a ValueError that says so -- never an
    OverflowError or ZeroDivisionError out of the series, and never a number.
    """

    @PROPERTY
    @given(dimension, _log_uniform(1.0, 1e4), st.booleans(), dimension, anywhere, anywhere, angles)
    def test_rectangle_any_input_is_a_factor_or_an_edge_error(self, short, ratio, along_x, s, x, y, a):
        long_side = min(short * ratio, 1e6)
        w, l = (short, long_side) if along_x else (long_side, short)
        inside = geo.rectangle_edge_clearance(w, l, s, (x, y), a) > 0
        try:
            factor = geo.rectangle_factor(w, l, s, (x, y), a)
        except ValueError as exc:
            assert not inside
            assert "edge" in str(exc)
        else:
            assert inside
            assert 0.0 < factor <= U * (1.0 + 1e-6)

    @PROPERTY
    @given(dimension, dimension, anywhere, anywhere, angles)
    def test_disc_any_input_is_a_factor_or_an_edge_error(self, d, s, x, y, a):
        inside = geo.circle_edge_clearance(d, s, (x, y), a) > 0
        try:
            factor = geo.circle_factor(d, s, (x, y), a)
        except ValueError as exc:
            assert not inside
            assert "edge" in str(exc)
        else:
            assert inside
            assert 0.0 < factor <= U * (1.0 + 1e-6)

    @PROPERTY
    @given(dimension, _log_uniform(1.0, 1e4), st.booleans(), st.floats(1.0, 1e6), unit, angles, st.integers(0, 3))
    def test_rectangle_just_past_an_edge(self, short, ratio, along_x, overshoot, u, a, edge):
        """One tip pushed past a chosen edge by anything from one part in
        1e9 of the side to a million sides."""
        long_side = min(short * ratio, 1e6)
        w, l = (short, long_side) if along_x else (long_side, short)
        s = short * 0.1
        tips = geo.probe_tips((0.0, 0.0), a, s)
        past = overshoot * 1e-9
        if edge < 2:        # beyond +x or -x
            sign = 1.0 if edge == 0 else -1.0
            reach = max(sign * t.real for t in tips)
            centre = (sign * (l / 2.0 * (1.0 + past) - reach), u * w)
        else:               # beyond +y or -y
            sign = 1.0 if edge == 2 else -1.0
            reach = max(sign * t.imag for t in tips)
            centre = (u * l, sign * (w / 2.0 * (1.0 + past) - reach))
        assert geo.rectangle_edge_clearance(w, l, s, centre, a) <= 0
        with pytest.raises(ValueError, match="edge"):
            geo.rectangle_factor(w, l, s, centre, a)
        with pytest.raises(ValueError, match="edge"):
            geo.rectangle_position_effect(w, l, s, centre, a)

    @PROPERTY
    @given(dimension, st.floats(1.0, 1e6), angles)
    def test_disc_tip_exactly_on_or_past_the_rim(self, d, overshoot, phi):
        s = d * 0.05
        # Radial probe: the outer tip is 1.5 s beyond the centre.
        r = d / 2.0 * (1.0 + overshoot * 1e-9) - 1.5 * s
        centre = _xy(r, phi)
        if geo.circle_edge_clearance(d, s, centre, phi) > 0:
            return      # rounding in cos/sin pulled the tip back inside
        with pytest.raises(ValueError, match="edge"):
            geo.circle_factor(d, s, centre, phi)

    @pytest.mark.parametrize("centre, angle", [
        ((float("nan"), 0.0), 0.0),
        ((0.0, float("nan")), 0.0),
        ((0.0, 0.0), float("nan")),
        ((0.0, 0.0), float("inf")),
    ])
    def test_a_position_that_is_not_a_number_is_refused(self, centre, angle):
        with pytest.raises(ValueError):
            geo.rectangle_factor(10.0, 10.0, 1.0, centre, angle)
        with pytest.raises(ValueError):
            geo.circle_factor(10.0, 1.0, centre, angle)
        with pytest.raises(ValueError):
            geo.rectangle_edge_clearance(10.0, 10.0, 1.0, centre, angle)
        with pytest.raises(ValueError):
            geo.circle_edge_clearance(10.0, 1.0, centre, angle)
        with pytest.raises(ValueError):
            geo.rectangle_position_effect(10.0, 10.0, 1.0, centre, angle)
        with pytest.raises(ValueError):
            geo.circle_position_effect(10.0, 1.0, centre, angle)


class TestLargeSampleLimit:
    """Two independent kernels -- one image in a circle, a doubly periodic
    lattice in a rectangle -- must agree where the shape stops mattering."""

    BIG = 1e5

    @PROPERTY
    @given(st.floats(-100.0, 100.0), st.floats(-100.0, 100.0), angles)
    def test_far_from_every_edge_both_are_the_unbounded_sheet(self, x, y, a):
        rect = geo.rectangle_factor(self.BIG, self.BIG, 1.0, (x, y), a)
        disc = geo.circle_factor(self.BIG, 1.0, (x, y), a)
        assert rect == pytest.approx(disc, rel=1e-7)
        assert rect == pytest.approx(U, rel=1e-7)

    @PROPERTY
    @given(st.floats(1.6, 30.0), st.floats(0.0, math.pi))
    def test_near_one_edge_both_are_the_half_plane(self, distance, a):
        """Probe ``distance`` spacings from the lowest point of the boundary:
        a straight edge for the rectangle, a rim of radius 5e4 for the disc.
        The curvature is worth about s / R."""
        if distance - 1.5 * abs(math.sin(a)) < 0.05:
            return
        centre = (0.0, -self.BIG / 2.0 + distance)
        rect = geo.rectangle_factor(self.BIG, self.BIG, 1.0, centre, a)
        disc = geo.circle_factor(self.BIG, 1.0, centre, a)
        assert rect == pytest.approx(disc, rel=1e-4)
        assert rect < U


class TestScaleInvariance:
    """Only ratios of lengths matter, so the unit is the user's choice."""

    @PROPERTY
    @given(probes_on_a_rectangle(), _log_uniform(1e-3, 1e3))
    def test_rectangle(self, case, k):
        w, l, s, (x, y), a = case
        assert geo.rectangle_factor(w * k, l * k, s * k, (x * k, y * k), a) == pytest.approx(
            geo.rectangle_factor(w, l, s, (x, y), a), rel=1e-8)
        assert geo.rectangle_edge_clearance(w * k, l * k, s * k, (x * k, y * k), a) == pytest.approx(
            geo.rectangle_edge_clearance(w, l, s, (x, y), a), rel=1e-9, abs=1e-9)

    @PROPERTY
    @given(probes_on_a_disc(), _log_uniform(1e-3, 1e3))
    def test_disc(self, case, k):
        d, s, r, phi, a = case
        assert geo.circle_factor(d * k, s * k, _xy(r * k, phi), a) == pytest.approx(
            geo.circle_factor(d, s, _xy(r, phi), a), rel=1e-10)

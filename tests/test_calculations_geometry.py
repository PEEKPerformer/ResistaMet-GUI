"""The position-aware 4PP geometry factor against the tables it generalises."""

import math

import pytest

from resistamet_gui import calculations as calc
from resistamet_gui import calculations_geometry as geo


class TestCircleAgainstF84Table3:
    """At the centre the closed form is ASTM F84 Table 3."""

    #: The one row that does not round to the printed value: the closed form
    #: gives 4.26555, the table prints 4.265.
    ODD_ROW = 0.085

    @pytest.mark.parametrize("s_over_d, table_f2",
                             [row for row in calc._F84_TABLE3_F2 if row[0] > 0])
    def test_every_row_rounds_to_the_printed_value(self, s_over_d, table_f2):
        factor = geo.circle_factor(diameter=1.0 / s_over_d, spacing=1.0)
        if s_over_d == self.ODD_ROW:
            assert factor == pytest.approx(4.26555, abs=1e-5)
            assert abs(factor - table_f2) < 6e-4
        else:
            assert round(factor, 3) == pytest.approx(table_f2, abs=1e-9)

    def test_large_disc_tends_to_the_unbounded_sheet(self):
        assert geo.circle_factor(diameter=1e6, spacing=1.0) == pytest.approx(math.pi / math.log(2), rel=1e-9)
        assert geo.UNBOUNDED_FACTOR == pytest.approx(4.5324, abs=5e-5)

    def test_units_do_not_matter(self):
        assert geo.circle_factor(5.08, 0.1016) == pytest.approx(geo.circle_factor(50.8, 1.016), rel=1e-12)


#: Table entries the image series does not reproduce. Every other entry
#: agrees to 0.06 %; these look like transcription errors in the table, which
#: is left as it is until the original has been checked.
#: (D/s, column) -> what the series gives.
SMITS_DISAGREEMENTS = {
    (32.0, 1): 4.4997, (32.0, 2): 4.5011, (32.0, 3): 4.5011, (32.0, 4): 4.5011,
}
SMITS_LOW_DISAGREEMENTS = {
    (1.25, 3): 1.2468,   # L/W = 4; the table has 1.2248, its L/W = 3 neighbour 1.2467
    (2.0, 1): 1.9454,    # L/W = 2; the table has 1.9475
}
_RATIOS = {1: 1.0, 2: 2.0, 3: 3.0, 4: 4.0}


def _smits_cases():
    for row in calc._SMITS_GEOMETRY_CF:
        d_over_s = row[0]
        if d_over_s > 1e6:
            continue
        for col in (1, 2, 3, 4):
            yield d_over_s, _RATIOS[col], row[col], SMITS_DISAGREEMENTS.get((d_over_s, col))
    for row in calc._SMITS_RECT_LOW_DS:
        d_over_s = row[0]
        for col, ratio in ((1, 2.0), (2, 3.0), (3, 4.0)):
            if math.isnan(row[col]):
                continue
            yield d_over_s, ratio, row[col], SMITS_LOW_DISAGREEMENTS.get((d_over_s, col))


class TestRectangleAgainstSmits:
    """At the centre, probe along the length, the series is the Smits table."""

    @pytest.mark.parametrize("d_over_s, ratio, table_value, series_value", list(_smits_cases()))
    def test_table_entry(self, d_over_s, ratio, table_value, series_value):
        length = ratio * d_over_s
        if length <= 3.0:
            pytest.skip("the probe (3 s long) does not fit on this sample")
        factor = geo.rectangle_factor(width=d_over_s, length=length, spacing=1.0)
        if series_value is None:
            assert factor == pytest.approx(table_value, rel=6e-4)
        else:
            # Documented disagreement: pin the series, and show the table is
            # off by more than the agreement everywhere else.
            assert factor == pytest.approx(series_value, abs=1e-4)
            assert abs(factor / table_value - 1.0) > 1e-3

    def test_large_rectangle_tends_to_the_unbounded_sheet(self):
        assert geo.rectangle_factor(2000.0, 3000.0, 1.0) == pytest.approx(geo.UNBOUNDED_FACTOR, rel=1e-5)

    def test_arbitrary_aspect_ratio_lies_between_its_tabulated_neighbours(self):
        square = geo.rectangle_factor(5.0, 5.0, 1.0)
        between = geo.rectangle_factor(5.0, 7.5, 1.0)
        double = geo.rectangle_factor(5.0, 10.0, 1.0)
        assert square < between < double


class TestAnyAspectRatio:
    """The sum converges, and does not overflow, whatever the sample's shape."""

    def test_turning_sample_and_probe_together_changes_nothing(self):
        a = geo.rectangle_factor(20.0, 30.0, 1.0, centre=(3.0, 2.0), angle=0.3)
        b = geo.rectangle_factor(30.0, 20.0, 1.0, centre=(2.0, 3.0), angle=math.pi / 2 - 0.3)
        assert a == pytest.approx(b, rel=1e-12)

    @pytest.mark.parametrize("width, length", [(60.0, 10.0), (1000.0, 4.5), (5.0, 1000.0), (4.0, 1000.0)])
    def test_extreme_shapes_give_a_finite_factor(self, width, length):
        angle = 0.0 if length > 3.0 else math.pi / 2
        factor = geo.rectangle_factor(width, length, 1.0, angle=angle)
        assert 0.0 < factor < geo.UNBOUNDED_FACTOR

    def test_a_long_strip_no_longer_depends_on_its_length(self):
        # Probe along a strip 5 s wide: once the ends are far away the factor
        # is the strip's own, 3.5750 in the Smits table for L/W >= 3. The far
        # rows are large numbers that cancel, which costs a few digits.
        assert geo.rectangle_factor(5.0, 200.0, 1.0) == pytest.approx(geo.rectangle_factor(5.0, 1000.0, 1.0), rel=1e-9)
        assert geo.rectangle_factor(5.0, 1000.0, 1.0) == pytest.approx(3.5750, abs=1e-4)

    def test_log_sin_far_from_the_axis_matches_the_function_near_the_switch(self):
        for im in (29.9, 30.1):
            u = complex(0.7, im)
            assert geo._log_abs_sin(u) == pytest.approx(im - math.log(2.0), rel=1e-12)
        assert math.isfinite(geo._log_abs_sin(complex(0.3, 5000.0)))


class TestPosition:
    def test_mirror_positions_give_equal_factors(self):
        a = geo.rectangle_factor(20.0, 30.0, 1.0, centre=(4.0, 3.0), angle=0.3)
        assert geo.rectangle_factor(20.0, 30.0, 1.0, centre=(-4.0, 3.0), angle=math.pi - 0.3) == pytest.approx(a, rel=1e-9)
        assert geo.rectangle_factor(20.0, 30.0, 1.0, centre=(4.0, -3.0), angle=-0.3) == pytest.approx(a, rel=1e-9)
        c = geo.circle_factor(20.0, 1.0, centre=(6.0, 0.0), angle=0.0)
        assert geo.circle_factor(20.0, 1.0, centre=(0.0, 6.0), angle=math.pi / 2) == pytest.approx(c, rel=1e-12)

    def test_reversing_the_probe_changes_nothing(self):
        a = geo.circle_factor(20.0, 1.0, centre=(5.0, 2.0), angle=0.4)
        assert geo.circle_factor(20.0, 1.0, centre=(5.0, 2.0), angle=0.4 + math.pi) == pytest.approx(a, rel=1e-12)

    def test_an_edge_lowers_the_factor_and_more_so_when_parallel(self):
        # 20 s x 20 s square, probe 3 s from the lower edge.
        parallel = geo.rectangle_position_effect(20.0, 20.0, 1.0, centre=(0.0, -7.0), angle=0.0)
        assert parallel.factor_here < parallel.factor_centre
        assert parallel.relative_error == pytest.approx(0.0532, abs=5e-4)
        # Pointing at the edge, nearest tip 3 s from it.
        pointing = geo.rectangle_position_effect(20.0, 20.0, 1.0, centre=(0.0, -5.5), angle=math.pi / 2)
        assert 0 < pointing.relative_error < parallel.relative_error
        assert pointing.relative_error == pytest.approx(0.0141, abs=5e-4)

    def test_the_centre_has_no_position_effect(self):
        assert geo.circle_position_effect(20.0, 1.0, (0.0, 0.0)).relative_error == pytest.approx(0.0, abs=1e-12)

    def test_a_long_strip_far_from_its_ends_is_a_half_plane_near_one_edge(self):
        # One straight insulating edge, array parallel at distance d: the
        # single-image result is  ln 4 + ln((4 + 4u^2) / (1 + 4u^2)),  u = d/s,
        # in place of  ln 4.
        u = 2.0
        expected = 2 * math.pi / (math.log(4) + math.log((4 + 4 * u * u) / (1 + 4 * u * u)))
        factor = geo.rectangle_factor(width=4000.0, length=4000.0, spacing=1.0,
                                      centre=(0.0, -2000.0 + u), angle=0.0)
        assert factor == pytest.approx(expected, rel=1e-4)


class TestClearance:
    def test_clearance_is_in_units_of_the_spacing(self):
        assert geo.circle_edge_clearance(20.0, 1.0) == pytest.approx(8.5)
        assert geo.circle_edge_clearance(40.0, 2.0) == pytest.approx(8.5)
        assert geo.rectangle_edge_clearance(10.0, 30.0, 1.0) == pytest.approx(5.0)
        assert geo.rectangle_edge_clearance(10.0, 30.0, 1.0, angle=math.pi / 2) == pytest.approx(3.5)

    def test_a_tip_off_the_sample_is_refused(self):
        assert geo.circle_edge_clearance(20.0, 1.0, centre=(9.0, 0.0)) < 0
        with pytest.raises(ValueError, match="edge"):
            geo.circle_factor(20.0, 1.0, centre=(9.0, 0.0))
        with pytest.raises(ValueError, match="edge"):
            geo.rectangle_factor(10.0, 30.0, 1.0, centre=(0.0, 4.0), angle=math.pi / 2)

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
    def test_nonsense_dimensions_are_refused(self, bad):
        with pytest.raises(ValueError):
            geo.circle_factor(bad, 1.0)
        with pytest.raises(ValueError):
            geo.circle_factor(10.0, bad)
        with pytest.raises(ValueError):
            geo.rectangle_factor(bad, 10.0, 1.0)

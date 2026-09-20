"""Invariants of the 4PP and van der Pauw arithmetic.

``test_calculations.py`` and ``test_calculations_vdp.py`` check worked
examples and table rows. Here the inputs are drawn by hypothesis and the
assertions are the relations that hold for all of them: linearity in V and
1/I, agreement between the legacy K-factor path and the F84 decomposition
where the two describe the same sample, the van der Pauw equation itself as
an independent check on f(Q), and each function's documented behaviour on
NaN, infinity and zero.

"Any reading" below means zero, NaN, +/-inf, or a magnitude from 1e-15 to
1e38. The upper end is the 9.9e37 overflow sentinel a 2400 really does send;
magnitudes beyond about 1e150 are a separate, pinned case.

The run is derandomised and keeps no example database.
"""

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from resistamet_gui import calculations as calc
from resistamet_gui import calculations_geometry as geo
from resistamet_gui import calculations_vdp as vdp

# No deadline: nothing here is about speed, and shared CI runners make the
# per-example clock flaky. max_examples bounds the run.
PROPERTY = settings(max_examples=200, deadline=None, database=None, derandomize=True)

NAN = float("nan")
INF = float("inf")
MODELS = ("thin_film", "semi_infinite", "finite_thin", "something_else")
GEOMETRIES = calc._GEOMETRIES


def _magnitude(low_exp: float, high_exp: float):
    return st.floats(low_exp, high_exp).map(lambda e: 10.0 ** e)


def _signed(low_exp: float, high_exp: float):
    return st.builds(lambda m, neg: -m if neg else m, _magnitude(low_exp, high_exp), st.booleans())


#: What can arrive in a reading or a settings field.
any_reading = st.one_of(
    st.sampled_from([0.0, -0.0, NAN, INF, -INF, 9.9e37, -9.9e37, 9.91e37]),
    _signed(-15.0, 38.0),
)
volts = _signed(-9.0, 2.0)
amps = _signed(-9.0, 0.0)
positive = _magnitude(-6.0, 3.0)


class TestLegacyFourPointProbe:
    @PROPERTY
    @given(volts, amps, positive, positive, st.floats(0.5, 5.0), st.floats(0.1, 1.0),
           st.sampled_from(MODELS), _magnitude(-3.0, 3.0))
    def test_linear_in_v_and_inverse_in_i(self, v, i, s, t, k, alpha, model, scale):
        base = calc.calculate_four_point_probe(v, i, s, t, k, alpha, model)
        more_v = calc.calculate_four_point_probe(v * scale, i, s, t, k, alpha, model)
        more_i = calc.calculate_four_point_probe(v, i * scale, s, t, k, alpha, model)
        assert more_v.ratio == pytest.approx(base.ratio * scale, rel=1e-12)
        assert more_v.sheet_resistance == pytest.approx(base.sheet_resistance * scale, rel=1e-12)
        assert more_v.resistivity == pytest.approx(base.resistivity * scale, rel=1e-12)
        assert more_i.sheet_resistance == pytest.approx(base.sheet_resistance / scale, rel=1e-12)
        assert more_i.resistivity == pytest.approx(base.resistivity / scale, rel=1e-12)
        assert more_i.conductivity == pytest.approx(base.conductivity * scale, rel=1e-12)

    @PROPERTY
    @given(volts, amps, positive, positive, st.floats(0.5, 5.0), st.floats(0.1, 1.0), st.sampled_from(MODELS))
    def test_the_four_outputs_are_consistent(self, v, i, s, t, k, alpha, model):
        r = calc.calculate_four_point_probe(v, i, s, t, k, alpha, model)
        assert r.ratio == v / i
        assert r.resistivity * r.conductivity == pytest.approx(1.0, rel=1e-12)
        # Reversing both leads, or the current and the voltage together,
        # changes nothing; reversing one flips the sign.
        assert calc.calculate_four_point_probe(-v, -i, s, t, k, alpha, model) == r
        flipped = calc.calculate_four_point_probe(-v, i, s, t, k, alpha, model)
        assert flipped.sheet_resistance == -r.sheet_resistance
        if model in ("thin_film", "finite_thin"):
            # rho = Rs * t, with t given in micrometres.
            assert r.resistivity == pytest.approx(r.sheet_resistance * t * 1e-4, rel=1e-12)

    @PROPERTY
    @given(volts, amps, positive, positive, st.floats(0.5, 5.0), st.floats(0.1, 1.0))
    def test_alpha_belongs_to_the_thin_film_model_only(self, v, i, s, t, k, alpha):
        thin = calc.calculate_four_point_probe(v, i, s, t, k, alpha, "thin_film")
        finite = calc.calculate_four_point_probe(v, i, s, t, k, alpha, "finite_thin")
        assert thin.sheet_resistance == pytest.approx(finite.sheet_resistance * alpha, rel=1e-12)
        assert finite == calc.calculate_four_point_probe(v, i, s, t, k, 1.0, "finite_thin")
        bulk = calc.calculate_four_point_probe(v, i, s, t, k, alpha, "semi_infinite")
        assert bulk.resistivity == pytest.approx(2 * math.pi * s * v / i, rel=1e-12)


class TestLegacyAgainstF84:
    """Two code paths, one sample: they must give one number."""

    @PROPERTY
    @given(volts, amps, positive, st.floats(1e-3, 0.399), st.one_of(st.none(), _magnitude(0.0, 3.0)),
           st.sampled_from(GEOMETRIES))
    def test_thin_sample(self, v, i, s, w_over_s, d_over_s, geometry):
        """Below w/S = 0.4 F(w/S) is 1, so F84 is rho = R * F2 * w -- the
        legacy thin-film formula with K = F2."""
        thickness_um = w_over_s * s * 1e4
        diameter = None if d_over_s is None else d_over_s * s
        f84 = calc.calculate_four_point_probe_f84(v, i, s, thickness_um, diameter, geometry)
        assert f84.f_w_s == 1.0
        legacy = calc.calculate_four_point_probe(v, i, s, thickness_um, k_factor=f84.f2, model="finite_thin")
        assert f84.rho_T == pytest.approx(legacy.resistivity, rel=1e-12)
        if diameter is None:
            default = calc.calculate_four_point_probe(v, i, s, thickness_um, model="thin_film")
            assert f84.rho_T == pytest.approx(default.resistivity, rel=1e-12)

    @PROPERTY
    @given(volts, amps, positive, st.floats(10.0, 1000.0))
    def test_thick_sample_is_the_semi_infinite_formula(self, v, i, s, w_over_s):
        """F2 * w * F(w/S) tends to 2 pi s. The constants are printed to four
        figures (4.532, 1.3863), which is what the 0.1 % allows for."""
        thickness_um = w_over_s * s * 1e4
        f84 = calc.calculate_four_point_probe_f84(v, i, s, thickness_um)
        bulk = calc.calculate_four_point_probe(v, i, s, thickness_um, model="semi_infinite")
        assert f84.rho_T == pytest.approx(bulk.resistivity, rel=1e-3)


class TestF84Decomposition:
    @PROPERTY
    @given(_signed(-6.0, 6.0), positive, st.floats(1e-3, 50.0), st.one_of(st.none(), _magnitude(0.0, 3.0)),
           st.sampled_from(GEOMETRIES), st.floats(0.9, 1.1),
           st.one_of(st.none(), st.floats(18.0, 28.0)), st.sampled_from([None, "n", "p", "N-type"]))
    def test_result_is_the_product_of_its_factors(self, r, s, w_over_s, d_over_s, geometry, f_sp, temp, dopant):
        w = w_over_s * s
        d = None if d_over_s is None else d_over_s * s
        out = calc.calculate_resistivity_f84(r, s, w, d, f_sp, temp, dopant, geometry)
        assert out.f2 == calc.f2_finite_diameter(s, d, geometry)
        assert out.f_w_s == calc.f_thickness_correction(w, s)
        assert out.geometric_factor == pytest.approx(out.f2 * w * out.f_w_s * f_sp, rel=1e-12)
        assert out.rho_T == pytest.approx(r * out.geometric_factor, rel=1e-12)
        doubled = calc.calculate_resistivity_f84(2 * r, s, w, d, f_sp, temp, dopant, geometry)
        assert doubled.rho_T == pytest.approx(2 * out.rho_T, rel=1e-12)
        if temp is None or dopant is None:
            assert out.rho_23 is None and out.f_T is None
        elif out.rho_T > 0:
            assert out.rho_23 == pytest.approx(out.rho_T * out.f_T, rel=1e-12)
            # Table 5 never exceeds 0.009 / K; five kelvin is under 5 %.
            assert abs(out.f_T - 1.0) <= 0.009 * abs(temp - 23.0) + 1e-12
        else:
            # C_T is tabulated against a positive resistivity.
            assert out.rho_23 is None and math.isnan(out.f_T)

    @PROPERTY
    @given(positive, st.floats(0.4, 1000.0), st.floats(0.4, 1000.0), _magnitude(-3.0, 3.0))
    def test_thickness_correction(self, s, a, b, unit):
        thin, thick = sorted((a, b))
        f_thin = calc.f_thickness_correction(thin * s, s)
        f_thick = calc.f_thickness_correction(thick * s, s)
        # The series is cut off where a term drops under 1e-5 (the standard's
        # own criterion), and the split between exact and asymptotic terms
        # moves with int(2 S / w). Both leave steps of a few 1e-5, which is
        # the resolution of "falls with thickness"; "only w/S matters" holds
        # to the 4e-4 step where the w/S < 0.4 shortcut ends.
        assert 0.0 < f_thick <= 1.0 and 0.0 < f_thin <= 1.0
        assert f_thick <= f_thin + 1e-5
        if thick > 1.1 * thin:
            assert f_thick < f_thin
        assert calc.f_thickness_correction(thin * s * unit, s * unit) == pytest.approx(f_thin, abs=5e-4)

    def test_thickness_correction_is_continuous_where_the_shortcut_ends(self):
        # Below w/S = 0.4 the standard says "unity to four decimals".
        below = calc.f_thickness_correction(0.4 * (1 - 1e-12), 1.0)
        above = calc.f_thickness_correction(0.4, 1.0)
        assert below == 1.0
        assert 0.0 < below - above < 5e-4


class TestTabulatedLateralFactor:
    """``f2_finite_diameter``: what the interpolated tables promise."""

    @PROPERTY
    @given(positive, _magnitude(-1.0, 7.0), _magnitude(-1.0, 7.0), st.sampled_from(GEOMETRIES), _magnitude(-3.0, 3.0))
    def test_a_larger_sample_never_has_a_smaller_factor(self, s, a, b, geometry, unit):
        small, large = sorted((a, b))
        f_small = calc.f2_finite_diameter(s, small * s, geometry)
        f_large = calc.f2_finite_diameter(s, large * s, geometry)
        assert 0.9 < f_small <= f_large <= 4.5324
        assert calc.f2_finite_diameter(s * unit, small * s * unit, geometry) == pytest.approx(f_small, rel=1e-9)

    @PROPERTY
    @given(st.floats(10.0, 2000.0))
    def test_circle_is_the_closed_form(self, d_over_s):
        table = calc.f2_finite_diameter(1.0, d_over_s, "circle")
        # Table 3 is printed to three decimals and interpolated linearly.
        assert table == pytest.approx(geo.circle_factor(d_over_s, 1.0), abs=1e-3)

    @PROPERTY
    @given(st.floats(3.0, 40.0), st.sampled_from([("square", 1.0), ("rectangle_2", 2.0),
                                                  ("rectangle_3", 3.0), ("rectangle_4", 4.0)]))
    def test_rectangles_between_the_printed_rows(self, d_over_s, shape):
        """Straight lines between sparse rows of a concave curve read low, by
        up to 1.7 % (a square 3.4 spacings wide). Pinned so that it cannot
        get worse unnoticed; the closed form is the way to make it better."""
        geometry, ratio = shape
        if d_over_s * ratio <= 3.01:
            return
        table = calc.f2_finite_diameter(1.0, d_over_s, geometry)
        exact = geo.rectangle_factor(d_over_s, d_over_s * ratio, 1.0)
        assert -0.017 < table / exact - 1.0 < 1e-3

    @pytest.mark.xfail(strict=True, reason=(
        "_SMITS_GEOMETRY_CF jumps from D/s = 40 to a 1e9 sentinel row, and "
        "_linear_interp draws a straight line between them, so every square "
        "or rectangle wider than 40 spacings gets the D/s = 40 value: 4.5120 "
        "at D/s = 1000 where the series gives 4.5322 (0.45 % low), while "
        "diameter=None gives 4.532. calculations.py, f2_finite_diameter."))
    @pytest.mark.parametrize("d_over_s", [60.0, 100.0, 1000.0])
    def test_wide_rectangles_approach_the_unbounded_sheet(self, d_over_s):
        table = calc.f2_finite_diameter(1.0, d_over_s, "square")
        assert table == pytest.approx(geo.rectangle_factor(d_over_s, d_over_s, 1.0), rel=1e-3)


class TestCurrentFloor:
    @PROPERTY
    @given(_signed(-12.0, 1.0), _signed(-12.0, 1.0))
    def test_floor_is_a_range_fraction_and_grows_with_the_current(self, a, b):
        low, high = sorted((abs(a), abs(b)))
        allowed = {r * 1e-3 for r in calc._KEITHLEY_2400_CURRENT_RANGES}
        assert calc.estimate_current_floor(a) in allowed
        assert calc.estimate_current_floor(-a) == calc.estimate_current_floor(a)
        assert calc.estimate_current_floor(low) <= calc.estimate_current_floor(high)
        if abs(a) <= 1.0:
            assert calc.estimate_current_floor(a) >= abs(a) * 1e-3

    @PROPERTY
    @given(_signed(-3.0, 2.3), amps, amps, positive, positive, st.sampled_from(MODELS))
    def test_a_bound_is_never_above_what_the_measured_current_implies(self, v_comp, i_meas, i_src, s, t, model):
        bound = calc.calculate_four_point_probe_bound(v_comp, i_meas, i_src, s, t, model=model)
        naive = calc.calculate_four_point_probe(abs(v_comp), abs(i_meas), s, t, model=model)
        assert 0.0 < bound.ratio <= naive.ratio * (1 + 1e-12)
        assert bound.sheet_resistance <= naive.sheet_resistance * (1 + 1e-12)
        assert bound.conductivity >= naive.conductivity * (1 - 1e-12)


class TestUnusableInputsReturnNaN:
    """The documented contract of the 4PP functions is a NaN, not a raise."""

    @PROPERTY
    @given(any_reading, any_reading)
    def test_ratio(self, v, i):
        ratio = calc.calculate_ratio(v, i)
        if math.isfinite(v) and math.isfinite(i) and i != 0:
            assert ratio == v / i
        else:
            assert math.isnan(ratio)

    @PROPERTY
    @given(any_reading, any_reading, any_reading, any_reading, any_reading, any_reading, st.sampled_from(MODELS))
    def test_four_point_probe(self, v, i, s, t, k, alpha, model):
        r = calc.calculate_four_point_probe(v, i, s, t, k, alpha, model)
        usable = math.isfinite(v) and math.isfinite(i) and i != 0
        if not usable or not math.isfinite(v / i):
            assert all(math.isnan(x) for x in r)
        # Conductivity is NaN, never inf, when the resistivity is zero.
        assert not math.isinf(r.conductivity)
        assert calc.calculate_sheet_resistance(NAN, k, alpha, model) != calc.calculate_sheet_resistance(NAN, k, alpha, model)
        bound = calc.calculate_four_point_probe_bound(v, i, s, t, k, model=model)
        assert isinstance(bound, calc.FourPointProbeResult)

    @PROPERTY
    @given(any_reading)
    def test_conductivity(self, rho):
        sigma = calc.calculate_conductivity(rho)
        if math.isfinite(rho) and rho != 0:
            assert sigma == 1.0 / rho
        else:
            assert math.isnan(sigma)

    @PROPERTY
    @given(any_reading, any_reading)
    def test_thickness_correction(self, w, s):
        f = calc.f_thickness_correction(w, s)
        if math.isfinite(w) and math.isfinite(s) and w > 0 and s > 0:
            assert 0.0 < f <= 1.0
        else:
            assert math.isnan(f)

    @PROPERTY
    @given(any_reading, any_reading, st.sampled_from(GEOMETRIES + ("hexagon", " Circle ", "")))
    def test_lateral_factor(self, s, d, geometry):
        f2 = calc.f2_finite_diameter(s, d, geometry)
        known = geometry.lower().strip() in GEOMETRIES
        if not (math.isfinite(d) and d > 0):
            assert f2 == 4.532              # "effectively infinite"
        elif not (math.isfinite(s) and s > 0) or not known:
            assert math.isnan(f2)
        else:
            assert 0.9 < f2 <= 4.5324

    @PROPERTY
    @given(any_reading, any_reading, st.sampled_from(["n", "p", "N", " p-type ", "ntype", "x", ""]))
    def test_temperature_correction(self, rho, temp, dopant):
        f_t = calc.f_temperature_correction(rho, temp, dopant)
        known = dopant.lower().strip() in ("n", "n-type", "ntype", "p", "p-type", "ptype")
        if math.isfinite(rho) and rho > 0 and math.isfinite(temp) and known:
            assert math.isfinite(f_t)
        else:
            assert math.isnan(f_t)

    @PROPERTY
    @given(any_reading, any_reading, any_reading, st.one_of(st.none(), any_reading), any_reading,
           st.one_of(st.none(), any_reading), st.sampled_from([None, "n", "p", "x"]), st.sampled_from(GEOMETRIES))
    def test_f84(self, r, s, w, d, f_sp, temp, dopant, geometry):
        out = calc.calculate_resistivity_f84(r, s, w, d, f_sp, temp, dopant, geometry)
        if not (math.isfinite(r) and math.isfinite(s) and math.isfinite(w) and s > 0 and w > 0):
            assert math.isnan(out.rho_T) and math.isnan(out.geometric_factor)
            assert out.rho_23 is None and out.f_T is None
        if out.rho_23 is not None:
            assert math.isfinite(out.f_T)

    @pytest.mark.xfail(strict=True, reason=(
        "Finite, positive but absurd magnitudes escape the NaN contract as "
        "OverflowError: f_thickness_correction squares n*w/S with `**`, which "
        "raises where `*` would give inf (calculations.py, the sum1 loop)."))
    @pytest.mark.parametrize("w, s", [(1e200, 1.0), (1.0, 1e-200), (1e-100, 5e-324)])
    def test_absurd_but_finite_thickness_ratio_does_not_raise(self, w, s):
        value = calc.f_thickness_correction(w, s)
        assert math.isnan(value) or 0.0 <= value <= 1.0


def _readings(r1: float, r2: float, r3: float, r4: float, current: float, offsets=(0.0,) * 4):
    """The eight F76 voltages of a sample whose four geometries have the
    four-terminal resistances r1..r4, each with its own thermal offset."""
    out = {}
    for geometry, r, offset in zip(vdp.f76_geometries(), (r1, r2, r3, r4), offsets):
        out[geometry.label_pos] = current * r + offset
        out[geometry.label_neg] = -current * r + offset
    return out


def _a_q_is_zero(volts) -> bool:
    for first_pos, first_neg, second_pos, second_neg in (vdp._BASE_LABELS_A, vdp._BASE_LABELS_B):
        first = volts[first_pos] - volts[first_neg]
        second = volts[second_pos] - volts[second_neg]
        if second != 0.0 and abs(first / second) == 0.0:
            return True
    return False


resistance = _magnitude(-4.0, 4.0)
asymmetry = _magnitude(0.0, 3.0)
microvolts = st.floats(-1e-4, 1e-4)


class TestGeometricFactor:
    @PROPERTY
    @given(_magnitude(0.0, 4.0), _magnitude(0.0, 4.0))
    def test_f_is_in_the_unit_interval_and_falls_with_q(self, a, b):
        low, high = sorted((a, b))
        f_low, f_high = vdp.vdp_geometric_factor(low), vdp.vdp_geometric_factor(high)
        assert 0.0 < f_high <= f_low <= 1.0
        if high > 1.001 * low:
            assert f_high < f_low

    @PROPERTY
    @given(st.floats(0.0, 1e-3))
    def test_f_is_continuous_at_q_equals_one(self, excess):
        # f = 1 - (ln 2 / 2) * ((Q - 1) / (Q + 1))**2 - ...
        f = vdp.vdp_geometric_factor(1.0 + excess)
        assert f == pytest.approx(1.0 - 0.5 * math.log(2) * (excess / (2 + excess)) ** 2, abs=1e-9)

    @PROPERTY
    @given(resistance, asymmetry)
    def test_f_solves_the_van_der_pauw_equation(self, r_small, q):
        """exp(-pi R1 / Rs) + exp(-pi R2 / Rs) = 1 defines Rs. F76 writes the
        solution as Rs = (pi / ln 2) * f(Q) * (R1 + R2) / 2; substituting it
        back checks the bisection against the physics, not against itself."""
        r_large = r_small * q
        rs = math.pi / math.log(2) * vdp.vdp_geometric_factor(q) * (r_small + r_large) / 2.0
        total = math.exp(-math.pi * r_small / rs) + math.exp(-math.pi * r_large / rs)
        assert total == pytest.approx(1.0, abs=1e-9)

    @PROPERTY
    @given(any_reading)
    def test_anything_else_is_a_value_error(self, q):
        if math.isfinite(q) and q >= 1.0:
            assert 0.0 < vdp.vdp_geometric_factor(q) <= 1.0
        else:
            with pytest.raises(ValueError):
                vdp.vdp_geometric_factor(q)


class TestVanDerPauw:
    @PROPERTY
    @given(resistance, st.floats(0.5, 2.0), _magnitude(-6.0, -1.0), _magnitude(-5.0, 0.0),
           st.tuples(microvolts, microvolts, microvolts, microvolts))
    def test_symmetric_sample(self, r, b_over_a, current, thickness, offsets):
        """All four geometries alike within a group: Q = 1, f = 1 and
        Rs = (pi / ln 2) R, whatever the thermal offsets."""
        volts = _readings(r, r, r * b_over_a, r * b_over_a, current, offsets)
        out = vdp.calculate_van_der_pauw(volts, current, thickness)
        tolerance = 1e-9 + 4e-4 / (current * r) * 1e-12
        assert out.q_a == pytest.approx(1.0, rel=1e-6) and out.q_b == pytest.approx(1.0, rel=1e-6)
        assert out.f_a == pytest.approx(1.0, abs=1e-9) and out.f_b == pytest.approx(1.0, abs=1e-9)
        assert out.rho_a / thickness == pytest.approx(math.pi / math.log(2) * r, rel=1e-6 + tolerance)
        assert out.sheet_resistance == pytest.approx(out.rho_avg / thickness, rel=1e-12)

    @PROPERTY
    @given(resistance, asymmetry, _magnitude(-6.0, -1.0), _magnitude(-5.0, 0.0), _magnitude(-2.0, 2.0))
    def test_rs_is_linear_in_v_inverse_in_i_and_blind_to_thickness(self, r, q, current, thickness, scale):
        volts = _readings(r, r * q, r * q, r, current)
        base = vdp.calculate_van_der_pauw(volts, current, thickness)
        more_v = vdp.calculate_van_der_pauw({k: v * scale for k, v in volts.items()}, current, thickness)
        more_i = vdp.calculate_van_der_pauw(volts, current * scale, thickness)
        thicker = vdp.calculate_van_der_pauw(volts, current, thickness * scale)
        assert more_v.sheet_resistance == pytest.approx(base.sheet_resistance * scale, rel=1e-9)
        assert more_i.sheet_resistance == pytest.approx(base.sheet_resistance / scale, rel=1e-9)
        assert thicker.sheet_resistance == pytest.approx(base.sheet_resistance, rel=1e-9)
        assert thicker.rho_avg == pytest.approx(base.rho_avg * scale, rel=1e-9)
        for other in (more_v, more_i, thicker):
            assert other.q_a == pytest.approx(base.q_a, rel=1e-9)
            assert other.asymmetry_pct == pytest.approx(base.asymmetry_pct, abs=1e-6)

    @PROPERTY
    @given(resistance, asymmetry, asymmetry, _magnitude(-6.0, -1.0), _magnitude(-5.0, 0.0))
    def test_relabelling_the_contacts(self, r, q_a, q_b, current, thickness):
        volts = _readings(r, r * q_a, r * q_b, r, current)
        out = vdp.calculate_van_der_pauw(volts, current, thickness)
        assert out.q_a == pytest.approx(q_a, rel=1e-9) and out.q_b == pytest.approx(q_b, rel=1e-9)
        # Swapping the two geometries of a group inverts Q, which is
        # normalised back; swapping the groups swaps rho_A with rho_B.
        swapped = vdp.calculate_van_der_pauw(_readings(r * q_a, r, r, r * q_b, current), current, thickness)
        assert swapped.rho_a == pytest.approx(out.rho_a, rel=1e-9)
        assert swapped.rho_b == pytest.approx(out.rho_b, rel=1e-9)
        groups = vdp.calculate_van_der_pauw(_readings(r * q_b, r, r, r * q_a, current), current, thickness)
        assert groups.rho_a == pytest.approx(out.rho_b, rel=1e-9)
        assert groups.rho_avg == pytest.approx(out.rho_avg, rel=1e-9)
        assert groups.homogeneous == out.homogeneous

    @PROPERTY
    @given(resistance, asymmetry, asymmetry, _magnitude(-6.0, -1.0), _magnitude(-5.0, 0.0))
    def test_homogeneity_gate_and_reversed_leads(self, r, q_a, q_b, current, thickness):
        volts = _readings(r, r * q_a, r * q_b, r, current)
        out = vdp.calculate_van_der_pauw(volts, current, thickness)
        assert out.asymmetry_pct == pytest.approx(100 * abs(out.rho_a - out.rho_b) / out.rho_avg, rel=1e-12)
        assert out.homogeneous == (out.asymmetry_pct <= vdp.F76_HOMOGENEITY_TOLERANCE_PCT)
        # Sense leads the wrong way round: every reading changes sign. The
        # result is a negative resistivity that must not pass the gate.
        backwards = vdp.calculate_van_der_pauw({k: -v for k, v in volts.items()}, current, thickness)
        assert backwards.rho_avg == pytest.approx(-out.rho_avg, rel=1e-12)
        assert math.isnan(backwards.sheet_resistance) and not backwards.homogeneous

    @PROPERTY
    @given(st.dictionaries(st.sampled_from(vdp._REQUIRED_BASE_LABELS), any_reading, min_size=0, max_size=8),
           any_reading, any_reading)
    def test_only_the_documented_errors(self, volts, current, thickness):
        """KeyError for a missing label, ValueError for a current, thickness,
        reading or Q that cannot be used."""
        complete = len(volts) == 8
        try:
            out = vdp.calculate_van_der_pauw(volts, current, thickness)
        except KeyError:
            assert not complete
        except ValueError:
            assert complete
        else:
            assert complete
            if not out.rho_avg > 0:
                assert math.isnan(out.sheet_resistance) and not out.homogeneous

    @pytest.mark.parametrize("label, value", [("V_21,34", 0.0), ("V_21,34", 9.9e37), ("V_21,34", 1.25e-3),
                                              ("V_43,12", 0.0), ("V_32,41", INF)])
    def test_a_q_of_zero_is_a_value_error(self, label, value):
        volts = _readings(1.0, 1.0, 1.0, 1.0, 1e-3)
        partner = {"V_21,34": "V_12,34", "V_43,12": "V_34,12"}.get(label)
        volts[label] = value
        if partner:
            volts[partner] = value
        assert _a_q_is_zero(volts)
        with pytest.raises(ValueError):
            vdp.calculate_van_der_pauw(volts, 1e-3, 0.05)


class TestCombinedUncertainty:
    @PROPERTY
    @given(st.lists(st.tuples(_magnitude(-9.0, 2.0), _magnitude(-9.0, 0.0), st.sampled_from([1.0, -1.0])),
                    min_size=1, max_size=12),
           st.floats(0.5, 5.0),
           st.sampled_from(["2400", "2410", "2420", "2440"]), st.sampled_from([0.01, 0.1, 1.0, 10.0]),
           st.randoms(use_true_random=False))
    def test_four_point(self, rows, k, model, nplc, rnd):
        # A real resistance: V and I share a sign (either polarity), so the
        # derived values are positive and their mean cannot cancel to zero.
        v = [volt * sign for volt, _, sign in rows]
        i = [amp * sign for _, amp, sign in rows]
        values = [k * a / b for a, b in zip(v, i)]
        out = calc.four_point_combined_uncertainty(values, v, i, model, nplc)
        assert out.u_stat >= 0 and out.u_inst > 0
        assert out.u_total == pytest.approx(math.hypot(out.u_stat, out.u_inst), rel=1e-12)
        assert out.u_total >= max(out.u_stat, out.u_inst)
        n = len(values)
        mean = sum(values) / n
        assert out.mean == pytest.approx(mean, rel=1e-9, abs=1e-300)
        if n > 1:
            sd = math.sqrt(sum((x - mean) ** 2 for x in values) / (n - 1))
            assert out.u_stat == pytest.approx(sd / math.sqrt(n), rel=1e-9, abs=1e-300)
        else:
            assert out.u_stat == 0.0
        # Row order is not information, and rows that did not read are
        # ignored rather than poisoning the rest.
        order = list(range(n))
        rnd.shuffle(order)
        shuffled = calc.four_point_combined_uncertainty(
            [values[j] for j in order], [v[j] for j in order], [i[j] for j in order], model, nplc)
        assert shuffled.u_total == pytest.approx(out.u_total, rel=1e-9)
        padded = calc.four_point_combined_uncertainty(values + [NAN, INF], v + [NAN, 1.0], i + [1e-3, 0.0], model, nplc)
        assert padded.u_total == pytest.approx(out.u_total, rel=1e-9)
        # A slower integration never makes the instrument term worse.
        slow = calc.four_point_combined_uncertainty(values, v, i, model, 1.0)
        fast = calc.four_point_combined_uncertainty(values, v, i, model, 0.01)
        assert slow.u_inst <= fast.u_inst

    @PROPERTY
    @given(st.lists(any_reading, max_size=6), st.lists(any_reading, max_size=6), st.lists(any_reading, max_size=6))
    def test_four_point_is_nan_safe(self, values, v, i):
        out = calc.four_point_combined_uncertainty(values, v, i)
        if not any(math.isfinite(x) for x in values):
            assert out is None
        else:
            assert out.u_stat >= 0 and out.u_inst >= 0 and out.u_total >= 0

    @PROPERTY
    @given(resistance, asymmetry, _magnitude(-6.0, -1.0), _magnitude(-5.0, 0.0),
           st.sampled_from(["2400", "2410", "2420", "2440"]))
    def test_van_der_pauw(self, r, q, current, thickness, model):
        volts = _readings(r, r * q, r * q, r, current)
        result = vdp.calculate_van_der_pauw(volts, current, thickness)
        out = vdp.vdp_combined_uncertainty(volts, current, result.sheet_resistance, result.rho_avg, model)
        assert out.u_inst_R > 0 and out.u_stat_R >= 0
        assert out.u_total_R == pytest.approx(math.hypot(out.u_inst_R, out.u_stat_R), rel=1e-12)
        # The same relative uncertainty lands on Rs and on rho.
        assert out.u_rs / result.sheet_resistance == pytest.approx(out.u_rho / result.rho_avg, rel=1e-9)
        if q == 1.0:
            assert out.u_stat_R == pytest.approx(0.0, abs=1e-12 * r)
        # The sign of the current is a convention.
        mirrored = vdp.vdp_combined_uncertainty(volts, -current, result.sheet_resistance, result.rho_avg, model)
        assert mirrored.u_rs == pytest.approx(out.u_rs, rel=1e-9)

    @PROPERTY
    @given(st.one_of(st.none(), st.dictionaries(st.sampled_from(vdp._REQUIRED_BASE_LABELS),
                                               st.one_of(st.none(), any_reading, st.just("x")), max_size=8)),
           any_reading, any_reading, any_reading)
    def test_van_der_pauw_never_raises(self, volts, current, rs, rho):
        out = vdp.vdp_combined_uncertainty(volts, current, rs, rho)
        assert isinstance(out, vdp.VdpCombinedUncertainty)
        usable = (isinstance(volts, dict) and len(volts) == 8
                  and all(isinstance(x, float) and math.isfinite(x) for x in volts.values())
                  and math.isfinite(current) and current != 0)
        if not usable:
            assert math.isnan(out.u_rs) and math.isnan(out.u_rho)
        for x in out:
            assert math.isnan(x) or x >= 0

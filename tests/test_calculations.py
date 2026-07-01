"""
Unit tests for the calculations module.

Tests cover:
- V/I ratio calculation
- Sheet resistance calculation for different models
- Resistivity calculation for different models
- Conductivity calculation
- Edge cases (zero current, NaN values, etc.)
"""

import math
import numpy as np
import pytest

from resistamet_gui.calculations import (
    calculate_ratio,
    calculate_sheet_resistance,
    calculate_resistivity,
    calculate_conductivity,
    calculate_four_point_probe,
    calculate_four_point_probe_bound,
    estimate_current_floor,
    f2_finite_diameter,
    f_thickness_correction,
    f_temperature_correction,
    calculate_resistivity_f84,
    select_four_point_current,
    CurrentSelection,
    FourPointProbeResult,
    F84ResistivityResult,
    DEFAULT_K_FACTOR,
)


class TestCalculateRatio:
    """Tests for the V/I ratio calculation."""

    def test_basic_ratio(self):
        """Test basic V/I ratio calculation."""
        result = calculate_ratio(voltage=0.001, current=0.001)
        assert result == pytest.approx(1.0)

    def test_different_values(self):
        """Test ratio with different voltage and current."""
        result = calculate_ratio(voltage=0.005, current=0.001)
        assert result == pytest.approx(5.0)

    def test_small_values(self):
        """Test ratio with small values typical of 4PP measurements."""
        result = calculate_ratio(voltage=1e-6, current=1e-3)
        assert result == pytest.approx(1e-3)

    def test_zero_current_returns_nan(self):
        """Test that zero current returns NaN."""
        result = calculate_ratio(voltage=0.001, current=0.0)
        assert math.isnan(result)

    def test_nan_voltage_returns_nan(self):
        """Test that NaN voltage returns NaN."""
        result = calculate_ratio(voltage=float('nan'), current=0.001)
        assert math.isnan(result)

    def test_nan_current_returns_nan(self):
        """Test that NaN current returns NaN."""
        result = calculate_ratio(voltage=0.001, current=float('nan'))
        assert math.isnan(result)

    def test_inf_returns_nan(self):
        """Test that infinite values return NaN."""
        result = calculate_ratio(voltage=float('inf'), current=0.001)
        assert math.isnan(result)

    def test_negative_values(self):
        """Test ratio with negative values (polarity reversal)."""
        result = calculate_ratio(voltage=-0.001, current=-0.001)
        assert result == pytest.approx(1.0)


class TestCalculateSheetResistance:
    """Tests for sheet resistance calculation."""

    def test_thin_film_default_k(self):
        """Test thin film model with default K factor."""
        result = calculate_sheet_resistance(ratio=1.0, model='thin_film')
        assert result == pytest.approx(DEFAULT_K_FACTOR)

    def test_thin_film_custom_k(self):
        """Test thin film model with custom K factor."""
        result = calculate_sheet_resistance(ratio=1.0, k_factor=4.0, model='thin_film')
        assert result == pytest.approx(4.0)

    def test_thin_film_with_alpha(self):
        """Test thin film model with alpha correction."""
        result = calculate_sheet_resistance(
            ratio=1.0, k_factor=4.532, alpha=0.9, model='thin_film'
        )
        expected = 4.532 * 0.9  # K * alpha * ratio
        assert result == pytest.approx(expected)

    def test_thin_film_alpha_one_no_correction(self):
        """Test that alpha=1.0 doesn't apply correction."""
        result = calculate_sheet_resistance(
            ratio=1.0, k_factor=4.532, alpha=1.0, model='thin_film'
        )
        assert result == pytest.approx(4.532)

    def test_semi_infinite_no_alpha(self):
        """Test semi-infinite model ignores alpha."""
        result = calculate_sheet_resistance(
            ratio=1.0, k_factor=4.532, alpha=0.5, model='semi_infinite'
        )
        # Alpha should not be applied for semi_infinite
        assert result == pytest.approx(4.532)

    def test_nan_ratio_returns_nan(self):
        """Test that NaN ratio returns NaN."""
        result = calculate_sheet_resistance(ratio=float('nan'))
        assert math.isnan(result)


class TestCalculateResistivity:
    """Tests for resistivity calculation."""

    def test_semi_infinite_model(self):
        """Test semi-infinite model: rho = 2*pi*s * ratio."""
        spacing = 0.1  # cm
        ratio = 100.0  # Ohms
        result = calculate_resistivity(
            ratio=ratio, spacing_cm=spacing, thickness_cm=0.01,
            model='semi_infinite'
        )
        expected = 2 * np.pi * spacing * ratio
        assert result == pytest.approx(expected)

    def test_thin_film_model(self):
        """Test thin film model: rho = K * t * ratio."""
        spacing = 0.1  # cm
        thickness = 0.001  # cm (10 um)
        ratio = 100.0  # Ohms
        k_factor = 4.532
        result = calculate_resistivity(
            ratio=ratio, spacing_cm=spacing, thickness_cm=thickness,
            k_factor=k_factor, model='thin_film'
        )
        expected = k_factor * thickness * ratio
        assert result == pytest.approx(expected)

    def test_thin_film_with_alpha(self):
        """Test thin film model with alpha correction."""
        spacing = 0.1
        thickness = 0.001
        ratio = 100.0
        k_factor = 4.532
        alpha = 0.9
        result = calculate_resistivity(
            ratio=ratio, spacing_cm=spacing, thickness_cm=thickness,
            k_factor=k_factor, alpha=alpha, model='thin_film'
        )
        expected = k_factor * alpha * thickness * ratio
        assert result == pytest.approx(expected)

    def test_finite_thin_no_alpha(self):
        """Test finite_thin model doesn't apply alpha."""
        spacing = 0.1
        thickness = 0.001
        ratio = 100.0
        k_factor = 4.532
        alpha = 0.9  # Should be ignored
        result = calculate_resistivity(
            ratio=ratio, spacing_cm=spacing, thickness_cm=thickness,
            k_factor=k_factor, alpha=alpha, model='finite_thin'
        )
        expected = k_factor * thickness * ratio  # No alpha
        assert result == pytest.approx(expected)

    def test_nan_ratio_returns_nan(self):
        """Test that NaN ratio returns NaN resistivity."""
        result = calculate_resistivity(
            ratio=float('nan'), spacing_cm=0.1, thickness_cm=0.001
        )
        assert math.isnan(result)


class TestCalculateConductivity:
    """Tests for conductivity calculation."""

    def test_basic_conductivity(self):
        """Test basic conductivity = 1/resistivity."""
        result = calculate_conductivity(resistivity=100.0)
        assert result == pytest.approx(0.01)

    def test_high_resistivity(self):
        """Test conductivity with high resistivity."""
        result = calculate_conductivity(resistivity=1e6)
        assert result == pytest.approx(1e-6)

    def test_zero_resistivity_returns_nan(self):
        """Test that zero resistivity returns NaN."""
        result = calculate_conductivity(resistivity=0.0)
        assert math.isnan(result)

    def test_nan_resistivity_returns_nan(self):
        """Test that NaN resistivity returns NaN."""
        result = calculate_conductivity(resistivity=float('nan'))
        assert math.isnan(result)

    def test_inf_resistivity_returns_nan(self):
        """Test that infinite resistivity returns NaN."""
        result = calculate_conductivity(resistivity=float('inf'))
        assert math.isnan(result)


class TestCalculateFourPointProbe:
    """Tests for the complete 4PP calculation."""

    def test_returns_named_tuple(self):
        """Test that result is a FourPointProbeResult."""
        result = calculate_four_point_probe(
            voltage=0.001, current=0.001,
            spacing_cm=0.1016, thickness_um=100
        )
        assert isinstance(result, FourPointProbeResult)

    def test_all_fields_present(self):
        """Test that all result fields are populated."""
        result = calculate_four_point_probe(
            voltage=0.001, current=0.001,
            spacing_cm=0.1016, thickness_um=100
        )
        assert hasattr(result, 'ratio')
        assert hasattr(result, 'sheet_resistance')
        assert hasattr(result, 'resistivity')
        assert hasattr(result, 'conductivity')

    def test_thin_film_calculation(self):
        """Test complete thin film calculation."""
        voltage = 0.001  # 1 mV
        current = 0.001  # 1 mA
        spacing = 0.1016  # cm
        thickness = 100  # um
        k_factor = 4.532

        result = calculate_four_point_probe(
            voltage=voltage, current=current,
            spacing_cm=spacing, thickness_um=thickness,
            k_factor=k_factor, model='thin_film'
        )

        # Verify ratio
        expected_ratio = voltage / current  # 1.0 Ohm
        assert result.ratio == pytest.approx(expected_ratio)

        # Verify sheet resistance
        expected_rs = k_factor * expected_ratio  # ~4.532 Ohms/sq
        assert result.sheet_resistance == pytest.approx(expected_rs)

        # Verify resistivity
        thickness_cm = thickness * 1e-4  # 0.01 cm
        expected_rho = k_factor * thickness_cm * expected_ratio
        assert result.resistivity == pytest.approx(expected_rho)

        # Verify conductivity
        expected_sigma = 1.0 / expected_rho
        assert result.conductivity == pytest.approx(expected_sigma)

    def test_zero_current_returns_all_nan(self):
        """Test that zero current results in all NaN values."""
        result = calculate_four_point_probe(
            voltage=0.001, current=0.0,
            spacing_cm=0.1016, thickness_um=100
        )
        assert math.isnan(result.ratio)
        assert math.isnan(result.sheet_resistance)
        assert math.isnan(result.resistivity)
        assert math.isnan(result.conductivity)

    def test_thickness_unit_conversion(self):
        """Test that thickness is correctly converted from um to cm."""
        # 100 um = 0.01 cm
        result = calculate_four_point_probe(
            voltage=0.001, current=0.001,
            spacing_cm=0.1, thickness_um=100,
            k_factor=1.0, model='thin_film'
        )

        # With ratio=1, k=1: rho = k * t_cm * ratio = 1 * 0.01 * 1 = 0.01
        assert result.resistivity == pytest.approx(0.01)


class TestEstimateCurrentFloor:
    """Tests for the 2400-series current measurement floor estimate."""

    def test_floor_for_1ma_source(self):
        # 1 mA source → 1 mA range → floor ~ 0.1% FS = 1 µA
        assert estimate_current_floor(1e-3) == pytest.approx(1e-6)

    def test_floor_for_100na_source(self):
        # 100 nA fits the 1 µA range → floor ~ 1 nA
        assert estimate_current_floor(1e-7) == pytest.approx(1e-9)

    def test_floor_picks_smallest_containing_range(self):
        # 5 µA fits the 10 µA range → floor ~ 10 nA
        assert estimate_current_floor(5e-6) == pytest.approx(1e-8)

    def test_floor_handles_negative_source(self):
        # Sign should not change the floor
        assert estimate_current_floor(-1e-3) == pytest.approx(1e-6)

    def test_floor_handles_zero(self):
        # Zero source → fall back to top range
        assert estimate_current_floor(0.0) == pytest.approx(1e-3)


class TestCalculateFourPointProbeBound:
    """Tests for the compliance-bound 4PP calculation."""

    def test_bound_uses_floor_when_measured_below_it(self):
        # Source 1 mA, V_comp 5 V, measured 100 nA (below 1 µA floor)
        # Effective I = 1 µA, R_min = 5 V / 1 µA = 5e6 Ω
        result = calculate_four_point_probe_bound(
            v_compliance=5.0, measured_current=1e-7,
            source_current=1e-3, spacing_cm=0.1016,
            thickness_um=100, model='thin_film'
        )
        assert result.ratio == pytest.approx(5e6)

    def test_bound_uses_measured_when_above_floor(self):
        # Measured 10 µA on 1 mA range (floor = 1 µA) → use 10 µA
        # R_min = 5 V / 10 µA = 5e5 Ω
        result = calculate_four_point_probe_bound(
            v_compliance=5.0, measured_current=1e-5,
            source_current=1e-3, spacing_cm=0.1016,
            thickness_um=100, model='thin_film'
        )
        assert result.ratio == pytest.approx(5e5)

    def test_lowering_source_current_tightens_bound(self):
        # Same V_comp, same insulator: lower source current → tighter (larger) R_min
        loose = calculate_four_point_probe_bound(
            v_compliance=5.0, measured_current=0.0,
            source_current=1e-3, spacing_cm=0.1, thickness_um=100,
        )
        tight = calculate_four_point_probe_bound(
            v_compliance=5.0, measured_current=0.0,
            source_current=1e-7, spacing_cm=0.1, thickness_um=100,
        )
        assert tight.ratio > loose.ratio
        assert tight.conductivity < loose.conductivity

    def test_bound_handles_nan_measurement(self):
        # NaN measured current → fall back to floor
        result = calculate_four_point_probe_bound(
            v_compliance=5.0, measured_current=float('nan'),
            source_current=1e-3, spacing_cm=0.1, thickness_um=100,
        )
        assert np.isfinite(result.ratio)
        assert result.ratio == pytest.approx(5e6)

    def test_negative_v_comp_uses_magnitude(self):
        a = calculate_four_point_probe_bound(
            v_compliance=5.0, measured_current=0.0,
            source_current=1e-3, spacing_cm=0.1, thickness_um=100,
        )
        b = calculate_four_point_probe_bound(
            v_compliance=-5.0, measured_current=0.0,
            source_current=1e-3, spacing_cm=0.1, thickness_um=100,
        )
        assert a.ratio == pytest.approx(b.ratio)

    def test_bound_returns_named_tuple(self):
        result = calculate_four_point_probe_bound(
            v_compliance=5.0, measured_current=0.0,
            source_current=1e-3, spacing_cm=0.1, thickness_um=100,
        )
        assert isinstance(result, FourPointProbeResult)


# ============================================================================
# ASTM F84-02 correction-factor tests
# ----------------------------------------------------------------------------
# These verify the F84 primitives against the standard's own tabulated values.
# Tolerances are tight (1e-3 absolute) because the reference values are the
# tables the standard requires implementations to reproduce.
# ============================================================================

class TestF2FiniteDiameter:
    """F84 Table 3: F2 as a function of S/D."""

    def test_infinite_diameter_returns_smits_value(self):
        # S/D = 0 -> F2 = pi/ln(2) = 4.5324, tabulated as 4.532.
        assert f2_finite_diameter(spacing_cm=0.1, diameter_cm=None) == pytest.approx(4.532)
        assert f2_finite_diameter(spacing_cm=0.1, diameter_cm=0) == pytest.approx(4.532)

    @pytest.mark.parametrize("s_over_d,expected", [
        (0.000, 4.532),
        (0.020, 4.517),
        (0.050, 4.436),
        (0.070, 4.348),
        (0.100, 4.171),
    ])
    def test_against_table_3(self, s_over_d, expected):
        # Pick any S and back out D so S/D matches.
        s = 0.1
        d = s / s_over_d if s_over_d > 0 else 1e9
        assert f2_finite_diameter(s, d) == pytest.approx(expected, abs=1e-3)

    def test_interpolates_between_rows(self):
        # S/D = 0.0125 should be between 0.010 (4.528) and 0.015 (4.524).
        s, d = 0.1, 0.1 / 0.0125
        result = f2_finite_diameter(s, d)
        assert 4.524 < result < 4.528

    def test_clamps_above_tabulated_range(self):
        # F84 Table 3 stops at S/D = 0.10; clamp rather than extrapolate.
        s, d = 0.1, 0.1 / 0.15  # S/D = 0.15
        assert f2_finite_diameter(s, d) == pytest.approx(4.171, abs=1e-3)


class TestGeometryCorrectionNonCircular:
    """Smits 1958 / Adamson lab table: square + rectangular geometries.

    F84 only tabulates circular wafers. Most non-Si materials labs measure
    on cut squares or rectangles, so the broader Smits table matters. These
    tests pin values directly from the Adamson group 4PP manual.
    """

    @pytest.mark.parametrize("d_over_s,expected", [
        (3.0, 2.4575), (4.0, 3.1127), (5.0, 3.5098), (7.5, 4.0095),
        (10.0, 4.2209), (15.0, 4.3882), (20.0, 4.4516),
        (32.0, 4.4878), (40.0, 4.5120),
    ])
    def test_square_against_table(self, d_over_s, expected):
        s = 0.1
        d = d_over_s * s
        assert f2_finite_diameter(s, d, geometry='square') == \
               pytest.approx(expected, abs=5e-4)

    @pytest.mark.parametrize("d_over_s,expected", [
        (1.5, 1.4788), (2.0, 1.9475), (3.0, 2.7000),
        (5.0, 3.5749), (10.0, 4.2357), (40.0, 4.5129),
    ])
    def test_rectangle_2_against_table(self, d_over_s, expected):
        s = 0.1
        d = d_over_s * s
        assert f2_finite_diameter(s, d, geometry='rectangle_2') == \
               pytest.approx(expected, abs=5e-4)

    @pytest.mark.parametrize("d_over_s,expected", [
        (1.0, 0.9988), (1.5, 1.4893), (3.0, 2.7005),
        (10.0, 4.2357), (40.0, 4.5129),
    ])
    def test_rectangle_3_against_table(self, d_over_s, expected):
        s = 0.1
        d = d_over_s * s
        assert f2_finite_diameter(s, d, geometry='rectangle_3') == \
               pytest.approx(expected, abs=5e-4)

    @pytest.mark.parametrize("d_over_s,expected", [
        (1.0, 0.9994), (1.5, 1.4893), (3.0, 2.7005),
        (10.0, 4.2357), (40.0, 4.5129),
    ])
    def test_rectangle_4_against_table(self, d_over_s, expected):
        s = 0.1
        d = d_over_s * s
        assert f2_finite_diameter(s, d, geometry='rectangle_4') == \
               pytest.approx(expected, abs=5e-4)

    def test_infinite_sample_all_geometries_agree(self):
        # In the infinite-sample limit every geometry → 4.5324 (= pi/ln(2)).
        # F84 Table 3 rounds this to 4.532; we accept either rounding.
        for geom in ('circle', 'square', 'rectangle_2',
                     'rectangle_3', 'rectangle_4'):
            assert f2_finite_diameter(0.1, None, geom) == \
                   pytest.approx(4.5324, abs=5e-4)

    def test_default_geometry_is_circle(self):
        # Backward compat: no geometry kwarg must match circle.
        s, d = 0.1, 0.1 / 0.05  # S/D = 0.05
        assert f2_finite_diameter(s, d) == \
               f2_finite_diameter(s, d, geometry='circle')

    def test_unknown_geometry_returns_nan(self):
        assert math.isnan(f2_finite_diameter(0.1, 1.0, geometry='hexagon'))

    def test_square_below_lowest_clamps(self):
        # Square table starts at D/s = 3. Below that, clamp not extrapolate.
        s = 0.1
        result = f2_finite_diameter(s, s * 2.0, geometry='square')
        assert result == pytest.approx(2.4575, abs=5e-4)  # value at D/s=3

    def test_geometry_matters_at_finite_size(self):
        # At D/s = 10 a square sample's CF (4.2209) should differ from
        # circle (4.1716) — this is the whole reason geometry parameter exists.
        s = 0.1
        d = 10 * s
        cf_circle = f2_finite_diameter(s, d, geometry='circle')
        cf_square = f2_finite_diameter(s, d, geometry='square')
        assert cf_square > cf_circle
        assert cf_square - cf_circle == pytest.approx(0.0493, abs=5e-3)


class TestThicknessCorrection:
    """F84 Table 4 / Appendix X1: F(w/S)."""

    def test_thin_sample_is_unity(self):
        # F84 §X1.3: for w/S < 0.4, F(w/S) = 1.000.
        assert f_thickness_correction(thickness_cm=0.01, spacing_cm=0.1016) == 1.000
        # w/S = 0.39
        assert f_thickness_correction(thickness_cm=0.039, spacing_cm=0.1) == 1.000

    @pytest.mark.parametrize("w_over_s,expected", [
        (0.5, 0.997),
        (0.6, 0.992),
        (0.7, 0.982),
        (0.8, 0.966),
        (0.9, 0.944),
        (1.0, 0.921),
    ])
    def test_against_table_4(self, w_over_s, expected):
        s = 0.1
        w = w_over_s * s
        # 5e-3 tolerance: Appendix X1 notes the table itself is interpolated
        # between Smits 1958 values to <= 2 parts in 1e4, and the X1.1 closed
        # form agrees with the table to 6 parts in 1e4 except at w/S = 0.9
        # and 1.0 where the table is +2e-4 off the closed form.
        assert f_thickness_correction(w, s) == pytest.approx(expected, abs=5e-3)

    def test_invalid_inputs_return_nan(self):
        import math
        assert math.isnan(f_thickness_correction(0, 0.1))
        assert math.isnan(f_thickness_correction(-0.01, 0.1))
        assert math.isnan(f_thickness_correction(0.01, 0))
        assert math.isnan(f_thickness_correction(float('nan'), 0.1))


class TestTemperatureCorrection:
    """F84 Table 5: F_T = 1 - C_T(T - 23) for silicon."""

    def test_at_23c_returns_unity(self):
        # No correction at the reference temperature, regardless of rho/dopant.
        assert f_temperature_correction(1.0, 23.0, 'n') == pytest.approx(1.0)
        assert f_temperature_correction(100.0, 23.0, 'p') == pytest.approx(1.0)

    @pytest.mark.parametrize("rho,dopant,expected_ct", [
        # Table 5 spot checks (rho, dopant, expected C_T)
        (0.030, 'n', 0.00139),
        (0.030, 'p', 0.00102),
        (1.0, 'n', 0.00736),
        (1.0, 'p', 0.00707),
        (10.0, 'n', 0.00813),
        (10.0, 'p', 0.00825),
        (100.0, 'p', 0.00876),
    ])
    def test_ct_lookup_against_table_5(self, rho, dopant, expected_ct):
        # F_T = 1 - C_T(T - 23). At T = 24, F_T - 1 = -C_T.
        f_t = f_temperature_correction(rho, 24.0, dopant)
        assert (1.0 - f_t) == pytest.approx(expected_ct, abs=1e-5)

    def test_dopant_case_insensitive(self):
        assert f_temperature_correction(1.0, 25.0, 'N') == \
               f_temperature_correction(1.0, 25.0, 'n')
        assert f_temperature_correction(1.0, 25.0, 'P-Type') == \
               f_temperature_correction(1.0, 25.0, 'p')

    def test_invalid_dopant_returns_nan(self):
        import math
        assert math.isnan(f_temperature_correction(1.0, 25.0, 'germanium'))


class TestResistivityF84:
    """Integration: ρ(T) = R · F2 · w · F(w/S) · F_sp, ρ(23) = ρ(T) · F_T."""

    def test_thin_sample_matches_smits_formula(self):
        # w/S << 0.4: F(w/S) = 1, F2 ~ 4.532, so rho(T) = 4.532 * w * R.
        R = 50.0  # Ohms
        s = 0.1016  # cm (40 mil)
        w = 100e-4  # 100 um = 0.01 cm; w/S = 0.0984 << 0.4
        result = calculate_resistivity_f84(R, s, w)
        expected = 4.532 * w * R
        assert result.rho_T == pytest.approx(expected, rel=1e-3)
        assert result.rho_23 is None  # no T or dopant supplied
        assert result.f_w_s == 1.000

    def test_f_sp_is_applied(self):
        baseline = calculate_resistivity_f84(50.0, 0.1, 0.001)
        with_fsp = calculate_resistivity_f84(50.0, 0.1, 0.001, f_sp=1.05)
        assert with_fsp.rho_T == pytest.approx(baseline.rho_T * 1.05)

    def test_diameter_applies_f2(self):
        # S/D = 0.05 -> F2 = 4.436 instead of 4.532.
        s, w = 0.1, 0.001  # cm; w/S = 0.01 < 0.4 so F(w/S) = 1
        result = calculate_resistivity_f84(50.0, s, w, diameter_cm=s / 0.05)
        expected = 4.436 * w * 50.0
        assert result.rho_T == pytest.approx(expected, rel=1e-3)
        assert result.f2 == pytest.approx(4.436, abs=1e-3)

    def test_temperature_correction_applied(self):
        # rho ~ 1 Ω·cm n-type at 25 C: C_T = 0.00736.
        # F_T = 1 - 0.00736 * (25 - 23) = 0.98528.
        R = 1.0 / (4.532 * 0.001)  # makes rho(T) ~ 1
        result = calculate_resistivity_f84(
            R, spacing_cm=0.1, thickness_cm=0.001,
            temperature_c=25.0, dopant_type='n',
        )
        assert result.rho_23 is not None
        assert result.f_T == pytest.approx(0.98528, abs=1e-4)
        assert result.rho_23 == pytest.approx(result.rho_T * 0.98528, rel=1e-3)

    def test_invalid_inputs_propagate_nan(self):
        result = calculate_resistivity_f84(float('nan'), 0.1, 0.001)
        assert math.isnan(result.rho_T)
        assert math.isnan(result.f2)
        assert result.rho_23 is None

    def test_returns_named_tuple(self):
        result = calculate_resistivity_f84(50.0, 0.1, 0.001)
        assert isinstance(result, F84ResistivityResult)


class TestSelectFourPointCurrent:
    """Pure current-planner for the 4PP pre-run current finder (sig-fig based)."""

    # A flat, empirical-style noise floor (delta repeatability), in volts.
    FLOOR = 1e-6
    TARGET_SNR = 1e4   # 4 valid significant figures

    def _flat_floor(self, _v):
        return self.FLOOR

    def test_measurable_sample_reaches_target_sig_figs(self):
        # R = 100 Ω, 4 figs (SNR 1e4) -> V 10 mV -> 0.1 mA, within a 20 mA ceiling.
        sel = select_four_point_current(
            100.0, self.TARGET_SNR, max_current=0.02,
            sigma_v=self._flat_floor, compliance_v=5.0,
        )
        assert sel.verdict == 'ok'
        assert sel.current == pytest.approx(1e-4, rel=1e-6)
        assert sel.expected_voltage == pytest.approx(0.01, rel=1e-6)
        assert sel.snr == pytest.approx(1e4, rel=1e-3)
        assert sel.sig_figs == pytest.approx(4.0, abs=0.05)

    def test_picks_minimum_current_for_target(self):
        # Gentlest current that reaches the target — not the max available.
        sel = select_four_point_current(
            100.0, self.TARGET_SNR, max_current=1.0,   # huge ceiling
            sigma_v=self._flat_floor, compliance_v=50.0,
        )
        assert sel.current == pytest.approx(1e-4, rel=1e-6)  # still just 0.1 mA

    def test_sign_of_resistance_is_ignored(self):
        pos = select_four_point_current(100.0, self.TARGET_SNR, 0.02, self._flat_floor, compliance_v=5.0)
        neg = select_four_point_current(-100.0, self.TARGET_SNR, 0.02, self._flat_floor, compliance_v=5.0)
        assert neg.verdict == 'ok'
        assert neg.current == pytest.approx(pos.current, rel=1e-9)

    def test_too_conductive_when_current_ceiling_too_low(self):
        # R = 15 µΩ; at a 20 mA ceiling V is sub-µV -> under ~1 valid fig.
        sel = select_four_point_current(
            15e-6, self.TARGET_SNR, max_current=0.02,
            sigma_v=self._flat_floor, compliance_v=5.0, min_snr=10.0,
        )
        assert sel.verdict == 'too_conductive'
        assert sel.current == pytest.approx(0.02, rel=1e-6)  # clamped to the ceiling
        assert sel.snr < 10.0
        assert sel.sig_figs < 1.0

    def test_conductive_sample_usable_with_more_current_headroom(self):
        # Same 15 µΩ copper becomes usable (a fig or two) once the ceiling is 1 A.
        sel = select_four_point_current(
            15e-6, self.TARGET_SNR, max_current=1.0,
            sigma_v=self._flat_floor, compliance_v=5.0, min_snr=10.0,
        )
        assert sel.verdict == 'ok'
        assert sel.snr >= 10.0
        assert sel.sig_figs >= 1.0
        # ceiling-limited, so it does NOT reach the 4-fig target.
        assert sel.snr < self.TARGET_SNR
        assert sel.expected_voltage == pytest.approx(15e-6 * sel.current, rel=1e-6)

    def test_too_resistive_when_min_current_exceeds_compliance(self):
        sel = select_four_point_current(
            1e9, self.TARGET_SNR, max_current=0.02,
            sigma_v=self._flat_floor, compliance_v=5.0, min_current=1e-6,
        )
        assert sel.verdict == 'too_resistive'

    def test_zero_resistance_is_too_conductive(self):
        sel = select_four_point_current(0.0, self.TARGET_SNR, 0.02, self._flat_floor, compliance_v=5.0)
        assert sel.verdict == 'too_conductive'
        sel_nan = select_four_point_current(float('nan'), self.TARGET_SNR, 0.02, self._flat_floor)
        assert sel_nan.verdict == 'too_conductive'

    def test_chosen_current_respects_compliance_headroom(self):
        # Demand so many figs that the voltage would blow past compliance;
        # the chosen current must keep V under the headroom.
        sel = select_four_point_current(
            1e5, target_snr=1e8, max_current=1.0,
            sigma_v=self._flat_floor, compliance_v=5.0, compliance_headroom=0.9,
        )
        assert sel.expected_voltage <= 0.9 * 5.0 + 1e-9

    def test_noise_floor_drives_the_current(self):
        # A larger floor needs proportionally more current for the same figs.
        small = select_four_point_current(
            100.0, self.TARGET_SNR, 1.0, lambda _v: 1e-6, compliance_v=50.0)
        big = select_four_point_current(
            100.0, self.TARGET_SNR, 1.0, lambda _v: 1e-4, compliance_v=50.0)
        assert big.current == pytest.approx(100 * small.current, rel=1e-3)
        assert big.verdict == 'ok'

    def test_higher_sig_fig_target_needs_more_current(self):
        three = select_four_point_current(100.0, 1e3, 1.0, self._flat_floor, compliance_v=50.0)
        five = select_four_point_current(100.0, 1e5, 1.0, self._flat_floor, compliance_v=50.0)
        assert five.current > three.current
        assert five.sig_figs > three.sig_figs

    def test_returns_named_tuple(self):
        sel = select_four_point_current(100.0, self.TARGET_SNR, 0.02, self._flat_floor, compliance_v=5.0)
        assert isinstance(sel, CurrentSelection)
        assert hasattr(sel, 'sig_figs')

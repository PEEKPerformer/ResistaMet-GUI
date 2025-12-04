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
    FourPointProbeResult,
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

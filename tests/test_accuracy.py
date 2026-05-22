"""Tests for the per-range accuracy module.

Reference values are pulled directly from:
- Keithley 2400 User's Manual, Section 4 (Ohms accuracy calculations)
  and Appendix A (Accuracy calculations).
- Series 2400 SourceMeter SMU Datasheet (1KW-2798-3, April 2021),
  Voltage/Current/Resistance Accuracy tables on pp. 5-7.
"""

import math

import pytest

from resistamet_gui.accuracy import (
    AccuracySpec,
    current_source_uncertainty,
    current_uncertainty,
    known_models,
    resistance_uncertainty,
    voltage_source_uncertainty,
    voltage_uncertainty,
)


# ---------------------------------------------------------------------------
# AccuracySpec primitives
# ---------------------------------------------------------------------------


class TestAccuracySpec:
    def test_combined_formula(self):
        # Datasheet 200 mV range: 0.012% rdg + 300 µV. At 100 mV reading:
        # ± (0.00012 × 0.1 + 300e-6) = 1.2e-5 + 3e-4 = 3.12e-4 V.
        spec = AccuracySpec(range_max=0.2, pct_reading=0.00012, offset=300e-6)
        assert math.isclose(spec.uncertainty(0.1), 3.12e-4)

    def test_negative_reading_uses_absolute(self):
        spec = AccuracySpec(range_max=0.2, pct_reading=0.00012, offset=300e-6)
        assert spec.uncertainty(-0.1) == spec.uncertainty(0.1)


# ---------------------------------------------------------------------------
# Voltage measure accuracy — datasheet p. 5 (2400/2401)
# ---------------------------------------------------------------------------


class TestVoltageUncertainty:
    def test_manual_appendix_a_example(self):
        # Manual Appendix A, p. A-2: 10 V on the 20 V range, 1-year.
        # 0.015% × 10 V + 1.5 mV = 1.5 mV + 1.5 mV = ±3 mV.
        sigma = voltage_uncertainty(10.0, model="2400", nplc=1.0)
        assert math.isclose(sigma, 3e-3, rel_tol=1e-6)

    def test_range_inference_picks_2v(self):
        # 1.5 V should land on the 2 V range (not 20 V).
        # 2 V range: 0.012% × 1.5 + 300 µV = 1.8e-4 + 3e-4 = 4.8e-4 V.
        sigma = voltage_uncertainty(1.5, model="2400", nplc=1.0)
        assert math.isclose(sigma, 4.8e-4, rel_tol=1e-6)

    def test_range_inference_picks_20v_for_overrange(self):
        # 2.5 V exceeds the 2 V range's 105% overrange (= 2.1 V) → rolls
        # to the 20 V range. 20 V spec: 0.015% × 2.5 + 1.5 mV = 1.875 mV.
        sigma = voltage_uncertainty(2.5, model="2400", nplc=1.0)
        assert math.isclose(sigma, 1.875e-3, rel_tol=1e-6)

    def test_range_inference_stays_on_2v_within_overrange(self):
        # 1.95 V is still inside 2 V × 1.05 = 2.1 V overrange capacity, so
        # the active range is 2 V. 2 V spec: 0.012% × 1.95 + 300 µV = 534 µV.
        sigma = voltage_uncertainty(1.95, model="2400", nplc=1.0)
        assert math.isclose(sigma, 5.34e-4, rel_tol=1e-6)

    def test_nplc_modifier_fast(self):
        # 200 mV range is the "special" group → 0.01 PLC adds 0.5% of range
        # (= 1 mV) on top of the normal spec.
        # Normal: 0.012% × 0.1 + 300 µV = 312 µV. Fast adds 1 mV.
        sigma_normal = voltage_uncertainty(0.1, model="2400", nplc=1.0)
        sigma_fast = voltage_uncertainty(0.1, model="2400", nplc=0.01)
        assert math.isclose(sigma_normal, 312e-6, rel_tol=1e-6)
        assert math.isclose(sigma_fast, 312e-6 + 1e-3, rel_tol=1e-6)

    def test_nplc_modifier_medium_default_range(self):
        # 2 V range is a "default" range → 0.1 PLC adds 0.005% of range
        # (= 100 µV). Reading 1 V: normal 0.012%×1+300µV=420µV, medium +100µV.
        sigma_normal = voltage_uncertainty(1.0, model="2400", nplc=1.0)
        sigma_medium = voltage_uncertainty(1.0, model="2400", nplc=0.1)
        assert math.isclose(sigma_normal, 4.2e-4, rel_tol=1e-6)
        assert math.isclose(sigma_medium, 4.2e-4 + 1e-4, rel_tol=1e-6)

    def test_unknown_model_falls_back_to_2400(self):
        # An unfamiliar model string shouldn't error; it should fall back
        # to the 2400 baseline (most conservative for the family).
        sigma = voltage_uncertainty(10.0, model="9999", nplc=1.0)
        assert math.isclose(sigma, 3e-3, rel_tol=1e-6)


# ---------------------------------------------------------------------------
# Current measure accuracy — datasheet p. 6 (2400/2401)
# ---------------------------------------------------------------------------


class TestCurrentUncertainty:
    def test_5ma_falls_on_10ma_range(self):
        # Manual Section 4 example: I-measure on 10 mA range, 1-year spec is
        # 0.035% rdg + 600 nA. At 5 mA: 0.035% × 5e-3 + 600 nA
        # = 1.75 µA + 600 nA = 2.35 µA.
        sigma = current_uncertainty(5e-3, model="2400", nplc=1.0)
        assert math.isclose(sigma, 2.35e-6, rel_tol=1e-6)

    def test_1ua_lowest_range(self):
        # 1 µA range: 0.029% rdg + 300 pA. At 500 nA:
        # 0.029% × 5e-7 + 3e-10 = 1.45e-10 + 3e-10 = 4.45e-10 A.
        sigma = current_uncertainty(5e-7, model="2400", nplc=1.0)
        assert math.isclose(sigma, 4.45e-10, rel_tol=1e-6)

    def test_1a_range_is_special_for_nplc(self):
        # 1 A range is a "special" range → 0.01 PLC adds 0.5% of range
        # (= 5 mA, huge). Reading 700 mA on the 1 A range:
        # Normal: 0.22% × 0.7 + 570 µA = 1.54e-3 + 5.7e-4 = 2.11 mA.
        # Fast (0.01 PLC): + 0.5% × 1 A = 5 mA → 7.11 mA total.
        sigma_normal = current_uncertainty(0.7, model="2400", nplc=1.0)
        sigma_fast = current_uncertainty(0.7, model="2400", nplc=0.01)
        assert math.isclose(sigma_normal, 2.11e-3, rel_tol=1e-6)
        assert math.isclose(sigma_fast, 2.11e-3 + 5e-3, rel_tol=1e-6)


# ---------------------------------------------------------------------------
# Resistance propagation — manual Section 4 worked example
# ---------------------------------------------------------------------------


class TestResistanceUncertainty:
    def test_100mohm_at_5ma_normal_mode_via_propagation(self):
        # Manual Section 4 (Ohms accuracy calculations), source-readback ON
        # case: V_meas = 500 µV on 200 mV range → V_acc = 300.06 µV;
        # I_meas = 5 mA on 10 mA range → I_acc = 2.35 µA. The manual sums
        # these as percentages (~60% from V, ~0.05% from I) for a total of
        # ~±60.06%, giving 100 mΩ ± 60.06% = 39.94 to 160.06 mΩ.
        #
        # We propagate in quadrature (RSS) which is the modern statistically
        # correct approach. The result is dominated by the V term either
        # way: σ_R ≈ R × σ_V/V = 100 mΩ × (300.06 µV / 500 µV) = 60.012 mΩ,
        # so the RSS answer is essentially identical to the linear-sum
        # answer for this dominated case.
        sigma = resistance_uncertainty(500e-6, 5e-3, model="2400", nplc=1.0)
        # Tight bound: should be in the 60 mΩ ballpark.
        assert 0.058 < sigma < 0.061, f"σ_R = {sigma}, expected ~60 mΩ"

    def test_1k5_at_1ma_is_reasonable(self):
        # 1.5 V at 1 mA → R = 1500 Ω. σ_V on 2V range = 0.012%×1.5+300µV
        # = 480 µV. σ_I on 1 mA range = 0.027%×1e-3+60 nA = 330 nA.
        # σ_R ≈ R × √((σ_V/V)² + (σ_I/I)²)
        #     = 1500 × √((480e-6/1.5)² + (330e-9/1e-3)²)
        #     = 1500 × √(1.024e-7 + 1.089e-7)
        #     = 1500 × 4.596e-4 ≈ 0.689 Ω
        sigma = resistance_uncertainty(1.5, 1e-3, model="2400", nplc=1.0)
        # Allow a generous bound for floating-point rounding.
        assert math.isclose(sigma, 0.689, rel_tol=0.01), f"σ_R = {sigma}"

    def test_zero_current_returns_nan(self):
        assert math.isnan(resistance_uncertainty(1.0, 0.0))

    def test_nan_inputs_return_nan(self):
        assert math.isnan(resistance_uncertainty(float("nan"), 1e-3))
        assert math.isnan(resistance_uncertainty(1.0, float("nan")))


# ---------------------------------------------------------------------------
# Model coverage
# ---------------------------------------------------------------------------


def test_known_models_includes_full_2400_family():
    # The four-digit IDN strings the worker is going to hand us.
    expected = {"2400", "2401", "2410", "2420", "2425", "2430", "2440"}
    assert expected.issubset(set(known_models()))


@pytest.mark.parametrize("model", ["2400", "2401", "2410", "2420", "2425", "2430", "2440"])
def test_every_model_gives_finite_uncertainty(model):
    # Smoke check: typical mid-range V and I produce a finite, positive
    # uncertainty for every model in the table. Catches typos in the
    # accuracy data (zero or NaN offsets, malformed range lists).
    assert voltage_uncertainty(1.0, model=model) > 0
    assert current_uncertainty(1e-3, model=model) > 0
    assert resistance_uncertainty(1.0, 1e-3, model=model) > 0
    assert voltage_source_uncertainty(1.0, model=model) > 0
    assert current_source_uncertainty(1e-3, model=model) > 0


# ---------------------------------------------------------------------------
# Source-side accuracy — datasheet p. 5/6 "Source Accuracy" columns
# ---------------------------------------------------------------------------


class TestSourceUncertainty:
    def test_manual_appendix_a_isource_example(self):
        # Manual Appendix A, p. A-2 worked example: source 0.7 mA on the
        # 1 mA range → ±(0.034% × 0.7 mA + 200 nA) = ±(238 nA + 200 nA)
        # = ±438 nA. Range 0.69956 mA to 0.70044 mA.
        sigma = current_source_uncertainty(0.7e-3, model="2400")
        assert math.isclose(sigma, 438e-9, rel_tol=1e-6)

    def test_voltage_source_2v_range(self):
        # 2 V range: 0.02% × 1.0 + 600 µV = 200 µV + 600 µV = 800 µV.
        sigma = voltage_source_uncertainty(1.0, model="2400")
        assert math.isclose(sigma, 800e-6, rel_tol=1e-6)

    def test_current_source_100ma_range(self):
        # 100 mA range: 0.066% × 50 mA + 20 µA = 33 µA + 20 µA = 53 µA.
        sigma = current_source_uncertainty(50e-3, model="2400")
        assert math.isclose(sigma, 53e-6, rel_tol=1e-6)

    def test_source_accuracy_ignores_nplc(self):
        # NPLC is a measurement parameter; source accuracy is independent.
        # We accept nplc for call-site symmetry but it must not change the
        # returned value.
        a = voltage_source_uncertainty(1.0, model="2400", nplc=1.0)
        b = voltage_source_uncertainty(1.0, model="2400", nplc=0.01)
        assert a == b

    def test_unknown_model_falls_back_to_2400(self):
        a = voltage_source_uncertainty(1.0, model="9999")
        b = voltage_source_uncertainty(1.0, model="2400")
        assert a == b

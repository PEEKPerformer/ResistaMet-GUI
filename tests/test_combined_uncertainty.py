"""Tests for combined statistical + instrument uncertainty helpers.

These power the 4PP and vdP result panels and the worker's finalize
metadata. The math used to live inline in main_window and workers; it
was moved into ``calculations.four_point_combined_uncertainty`` and
``calculations_vdp.vdp_combined_uncertainty`` so it's testable
without the GUI / a live measurement.
"""

import math
import random

import pytest

from resistamet_gui.calculations import (
    FourPointCombinedUncertainty,
    four_point_combined_uncertainty,
)
from resistamet_gui.calculations_vdp import (
    VdpCombinedUncertainty,
    vdp_combined_uncertainty,
)
from resistamet_gui.accuracy import (
    resistance_uncertainty,
    voltage_uncertainty,
)


# ---------------------------------------------------------------------------
# Four-point probe combined uncertainty
# ---------------------------------------------------------------------------


class TestFourPointCombinedUncertainty:
    def test_returns_none_for_empty_input(self):
        assert four_point_combined_uncertainty([], [], []) is None

    def test_returns_none_when_all_values_nan(self):
        nan = float("nan")
        result = four_point_combined_uncertainty([nan, nan], [1.0, 1.0], [1e-3, 1e-3])
        assert result is None

    def test_single_reading_has_no_stat_uncertainty(self):
        # N=1: std is undefined, u_stat should be 0; u_total = u_inst.
        v = 1.0; i = 1e-3
        result = four_point_combined_uncertainty(
            [v / i], [v], [i], model="2400", nplc=1.0,
        )
        assert result is not None
        assert result.u_stat == 0.0
        # u_inst should match resistance_uncertainty.
        expected_sigma_r = resistance_uncertainty(v, i, model="2400", nplc=1.0)
        assert math.isclose(result.u_inst, expected_sigma_r, rel_tol=1e-9)
        assert math.isclose(result.u_total, expected_sigma_r, rel_tol=1e-9)

    def test_high_n_low_noise_inst_dominates(self):
        # 100 readings of R=1.5 kΩ with 0.01% RSD on the V side. At V=1.5V on
        # the 2V range, σ_V/V ≈ 0.012% + 300µV/1.5V = 0.032%. σ_I on the
        # 1mA range is tiny. So u_inst ~0.32 Ω; u_stat ~0.15 Ω / √100 = 0.015 Ω.
        # Combined ≈ u_inst.
        random.seed(0)
        i = 1e-3
        v_noms = [1.5 * (1 + random.gauss(0, 0.0001)) for _ in range(100)]
        r_values = [v / i for v in v_noms]
        result = four_point_combined_uncertainty(
            r_values, v_noms, [i] * 100, model="2400", nplc=1.0,
        )
        assert result is not None
        assert result.u_inst > 10 * result.u_stat, (
            f"u_inst should dominate: u_stat={result.u_stat}, u_inst={result.u_inst}"
        )
        # u_total ≈ u_inst within a percent.
        assert math.isclose(result.u_total, result.u_inst, rel_tol=0.05)

    def test_combined_satisfies_pythagorean_sum(self):
        random.seed(1)
        i = 1e-3
        v_noms = [1.0 * (1 + random.gauss(0, 0.001)) for _ in range(20)]
        r_values = [v / i for v in v_noms]
        result = four_point_combined_uncertainty(
            r_values, v_noms, [i] * 20, model="2400", nplc=1.0,
        )
        assert result is not None
        expected_total = math.sqrt(result.u_stat ** 2 + result.u_inst ** 2)
        assert math.isclose(result.u_total, expected_total, rel_tol=1e-9)

    def test_propagates_to_derived_quantities(self):
        # The helper scales u_inst by mean of the input values, so passing
        # in Rs values vs ρ values produces uncertainties in the matching
        # units. Verify that consistency on a synthetic case where Rs and ρ
        # differ by a known factor (thickness).
        random.seed(2)
        i = 1e-3
        thickness_cm = 1e-4  # 1 µm
        v_noms = [1.0 + random.gauss(0, 1e-4) for _ in range(20)]
        r_values = [v / i for v in v_noms]
        # Suppose K_eff = 1, so Rs = R and ρ = Rs × t.
        rs_values = list(r_values)
        rho_values = [rs * thickness_cm for rs in rs_values]
        u_rs = four_point_combined_uncertainty(
            rs_values, v_noms, [i] * 20, model="2400", nplc=1.0,
        )
        u_rho = four_point_combined_uncertainty(
            rho_values, v_noms, [i] * 20, model="2400", nplc=1.0,
        )
        assert u_rs is not None and u_rho is not None
        # Relative uncertainties on Rs and ρ should match (same data behind).
        rel_rs = u_rs.u_total / u_rs.mean
        rel_rho = u_rho.u_total / u_rho.mean
        assert math.isclose(rel_rs, rel_rho, rel_tol=1e-9)

    def test_returns_namedtuple_shape(self):
        result = four_point_combined_uncertainty(
            [1000.0], [1.0], [1e-3], model="2400", nplc=1.0,
        )
        assert isinstance(result, FourPointCombinedUncertainty)
        assert result.mean == 1000.0


# ---------------------------------------------------------------------------
# Van der Pauw combined uncertainty
# ---------------------------------------------------------------------------


def _synthesize_vdp_voltages(r_true: float, current: float, noise: float = 0.0, seed: int = 0) -> dict:
    """Make a synthetic F76 8-reading voltage dict for a homogeneous sample.

    For a uniform sample with sheet resistance Rs, every geometry gives the
    same R = (V_p − V_n) / (2I). We back that out from r_true.
    """
    random.seed(seed)
    pairs = (
        ("V_21,34", "V_12,34"),
        ("V_32,41", "V_23,41"),
        ("V_43,12", "V_34,12"),
        ("V_14,23", "V_41,23"),
    )
    voltages = {}
    for p, n in pairs:
        v_p = +r_true * current * (1 + random.gauss(0, noise))
        v_n = -r_true * current * (1 + random.gauss(0, noise))
        voltages[p] = v_p
        voltages[n] = v_n
    return voltages


class TestVdpCombinedUncertainty:
    def test_homogeneous_sample_low_stat_inst_floor(self):
        # 1 kΩ uniform sample, 1 mA source, no synthetic noise → u_stat ≈ 0.
        # All uncertainty should be from σ_V on the 2V range readings.
        r_true = 1000.0
        current = 1e-3
        voltages = _synthesize_vdp_voltages(r_true, current, noise=0.0)
        # The F76 formula gives Rs ≈ π/ln2 × R_avg. We pass that through.
        rs = math.pi / math.log(2) * r_true  # ≈ 4532 Ω/sq
        rho = rs * 1e-4  # 1 µm thickness for example

        result = vdp_combined_uncertainty(
            voltages=voltages, current=current,
            sheet_resistance=rs, rho_avg=rho,
            model="2400", nplc=1.0,
        )
        assert math.isfinite(result.u_inst_R) and result.u_inst_R > 0
        # No noise → statistical should be zero (or machine-epsilon).
        assert result.u_stat_R < 1e-6
        # Pythagorean total ≈ u_inst.
        assert math.isclose(result.u_total_R, result.u_inst_R, rel_tol=1e-6)

    def test_zero_current_returns_nan(self):
        # Bad sample: I=0 makes R undefined.
        voltages = _synthesize_vdp_voltages(1000.0, 1e-3)
        result = vdp_combined_uncertainty(
            voltages=voltages, current=0.0,
            sheet_resistance=1.0, rho_avg=1e-4,
            model="2400", nplc=1.0,
        )
        for field in result:
            assert math.isnan(field)

    def test_missing_voltage_keys_return_nan(self):
        # F76 requires all 8 base labels; missing keys → graceful NaN.
        voltages = {"V_21,34": 1.0, "V_12,34": -1.0}  # only 2 of 8
        result = vdp_combined_uncertainty(
            voltages=voltages, current=1e-3,
            sheet_resistance=1.0, rho_avg=1e-4,
            model="2400", nplc=1.0,
        )
        for field in result:
            assert math.isnan(field)

    def test_relative_uncertainty_propagates_equally(self):
        # σ_Rs / Rs = σ_ρ / ρ since the F76 chain treats thickness as exact.
        voltages = _synthesize_vdp_voltages(1000.0, 1e-3)
        rs = math.pi / math.log(2) * 1000.0
        rho = rs * 1e-4
        result = vdp_combined_uncertainty(
            voltages=voltages, current=1e-3,
            sheet_resistance=rs, rho_avg=rho,
            model="2400", nplc=1.0,
        )
        assert math.isclose(result.u_rs / rs, result.u_rho / rho, rel_tol=1e-9)

    def test_noisy_data_increases_u_stat(self):
        # Add synthetic noise to the V readings → u_stat_R should grow,
        # u_inst_R should be roughly unchanged (it's set by the per-reading
        # spec, not the spread).
        clean = vdp_combined_uncertainty(
            voltages=_synthesize_vdp_voltages(1000.0, 1e-3, noise=0.0),
            current=1e-3, sheet_resistance=4532.0, rho_avg=0.4532,
            model="2400", nplc=1.0,
        )
        noisy = vdp_combined_uncertainty(
            voltages=_synthesize_vdp_voltages(1000.0, 1e-3, noise=0.01),
            current=1e-3, sheet_resistance=4532.0, rho_avg=0.4532,
            model="2400", nplc=1.0,
        )
        assert noisy.u_stat_R > clean.u_stat_R
        assert math.isclose(noisy.u_inst_R, clean.u_inst_R, rel_tol=0.1)

    def test_returns_namedtuple_shape(self):
        voltages = _synthesize_vdp_voltages(1000.0, 1e-3)
        result = vdp_combined_uncertainty(
            voltages=voltages, current=1e-3,
            sheet_resistance=1.0, rho_avg=1e-4,
            model="2400", nplc=1.0,
        )
        assert isinstance(result, VdpCombinedUncertainty)

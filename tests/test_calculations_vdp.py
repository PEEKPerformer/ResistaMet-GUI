"""
Unit tests for the van der Pauw calculations module (ASTM F76 Method A).

Tests are pinned to F76-08 (Reapproved 2016) Section 11 directly:
- Geometric factor f(Q) inverts the explicit forward equation in Fig. 5.
- F76 eqs. (1)-(2) recover rho on a synthetic uniform sample.
- F76 sec. 11.1 homogeneity gate (10 %) fires correctly.
- Protocol configuration list matches F76 sec. 10.4 voltage labels.

These tests do not require an instrument, scipy, or PyQt.
"""

import math

import pytest

from resistamet_gui.calculations_vdp import (
    F76_CONSTANT,
    F76_HOMOGENEITY_TOLERANCE_PCT,
    VdpConfiguration,
    VdpResult,
    calculate_van_der_pauw,
    f76_configurations,
    vdp_geometric_factor,
    vdp_resistivity_pair,
)


# Helper: the explicit forward equation from F76 Fig. 5.
# (Q - 1) / (Q + 1) = (f / ln 2) * arccosh{(1/2) * exp(ln 2 / f)}
def _q_from_f(f: float) -> float:
    ln2 = math.log(2.0)
    target = (f / ln2) * math.acosh(0.5 * math.exp(ln2 / f))
    return (1.0 + target) / (1.0 - target)


class TestF76Constant:
    """The constant in F76 eqs. (1)-(2) is pi / (4 ln 2)."""

    def test_value(self):
        # F76 states the value as "approximately 1.1331".
        assert F76_CONSTANT == pytest.approx(math.pi / (4.0 * math.log(2.0)))
        assert F76_CONSTANT == pytest.approx(1.1331, abs=1e-4)


class TestGeometricFactor:
    """vdp_geometric_factor inverts the F76 Fig. 5 implicit equation."""

    def test_symmetric_sample_returns_one(self):
        # f(1) = 1 by construction: target = 0, rhs(1) = 0.
        assert vdp_geometric_factor(1.0) == 1.0

    @pytest.mark.parametrize("f_truth", [0.99, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3])
    def test_round_trip_against_forward_equation(self, f_truth):
        # Compute Q from a known f using the explicit forward equation,
        # then ask the solver to recover f.
        q = _q_from_f(f_truth)
        f_solved = vdp_geometric_factor(q)
        assert f_solved == pytest.approx(f_truth, abs=1e-6)

    def test_monotone_decreasing(self):
        # f must decrease as Q grows.
        qs = [1.5, 2.0, 5.0, 10.0, 50.0, 100.0]
        fs = [vdp_geometric_factor(q) for q in qs]
        for earlier, later in zip(fs, fs[1:]):
            assert later < earlier

    def test_q_less_than_one_rejected(self):
        # Solver expects Q >= 1; pair function takes the reciprocal first.
        with pytest.raises(ValueError, match="Q must be >= 1"):
            vdp_geometric_factor(0.5)

    def test_non_finite_rejected(self):
        with pytest.raises(ValueError, match="Q must be finite"):
            vdp_geometric_factor(float("nan"))
        with pytest.raises(ValueError, match="Q must be finite"):
            vdp_geometric_factor(float("inf"))

    def test_extreme_asymmetry_returns_small_f(self):
        # As Q -> infinity, f -> 0. Practical floor on this solver is small
        # enough to be unambiguous even for unrealistically large Q.
        # F76 Fig. 5 doesn't tabulate beyond Q=100 (f ~= 0.4); anything
        # beyond that is academic.
        f = vdp_geometric_factor(1e12)
        assert 0.0 < f < 0.1


class TestProtocolConfigurations:
    """F76 sec. 10.4 lists 8 voltages; sec. 11 splits them into rho_A / rho_B."""

    def test_eight_entries(self):
        assert len(f76_configurations()) == 8

    def test_labels_match_f76_section_104(self):
        # The 8 voltage labels enumerated in F76 sec. 10.4.
        labels = [c.label for c in f76_configurations()]
        assert labels == [
            "V_21,34", "V_12,34", "V_32,41", "V_23,41",
            "V_43,12", "V_34,12", "V_14,23", "V_41,23",
        ]

    def test_group_a_and_b_split_four_four(self):
        configs = f76_configurations()
        assert sum(1 for c in configs if c.group == "A") == 4
        assert sum(1 for c in configs if c.group == "B") == 4

    def test_every_contact_used_as_both_source_and_sense(self):
        # Sanity: each of the 4 contacts must appear in at least one
        # source pair AND at least one sense pair across the base 8,
        # otherwise we'd never probe the full sample.
        configs = f76_configurations()
        source_contacts = set()
        sense_contacts = set()
        for c in configs:
            source_contacts.update([c.source_high, c.source_low])
            sense_contacts.update([c.sense_high, c.sense_low])
        assert source_contacts == {1, 2, 3, 4}
        assert sense_contacts == {1, 2, 3, 4}

    def test_polarity_alternates_within_each_geometry_pair(self):
        # F76 lists each geometry once with +I and once with -I:
        # V_21,34 then V_12,34 (source pair swapped is polarity reversal).
        configs = f76_configurations()
        for i in range(0, 8, 2):
            assert configs[i].source_high == configs[i + 1].source_low
            assert configs[i].source_low == configs[i + 1].source_high
            assert configs[i].sense_high == configs[i + 1].sense_high
            assert configs[i].sense_low == configs[i + 1].sense_low


def _uniform_sample_voltages(sheet_resistance_ohm_sq: float, current_a: float) -> dict:
    """Synthetic voltages for a perfectly uniform sample.

    For a uniform isotropic sample, all rotational permutations give the
    same R, where R_s = (pi / ln 2) * R (van der Pauw symmetric formula).
    Current reversal flips the sign of V; we ignore thermal offsets.
    """
    r = sheet_resistance_ohm_sq * math.log(2.0) / math.pi
    v = r * current_a
    return {
        "V_21,34": +v, "V_12,34": -v,
        "V_32,41": +v, "V_23,41": -v,
        "V_43,12": +v, "V_34,12": -v,
        "V_14,23": +v, "V_41,23": -v,
    }


class TestUniformSampleRecovery:
    """Synthetic uniform sample roundtrips through F76 eqs. (1)-(2)."""

    def test_recovers_sheet_resistance(self):
        rs_truth = 100.0  # Ohm/square
        current = 1.0e-3  # 1 mA
        thickness = 1.0e-5  # 100 nm
        v = _uniform_sample_voltages(rs_truth, current)

        result = calculate_van_der_pauw(v, current, thickness)

        # Uniform sample: f = 1, Q = 1, both rho values equal.
        assert result.q_a == pytest.approx(1.0)
        assert result.q_b == pytest.approx(1.0)
        assert result.f_a == pytest.approx(1.0)
        assert result.f_b == pytest.approx(1.0)
        assert result.rho_a == pytest.approx(result.rho_b)
        assert result.sheet_resistance == pytest.approx(rs_truth, rel=1e-9)
        assert result.rho_avg == pytest.approx(rs_truth * thickness, rel=1e-9)

    def test_homogeneous_flag_set(self):
        v = _uniform_sample_voltages(50.0, 1.0e-3)
        result = calculate_van_der_pauw(v, 1.0e-3, 1.0e-4)
        assert result.homogeneous is True
        assert result.asymmetry_pct == pytest.approx(0.0, abs=1e-9)

    @pytest.mark.parametrize("rs_truth", [0.5, 10.0, 100.0, 1e4, 1e6])
    def test_scales_across_decades(self, rs_truth):
        # vdP should be independent of measurement scale on uniform sample.
        current = 1.0e-3
        thickness = 1.0e-5
        v = _uniform_sample_voltages(rs_truth, current)
        result = calculate_van_der_pauw(v, current, thickness)
        assert result.sheet_resistance == pytest.approx(rs_truth, rel=1e-9)


class TestAsymmetricSample:
    """Q != 1 case: f(Q) correction matters and is applied per F76 eq. (1)."""

    def test_q_takes_reciprocal_when_below_one(self):
        # If the first delta is smaller than the second, Q < 1 raw, and
        # the pair function should silently use 1/Q.
        current = 1.0e-3
        thickness = 1.0e-5
        # Construct deltas with ratio 0.5 -> Q normalized to 2.
        v_pos, v_neg = +1.0e-3, -1.0e-3
        v_perp_pos, v_perp_neg = +2.0e-3, -2.0e-3
        rho, q, f = vdp_resistivity_pair(
            v_pos, v_neg, v_perp_pos, v_perp_neg, current, thickness
        )
        # delta_first = 2e-3, delta_second = 4e-3, ratio = 0.5 -> Q = 2.
        assert q == pytest.approx(2.0)
        # Should match the same call with deltas swapped.
        rho2, q2, f2 = vdp_resistivity_pair(
            v_perp_pos, v_perp_neg, v_pos, v_neg, current, thickness
        )
        assert q2 == pytest.approx(2.0)
        assert f2 == pytest.approx(f)
        # rho is the same up to which delta is "first" (formula is symmetric:
        # bracket is delta_first + delta_second).
        assert rho == pytest.approx(rho2)

    def test_f_factor_below_one_for_asymmetric(self):
        # Q = 2 should give f < 1.
        rho, q, f = vdp_resistivity_pair(
            +1.0e-3, -1.0e-3, +2.0e-3, -2.0e-3,
            current=1.0e-3, thickness_cm=1.0e-5,
        )
        assert q > 1.0
        assert 0.0 < f < 1.0


class TestHomogeneityGate:
    """F76 sec. 11.1: |rho_A - rho_B| / rho_av <= 10 % flag."""

    def test_uniform_passes(self):
        v = _uniform_sample_voltages(100.0, 1.0e-3)
        result = calculate_van_der_pauw(v, 1.0e-3, 1.0e-5)
        assert result.homogeneous is True
        assert result.asymmetry_pct < 1e-6

    def test_five_percent_asymmetry_passes(self):
        # rho_A != rho_B by 5 % -> still homogeneous per F76.
        v = _uniform_sample_voltages(100.0, 1.0e-3)
        # Boost group-B readings by 5 % proportionally so rho_B = 1.05 * rho_A.
        for label in ("V_43,12", "V_34,12", "V_14,23", "V_41,23"):
            v[label] *= 1.05
        result = calculate_van_der_pauw(v, 1.0e-3, 1.0e-5)
        # rho_A : rho_B = 1 : 1.05 -> asymmetry = 5/2.025 ~= 2.47 %
        # but the gate checks |rho_A - rho_B| / rho_av which here is ~4.88 %.
        assert result.asymmetry_pct < F76_HOMOGENEITY_TOLERANCE_PCT
        assert result.homogeneous is True

    def test_fifteen_percent_asymmetry_fails(self):
        v = _uniform_sample_voltages(100.0, 1.0e-3)
        for label in ("V_43,12", "V_34,12", "V_14,23", "V_41,23"):
            v[label] *= 1.15
        result = calculate_van_der_pauw(v, 1.0e-3, 1.0e-5)
        assert result.asymmetry_pct > F76_HOMOGENEITY_TOLERANCE_PCT
        assert result.homogeneous is False


class TestInputValidation:
    """Defensive checks at the calculation entry points."""

    def test_missing_label_raises_keyerror(self):
        v = _uniform_sample_voltages(100.0, 1.0e-3)
        del v["V_32,41"]
        with pytest.raises(KeyError, match=r"V_32,41"):
            calculate_van_der_pauw(v, 1.0e-3, 1.0e-5)

    def test_negative_current_raises(self):
        v = _uniform_sample_voltages(100.0, 1.0e-3)
        with pytest.raises(ValueError, match="current must be > 0"):
            calculate_van_der_pauw(v, -1.0e-3, 1.0e-5)

    def test_zero_current_raises(self):
        v = _uniform_sample_voltages(100.0, 1.0e-3)
        with pytest.raises(ValueError, match="current must be > 0"):
            calculate_van_der_pauw(v, 0.0, 1.0e-5)

    def test_negative_thickness_raises(self):
        v = _uniform_sample_voltages(100.0, 1.0e-3)
        with pytest.raises(ValueError, match="thickness must be > 0"):
            calculate_van_der_pauw(v, 1.0e-3, -1.0e-5)

    def test_degenerate_zero_orthogonal_delta_raises(self):
        # If one geometry yields zero net delta after current reversal,
        # Q is undefined.
        with pytest.raises(ValueError, match="orthogonal voltage difference"):
            vdp_resistivity_pair(
                +1.0e-3, -1.0e-3, +1.0e-3, +1.0e-3,
                current=1.0e-3, thickness_cm=1.0e-5,
            )


class TestResultDataclass:
    """VdpResult is a NamedTuple with expected fields."""

    def test_fields(self):
        v = _uniform_sample_voltages(100.0, 1.0e-3)
        result = calculate_van_der_pauw(v, 1.0e-3, 1.0e-5)
        assert isinstance(result, VdpResult)
        # Tuple unpacking should match the documented order.
        (rho_a, rho_b, rho_avg, sheet_r, q_a, q_b, f_a, f_b,
         homogeneous, asym_pct) = result
        assert rho_a == result.rho_a
        assert sheet_r == result.sheet_resistance
        assert homogeneous is True

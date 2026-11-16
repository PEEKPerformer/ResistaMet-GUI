"""Spot statistics against numbers worked out by hand."""
import math

import pytest

from resistamet_gui.accuracy import resistance_uncertainty
from resistamet_gui.session.spot_stats import (
    SpotSamples, quantity_statistics, relative_instrument_floor, spot_statistics,
)

CURRENT = 1e-3
#: Rs values whose statistics are easy to do on paper:
#: mean 101; deviations -1, 1, -3, 3; sum of squares 20; variance 20/3.
RS = [100.0, 102.0, 98.0, 104.0]
RS_MEAN = 101.0
RS_SD = math.sqrt(20.0 / 3.0)


def _voltages(rs_values):
    """The V a 4.532 geometry factor would have needed for each Rs."""
    return [rs / 4.532 * CURRENT for rs in rs_values]


def _instrument_floor(voltages, currents, model, nplc):
    floors = [resistance_uncertainty(v, i, model=model, nplc=nplc) / abs(v / i)
              for v, i in zip(voltages, currents)]
    return sum(floors) / len(floors)


class TestQuantityStatistics:
    def test_mean_sd_and_rsd_by_hand(self):
        stats = quantity_statistics(RS, _voltages(RS), [CURRENT] * 4)
        assert stats['n'] == 4
        assert stats['mean'] == pytest.approx(RS_MEAN)
        assert stats['sd'] == pytest.approx(RS_SD)
        assert stats['sd'] == pytest.approx(2.5819889, abs=1e-7)
        assert stats['rsd_pct'] == pytest.approx(2.5819889 / 101.0 * 100.0, abs=1e-6)

    def test_statistical_uncertainty_is_sd_over_root_n(self):
        stats = quantity_statistics(RS, _voltages(RS), [CURRENT] * 4)
        assert stats['u_stat'] == pytest.approx(RS_SD / 2.0)

    def test_instrument_floor_and_the_quadrature_sum(self):
        voltages, currents = _voltages(RS), [CURRENT] * 4
        stats = quantity_statistics(RS, voltages, currents, model='2420', nplc=1.0)
        floor = _instrument_floor(voltages, currents, '2420', 1.0)
        assert stats['u_inst'] == pytest.approx(RS_MEAN * floor)
        assert stats['u_total'] == pytest.approx(math.hypot(stats['u_stat'], stats['u_inst']))
        assert stats['u_inst'] > 0

    def test_nan_values_are_skipped_not_propagated(self):
        values = [100.0, float('nan'), 102.0, float('inf'), 98.0, 104.0]
        voltages = _voltages([100.0, 100.0, 102.0, 100.0, 98.0, 104.0])
        stats = quantity_statistics(values, voltages, [CURRENT] * 6)
        assert stats['n'] == 4
        assert stats['mean'] == pytest.approx(RS_MEAN)
        assert stats['sd'] == pytest.approx(RS_SD)

    def test_no_finite_value_is_all_nan(self):
        stats = quantity_statistics([float('nan')] * 3, _voltages(RS[:3]), [CURRENT] * 3)
        assert stats['n'] == 0
        for key in ('mean', 'sd', 'rsd_pct', 'u_stat', 'u_inst', 'u_total'):
            assert math.isnan(stats[key]), key

    def test_no_values_at_all(self):
        stats = quantity_statistics([], [], [])
        assert stats['n'] == 0
        assert math.isnan(stats['mean'])

    def test_one_value_has_a_mean_but_no_spread(self):
        stats = quantity_statistics([100.0], _voltages([100.0]), [CURRENT])
        assert stats['n'] == 1
        assert stats['mean'] == 100.0
        assert math.isnan(stats['sd'])
        assert math.isnan(stats['rsd_pct'])
        # As the shared function reports them, so the panels agree.
        assert stats['u_stat'] == 0.0
        assert stats['u_total'] == pytest.approx(stats['u_inst'])

    def test_rsd_uses_the_magnitude_of_the_mean(self):
        stats = quantity_statistics([-100.0, -102.0], _voltages([100.0, 102.0]), [CURRENT] * 2)
        assert stats['rsd_pct'] > 0

    def test_zero_mean_has_no_rsd(self):
        stats = quantity_statistics([-1.0, 1.0], _voltages([1.0, 1.0]), [CURRENT] * 2)
        assert stats['mean'] == 0.0
        assert math.isnan(stats['rsd_pct'])

    def test_matches_the_function_the_pyside_panel_calls(self):
        from resistamet_gui.calculations import four_point_combined_uncertainty

        voltages, currents = _voltages(RS), [CURRENT] * 4
        shared = four_point_combined_uncertainty(RS, voltages, currents, model='2400', nplc=0.1)
        stats = quantity_statistics(RS, voltages, currents, model='2400', nplc=0.1)
        assert stats['mean'] == pytest.approx(shared.mean)
        assert stats['rsd_pct'] == pytest.approx(shared.rsd_pct)
        assert (stats['u_stat'], stats['u_inst'], stats['u_total']) == (
            shared.u_stat, shared.u_inst, shared.u_total)


class TestSpotSamples:
    def _derived(self, rs, rho=float('nan'), sigma=float('nan')):
        return {'rs': rs, 'rho': rho, 'sigma': sigma, 'ratio': rs / 4.532}

    def test_collects_the_three_quantities(self):
        samples = SpotSamples()
        for rs, v in zip(RS, _voltages(RS)):
            samples.add(v, CURRENT, self._derived(rs, rho=rs * 1e-4, sigma=1.0 / (rs * 1e-4)))
        stats = spot_statistics(samples, model='2420', nplc=1.0)
        assert stats['n'] == 4
        assert stats['n_excluded'] == 0
        assert stats['rs']['mean'] == pytest.approx(RS_MEAN)
        assert stats['rho']['mean'] == pytest.approx(RS_MEAN * 1e-4)
        assert stats['rho']['sd'] == pytest.approx(RS_SD * 1e-4)
        assert stats['sigma']['n'] == 4

    def test_compliance_samples_are_counted_and_left_out(self):
        samples = SpotSamples()
        for rs, v in zip(RS, _voltages(RS)):
            samples.add(v, CURRENT, self._derived(rs))
        samples.add(5.0, 1e-9, self._derived(5e9), compliance='V_COMP')
        stats = spot_statistics(samples)
        assert (stats['n'], stats['n_excluded']) == (4, 1)
        assert stats['rs']['mean'] == pytest.approx(RS_MEAN)

    def test_unknown_thickness_leaves_rho_and_sigma_empty(self):
        samples = SpotSamples()
        for rs, v in zip(RS, _voltages(RS)):
            samples.add(v, CURRENT, self._derived(rs))
        stats = spot_statistics(samples)
        assert stats['rs']['n'] == 4
        assert stats['rho']['n'] == 0
        assert math.isnan(stats['sigma']['mean'])

    def test_an_empty_spot(self):
        stats = spot_statistics(SpotSamples())
        assert (stats['n'], stats['n_excluded'], stats['rs']['n']) == (0, 0, 0)

    def test_a_bad_value_becomes_nan_instead_of_raising(self):
        samples = SpotSamples()
        samples.add('garbage', None, {'rs': 'x'})
        assert len(samples) == 1
        assert math.isnan(samples.voltage[0]) and math.isnan(samples.rs[0])

    def test_a_missing_derived_dict_is_a_sample_without_derived_values(self):
        samples = SpotSamples()
        samples.add(0.1, CURRENT, None)
        samples.add(0.1, CURRENT, 'not a dict')
        assert len(samples) == 2
        assert math.isnan(samples.rs[0]) and math.isnan(samples.sigma[1])
        assert samples.voltage[0] == 0.1

    def test_a_sample_is_kept_whole_or_not_at_all(self):
        """The columns stay the same length whatever the values were."""
        samples = SpotSamples()
        samples.add(10 ** 400, CURRENT, {'rs': [1, 2], 'rho': object(), 'sigma': '1e3'})
        lengths = {len(getattr(samples, name))
                   for name in ('voltage', 'current', 'rs', 'rho', 'sigma')}
        assert lengths == {1}
        assert math.isnan(samples.voltage[0])      # too large for a double
        assert samples.sigma[0] == 1000.0
        assert spot_statistics(samples)['n'] == 1


class TestTheInstrumentFloorIsWorkedOutOnce:
    def _samples(self):
        samples = SpotSamples()
        for rs, v in zip(RS, _voltages(RS)):
            samples.add(v, CURRENT, {'rs': rs, 'rho': rs * 1e-4, 'sigma': 1e4 / rs})
        return samples

    def test_it_is_the_mean_relative_sigma_r(self):
        voltages, currents = _voltages(RS), [CURRENT] * 4
        assert relative_instrument_floor(voltages, currents, '2420', 1.0) == pytest.approx(
            _instrument_floor(voltages, currents, '2420', 1.0))

    def test_no_usable_reading_is_a_floor_of_zero(self):
        assert relative_instrument_floor([], []) == 0.0
        assert relative_instrument_floor([float('nan')], [CURRENT]) == 0.0

    def test_every_quantity_equals_the_shared_function_called_with_the_readings(self):
        """The guard against drift: bit for bit, not approximately."""
        from resistamet_gui.calculations import four_point_combined_uncertainty

        samples = self._samples()
        stats = spot_statistics(samples, model='2420', nplc=0.1)
        for name in ('rs', 'rho', 'sigma'):
            shared = four_point_combined_uncertainty(
                list(getattr(samples, name)), list(samples.voltage), list(samples.current),
                model='2420', nplc=0.1)
            assert (stats[name]['u_stat'], stats[name]['u_inst'], stats[name]['u_total']) == (
                shared.u_stat, shared.u_inst, shared.u_total), name

    def test_the_accuracy_tables_are_walked_once_per_spot(self, monkeypatch):
        from resistamet_gui import accuracy

        calls = {'n': 0}
        real = accuracy.resistance_uncertainty

        def counted(*args, **kwargs):
            calls['n'] += 1
            return real(*args, **kwargs)
        monkeypatch.setattr(accuracy, 'resistance_uncertainty', counted)
        spot_statistics(self._samples())
        assert calls['n'] == 4          # one per reading, not three

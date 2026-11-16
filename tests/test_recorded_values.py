"""What a row records, when the instrument or the settings are awkward.

Each class is a defect found in bench files or in review: a number that was
written wrong while everything looked fine. The simulator echoes the
programmed source level exactly and has no fixed ranges, so none of these
could fail before they were written.
"""
import copy
import csv
import math
import time

import pytest

from resistamet_gui import accuracy
from resistamet_gui.calculations import f2_finite_diameter, four_point_combined_uncertainty
from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.session import ListSink, MeasurementSession
from resistamet_gui.session.configure import FourPointState
from resistamet_gui.session.samples import (
    build_row, fixed_range_for, parse_four_point, parse_source_i, parse_source_v,
)

OVERFLOW = 9.9e37


@pytest.fixture(autouse=True)
def _no_sleep_inhibitor(monkeypatch):
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)


def _state(current=1e-3, delta=False):
    return FourPointState(source_current=current, delta_mode=delta, delta_settling=0.1,
                          power_warn_w=1.0, power_stop_w=2.0, stop_on_overpower=True)


def _fpp(**overrides):
    settings = {
        'fpp_current': 1e-3, 'fpp_voltage_compliance': 5.0, 'fpp_voltage_range_auto': True,
        'fpp_spacing_cm': 0.1016, 'fpp_thickness_um': 0.0, 'fpp_k_factor': 4.532,
        'fpp_alpha': 1.0, 'fpp_model': 'thin_film', 'fpp_diameter_cm': 0.0,
        'fpp_geometry': 'circle', 'fpp_temperature_c': float('nan'),
        'fpp_dopant_type': 'none',
    }
    settings.update(overrides)
    return settings


def _row(settings, volts, amps, compliance='OK'):
    row, derived = build_row('four_point', 0.0, {'voltage': volts, 'current': amps},
                             compliance, '', settings, 1.0, False, '2420', None)
    return row, derived


class TestTheCurrentIsTheOneProgrammed:
    """A 2420 returned a CURR element that crept towards the setpoint over
    forty readings (0.2094 of it on the first row) while V was steady, and
    V/I was computed from it: sheet resistance 4.8 times too high on row 1."""

    def test_four_point_ignores_a_lagging_element(self):
        data, status, _ = parse_four_point(
            ['5.81032E+01', '1.45539E-04', '0'], 0, False,
            _fpp(fpp_current=6.951282895854303e-4, fpp_voltage_compliance=60.0), 1.0,
            '2420', _state(6.951282895854303e-4), None)
        assert status == 'OK'
        assert data['current'] == 6.951282895854303e-4
        assert data['voltage'] == 58.1032

    def test_a_value_that_range_could_never_read_is_not_recorded(self):
        # -23.7 uA came back on the 1 uA range.
        data, _, _ = parse_four_point(['4.43772', '-2.37055E-05', '0'], 0, False,
                                      _fpp(fpp_current=1e-6), 1.0, '2420', _state(1e-6), None)
        assert data['current'] == 1e-6

    def test_a_delta_reading_keeps_the_magnitude_the_run_put_there(self):
        data, _, _ = parse_four_point(['0.1', '1.0E-03', '0'], 0, False,
                                      _fpp(fpp_current=-1e-3), 1.0, '2420',
                                      _state(-1e-3, delta=True), None)
        assert data['current'] == 1e-3

    def test_source_i_records_the_programmed_current(self):
        settings = {'isource_current': 2e-3, 'isource_voltage_compliance': 10.0,
                    'isource_voltage_range_auto': True}
        data, _, _ = parse_source_i(['0.2', '4.0E-04', '0'], 0, False, settings, 1.0,
                                    '2400', None, None)
        assert data['current'] == 2e-3

    def test_source_v_records_the_programmed_voltage(self):
        settings = {'vsource_voltage': 1.5, 'vsource_current_compliance': 0.1,
                    'vsource_current_range_auto': True}
        data, _, _ = parse_source_v(['0.3', '1.5E-02', '0'], 0, False, settings, 1.0,
                                    '2400', None, None)
        assert data['voltage'] == 1.5

    def test_a_run_writes_the_setpoint_whatever_the_element_says(
            self, fake_rm, tmp_path, monkeypatch):
        from resistamet_gui._simulator import FakeKeithley
        real = FakeKeithley._format_elements

        def lagging(self, v, i, r, stat):
            return real(self, v, i * 0.2094, r, stat)
        monkeypatch.setattr(FakeKeithley, '_format_elements', lagging)

        profile = copy.deepcopy({k: DEFAULT_SETTINGS[k] for k in ('measurement', 'display')})
        profile['file'] = {'auto_save_interval': 60, 'data_directory': str(tmp_path / 'data')}
        profile['measurement'].update({
            'gpib_address': 'GPIB0::24::INSTR', 'sampling_rate': 100.0, 'settling_time': 0.0,
            'fpp_current': 1e-4, 'fpp_voltage_compliance': 5.0, 'fpp_samples': 3,
            'fpp_power_warn_w': 1.0, 'fpp_power_stop_w': 2.0,
        })
        sink = ListSink()
        session = MeasurementSession(sink)
        try:
            session.start(profile, 'four_point', 'wafer', 'alice')
            deadline = time.time() + 10.0
            while session.state != 'idle' and time.time() < deadline:
                time.sleep(0.02)
            path = sink.of_type('run_ended')[-1].payload['path']
        finally:
            session.close(timeout=5.0)
        with open(path) as handle:
            rows = [r for r in csv.reader(handle) if r and not r[0].startswith('#')]
        columns = dict(zip(rows[0], zip(*rows[1:])))
        assert len(rows) == 4
        for volts, amps, ratio, rs in zip(columns['V'], columns['I'],
                                          columns['V_over_I'], columns['Rs_ohm_sq']):
            assert float(amps) == 1e-4
            assert float(ratio) == pytest.approx(float(volts) / 1e-4, rel=1e-9)
            assert float(rs) == pytest.approx(4.532 * float(volts) / 1e-4, rel=1e-9)


class TestSheetResistanceNeedsNoThickness:
    """A diameter with no thickness put every run of one user on the F84 path
    with Rs = rho / t = NaN, under a header identical to a working file's."""

    def test_finite_diameter_and_no_thickness(self):
        settings = _fpp(fpp_diameter_cm=5.08)
        row, derived = _row(settings, 0.1, 1e-3)
        f2 = f2_finite_diameter(0.1016, 5.08)
        assert derived['method'] == 'f84'
        assert derived['rs'] == pytest.approx(f2 * 100.0)
        assert row[4] == derived['rs']
        assert math.isnan(derived['rho'])        # that does need a thickness

    def test_with_a_thickness_it_is_still_rho_over_t(self):
        _, derived = _row(_fpp(fpp_diameter_cm=5.08, fpp_thickness_um=100.0), 0.1, 1e-3)
        assert derived['rs'] == pytest.approx(derived['rho'] / 100e-4)

    def test_a_thin_sample_gives_one_rs_with_or_without_its_thickness(self):
        _, bare = _row(_fpp(fpp_diameter_cm=5.08), 0.1, 1e-3)
        _, thin = _row(_fpp(fpp_diameter_cm=5.08, fpp_thickness_um=10.0), 0.1, 1e-3)
        assert bare['rs'] == pytest.approx(thin['rs'], rel=1e-9)


class TestSettingsThatAreMissing:
    def test_a_missing_setting_gets_its_default(self):
        settings = _fpp()
        del settings['fpp_alpha'], settings['fpp_k_factor']
        _, derived = _row(settings, 0.1, 1e-3)
        assert derived['rs'] == pytest.approx(453.2)


class TestUncertaintyOnTheRangeTheRunUsed:
    """With auto range off the instrument sits on the range that holds the
    compliance value. The lookup inferred the range from the reading."""

    def test_the_fixed_range_is_the_lowest_that_holds_the_limit(self):
        on_200mv = accuracy.voltage_uncertainty(1e-3, '2400', 1.0)
        on_20v = accuracy.voltage_uncertainty(1e-3, '2400', 1.0, fixed_range=5.0)
        assert on_20v > 3 * on_200mv
        assert on_20v == accuracy.voltage_uncertainty(1e-3, '2400', 1.0, fixed_range=20.0)

    def test_current_measured_on_a_fixed_range(self):
        inferred = accuracy.current_uncertainty(1e-6, '2400', 1.0)
        pinned = accuracy.current_uncertainty(1e-6, '2400', 1.0, fixed_range=0.1)
        assert pinned > 1000 * inferred

    def test_auto_range_infers_as_before(self):
        assert fixed_range_for({'fpp_voltage_range_auto': True,
                                'fpp_voltage_compliance': 5.0},
                               'fpp_voltage_range_auto', 'fpp_voltage_compliance') is None
        assert accuracy.voltage_uncertainty(1e-3, '2400', 1.0, fixed_range=None) == \
            accuracy.voltage_uncertainty(1e-3, '2400', 1.0)

    def test_a_four_point_row_on_a_fixed_range(self):
        _, auto = _row(_fpp(), 1e-3, 1e-3)
        _, fixed = _row(_fpp(fpp_voltage_range_auto=False), 1e-3, 1e-3)
        assert fixed['v_unc'] == accuracy.voltage_uncertainty(1e-3, '2420', 1.0, fixed_range=5.0)
        assert fixed['v_unc'] > auto['v_unc']

    def test_the_footer_floor_follows_the_rows(self):
        auto = four_point_combined_uncertainty([1.0], [1e-3], [1e-3], model='2420')
        fixed = four_point_combined_uncertainty([1.0], [1e-3], [1e-3], model='2420',
                                                v_fixed_range=5.0)
        assert fixed.u_inst > auto.u_inst


class TestTheSourcedCurrentHasTheSourceAccuracy:
    def test_four_point_i_unc_is_the_source_specification(self):
        _, derived = _row(_fpp(fpp_current=1e-4), 0.1, 1e-4)
        assert derived['i_unc'] == accuracy.current_source_uncertainty(1e-4, '2420')
        assert derived['i_unc'] > accuracy.current_uncertainty(1e-4, '2420', 1.0)


class TestAnOverflowIsNotAResult:
    def test_source_i_overflow_voltage(self):
        settings = {'isource_current': 1e-3, 'isource_voltage_compliance': 10.0,
                    'isource_voltage_range_auto': True}
        data, status, _ = parse_source_i([str(OVERFLOW), '1E-3', '8'], 8, True, settings,
                                         1.0, '2420', None, None)
        assert status == 'V_COMP'
        assert math.isnan(data['voltage_unc']) and math.isnan(data['resistance_unc'])
        row, _ = build_row('source_i', 0.0, data, status, '', settings, 1.0, False,
                           '2420', None)
        assert row[1] == OVERFLOW           # the reading itself is kept
        assert math.isnan(row[3])           # 9.9e40 ohm is not written

    def test_source_v_overflow_current(self):
        settings = {'vsource_voltage': 1.0, 'vsource_current_compliance': 0.1,
                    'vsource_current_range_auto': True}
        data, status, _ = parse_source_v(['1.0', str(OVERFLOW), '8'], 8, True, settings,
                                         1.0, '2420', None, None)
        assert status == 'I_COMP'
        assert math.isnan(data['current_unc'])
        row, _ = build_row('source_v', 0.0, data, status, '', settings, 1.0, False,
                           '2420', None)
        assert math.isnan(row[3])

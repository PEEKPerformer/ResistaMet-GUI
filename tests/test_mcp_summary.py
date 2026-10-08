"""Run files as statistics, from literal file text with hand-computed answers.

No SDK and no network: ``summary`` is pure.
"""
import math

import pytest

from resistamet_gui.mcp.summary import (MAX_ROWS, column_stats, find_listed, fit_sweep,
                                        read_slice, summarise)

RESISTANCE = """\
# resistamet_format_version: 2.0
# user: alice
# sample: s1
# mode: resistance
# params.test_current_A: 0.001
# params.auto_range: true
# started_by: agent
# units: s,V,A,\u03a9,\u03a9,,
elapsed_s,V_meas,I_meas,R_ohm,R_unc_ohm,compliance,event
0.1,0.1,0.001,100,0.06,OK,
0.2,0.1,0.001,102,0.06,OK,lamp on
0.3,0.1,0.001,98,0.06,V_COMP,
0.4,0.1,0.001,104,0.06,OK,"a, b"
# --- run completed ---
# ended_at: 2026-10-07T15:31:12
# total_samples: 4
"""

#: A van der Pauw file: words in most columns, its result in the end block.
VDP = """\
# mode: vdp
# units: s,,,,,,,,V,,V,A
elapsed_s,geometry,group,source_high,source_low,sense_high,sense_low,label_pos,V_pos,label_neg,V_neg,current_A
1.0,R_21_34,A,2,1,3,4,V_34+,0.0010,V_34-,-0.0011,0.001
2.0,R_32_41,A,3,2,4,1,V_41+,0.0012,V_41-,-0.0013,0.001
# --- run completed ---
# sheet_resistance: 5.65e-3
# homogeneous: true
"""


class TestColumnStats:
    def test_hand_computed(self):
        # mean 101; squared deviations 1 + 1 + 9 + 9 = 20; sd = sqrt(20 / 3).
        stats = column_stats(['100', '102', '98', '104'])
        assert stats['count'] == 4
        assert stats['mean'] == 101.0
        assert stats['sd'] == pytest.approx(math.sqrt(20 / 3))
        assert (stats['min'], stats['max'], stats['last']) == (98.0, 104.0, 104.0)

    def test_one_value_has_no_sd(self):
        assert column_stats(['5'])['sd'] is None

    def test_non_finite_and_empty_cells_are_left_out(self):
        stats = column_stats(['1', 'nan', '', '3', 'inf'])
        assert (stats['count'], stats['mean'], stats['last']) == (2, 2.0, 3.0)

    def test_a_column_of_words_is_not_numeric(self):
        assert column_stats(['1', 'R_21_34']) is None

    def test_an_empty_column_counts_nothing(self):
        assert column_stats(['', '']) == {'count': 0, 'mean': None, 'sd': None,
                                          'min': None, 'max': None, 'last': None}


class TestSummarise:
    def test_a_resistance_run(self):
        summary = summarise(RESISTANCE)
        assert summary['rows'] == 4
        assert summary['finalized'] is True
        r = summary['columns']['R_ohm']
        assert r['unit'] == '\u03a9'
        assert (r['count'], r['mean'], r['min'], r['max'], r['last']) == (4, 101.0, 98.0,
                                                                           104.0, 104.0)
        assert r['sd'] == pytest.approx(math.sqrt(20 / 3))
        # 0.1 .. 0.4: mean 0.25, squared deviations 2 * (0.15^2 + 0.05^2) = 0.05.
        t = summary['columns']['elapsed_s']
        assert t['mean'] == pytest.approx(0.25)
        assert t['sd'] == pytest.approx(math.sqrt(0.05 / 3))
        assert summary['columns']['V_meas']['sd'] == pytest.approx(0.0)
        assert set(summary['columns']) == {'elapsed_s', 'V_meas', 'I_meas', 'R_ohm',
                                           'R_unc_ohm'}

    def test_compliance_and_marks(self):
        summary = summarise(RESISTANCE)
        assert summary['compliance'] == {'rows': 1, 'kinds': {'V_COMP': 1}}
        assert summary['marks'] == [{'row': 2, 'elapsed_s': 0.2, 'label': 'lamp on'},
                                    {'row': 4, 'elapsed_s': 0.4, 'label': 'a, b'}]
        assert summary['marks_total'] == 2

    def test_the_header_and_end_blocks(self):
        summary = summarise(RESISTANCE)
        assert summary['metadata']['params.test_current_A'] == 0.001
        assert summary['metadata']['params.auto_range'] is True
        assert summary['metadata']['started_by'] == 'agent'
        assert summary['metadata']['units'] == ['s', 'V', 'A', '\u03a9', '\u03a9', '', '']
        assert summary['end'] == {'ended_at': '2026-10-07T15:31:12', 'total_samples': 4}

    def test_a_file_still_being_written_has_no_end(self):
        open_run = RESISTANCE.split('# --- run completed ---')[0]
        summary = summarise(open_run)
        assert (summary['finalized'], summary['end'], summary['rows']) == (False, None, 4)

    def test_windows_line_ends(self):
        assert summarise(RESISTANCE.replace('\n', '\r\n')) == summarise(RESISTANCE)

    def test_a_vdp_run_keeps_its_numbers_and_its_result(self):
        summary = summarise(VDP)
        assert set(summary['columns']) == {'elapsed_s', 'source_high', 'source_low',
                                           'sense_high', 'sense_low', 'V_pos', 'V_neg',
                                           'current_A'}
        assert summary['columns']['V_pos']['mean'] == pytest.approx(0.0011)
        assert summary['end'] == {'sheet_resistance': 5.65e-3, 'homogeneous': True}
        assert 'compliance' not in summary and 'marks' not in summary

    def test_a_non_finite_header_value_is_null(self):
        summary = summarise('# params.temperature_c: NaN\nelapsed_s\n0.1\n')
        assert summary['metadata']['params.temperature_c'] is None

    def test_marks_are_capped(self):
        rows = ''.join(f'{i},m{i}\n' for i in range(80))
        summary = summarise('elapsed_s,event\n' + rows)
        assert len(summary['marks']) == 50
        assert summary['marks_total'] == 80


class TestFirstRows:
    def test_only_the_first_rows_count(self):
        summarised = summarise(RESISTANCE, first_rows=2)
        # 100 and 102: mean 101, sd sqrt(2).
        resistance = summarised['columns']['R_ohm']
        assert (resistance['count'], resistance['mean'], resistance['last']) == (2, 101.0, 102.0)
        assert resistance['sd'] == pytest.approx(math.sqrt(2))
        assert (summarised['rows'], summarised['rows_total'], summarised['first_rows']) == \
            (2, 4, 2)
        assert summarised['note'] == ("statistics, compliance and marks over the first 2 "
                                      "of 4 data rows")

    def test_compliance_and_marks_are_of_those_rows_too(self):
        summarised = summarise(RESISTANCE, first_rows=2)
        assert summarised['compliance'] == {'rows': 0, 'kinds': {}}
        assert [mark['label'] for mark in summarised['marks']] == ['lamp on']

    def test_more_than_the_file_has_is_the_whole_file(self):
        summarised = summarise(RESISTANCE, first_rows=10)
        assert (summarised['rows'], summarised['rows_total']) == (4, 4)
        assert summarised['columns']['R_ohm']['count'] == 4

    def test_without_it_nothing_is_said_about_it(self):
        assert 'rows_total' not in summarise(RESISTANCE)


class TestReadSlice:
    def test_a_slice_from_the_start(self):
        sliced = read_slice(RESISTANCE, offset=1, rows=2)
        assert sliced['total_rows'] == 4
        assert sliced['offset'] == 1
        assert sliced['columns'][3] == 'R_ohm'
        assert sliced['rows'] == [[0.2, 0.1, 0.001, 102, 0.06, 'OK', 'lamp on'],
                                  [0.3, 0.1, 0.001, 98, 0.06, 'V_COMP', '']]
        assert sliced['end']['total_samples'] == 4

    def test_a_negative_offset_counts_from_the_end(self):
        sliced = read_slice(RESISTANCE, offset=-1)
        assert sliced['offset'] == 3
        assert sliced['rows'] == [[0.4, 0.1, 0.001, 104, 0.06, 'OK', 'a, b']]

    def test_past_the_end_is_empty(self):
        sliced = read_slice(RESISTANCE, offset=10)
        assert (sliced['offset'], sliced['rows']) == (4, [])

    def test_the_slice_is_bounded(self):
        rows = ''.join(f'{i}\n' for i in range(MAX_ROWS + 100))
        sliced = read_slice('n\n' + rows, rows=100_000)
        assert len(sliced['rows']) == MAX_ROWS
        assert sliced['total_rows'] == MAX_ROWS + 100

    def test_nan_is_null(self):
        assert read_slice('a,b\nnan,x\n')['rows'] == [[None, 'x']]


class TestFindListed:
    FILES = [
        {'path': 'bob/1_s1_R_1.00mA.csv', 'name': '1_s1_R_1.00mA.csv'},
        {'path': 'alice/1_s1_R_1.00mA.csv', 'name': '1_s1_R_1.00mA.csv'},
    ]

    def test_the_listed_path_the_run_path_ends_with(self):
        assert find_listed('measurement_data/alice/1_s1_R_1.00mA.csv',
                           self.FILES) == 'alice/1_s1_R_1.00mA.csv'

    def test_an_absolute_windows_path(self):
        assert find_listed('C:\\data\\alice\\1_s1_R_1.00mA.csv',
                           self.FILES) == 'alice/1_s1_R_1.00mA.csv'

    def test_failing_that_the_newest_of_the_same_name(self):
        assert find_listed('/elsewhere/1_s1_R_1.00mA.csv',
                           self.FILES) == 'bob/1_s1_R_1.00mA.csv'

    def test_none(self):
        assert find_listed('measurement_data/alice/other.csv', self.FILES) is None


class TestFitSweep:
    """Least squares by hand, on four points."""

    def test_a_voltage_sweep_fits_i_on_v(self):
        # x = V: 0..3, mean 1.5, Sxx = 5. y = I (mA): 1, 10, 21, 30, mean 15.5;
        # Sxy = 49e-3, so the slope is 9.8 mA/V and R = 1 / 9.8e-3.
        # Syy = 481e-6; residual 481e-6 - 9.8e-3 * 49e-3 = 0.8e-6 on 2 degrees
        # of freedom: s^2 = 0.4e-6, u(slope) = sqrt(0.4e-6 / 5).
        points = [(0.0, 1e-3, 'OK'), (1.0, 10e-3, 'OK'), (2.0, 21e-3, 'OK'),
                  (3.0, 30e-3, 'OK')]
        fit = fit_sweep(points, 'voltage')
        assert (fit['sourced'], fit['fit'], fit['n']) == ('voltage', 'I on V', 4)
        assert fit['R']['unit'] == 'Ω'
        assert fit['R']['value'] == pytest.approx(1 / 9.8e-3)
        u_slope = math.sqrt(0.4e-6 / 5)
        assert fit['R']['standard_error'] == pytest.approx(u_slope / 9.8e-3 ** 2)
        # Intercept 15.5e-3 - 9.8e-3 * 1.5 = 0.8 mA, the current at 0 V;
        # u = sqrt(s^2 (1/4 + 1.5^2 / 5)).
        assert fit['intercept']['unit'] == 'A'
        assert fit['intercept']['value'] == pytest.approx(0.8e-3)
        assert fit['intercept']['standard_error'] == pytest.approx(math.sqrt(0.4e-6 * 0.7))
        assert fit['r2'] == pytest.approx(49e-3 ** 2 / (5 * 481e-6))
        assert (fit['excluded_in_compliance'], fit['excluded_not_a_number']) == (0, 0)

    def test_a_current_sweep_fits_v_on_i_and_leaves_compliance_out(self):
        # V = 0.01 + 100 I exactly; the last point is the 5 V limit.
        points = [(0.01, 0.0, 'OK'), (0.11, 1e-3, 'OK'), (0.21, 2e-3, 'OK'),
                  (0.31, 3e-3, 'OK'), (5.0, 4e-3, 'COMP')]
        fit = fit_sweep(points, 'current')
        assert (fit['fit'], fit['n'], fit['excluded_in_compliance']) == ('V on I', 4, 1)
        assert fit['R']['value'] == pytest.approx(100.0)
        assert fit['R']['standard_error'] == pytest.approx(0.0, abs=1e-9)
        assert (fit['intercept']['unit'], fit['intercept']['meaning']) == ('V', 'V at I = 0')
        assert fit['intercept']['value'] == pytest.approx(0.01)
        assert fit['r2'] == pytest.approx(1.0)
        assert '1 point in compliance left out' in fit['headline']

    def test_two_points_give_no_standard_error(self):
        fit = fit_sweep([(0.0, 0.0, 'OK'), (1.0, 0.01, 'OK')], 'voltage')
        assert fit['R']['value'] == pytest.approx(100.0)
        assert fit['R']['standard_error'] is None

    def test_too_few_points_or_one_sourced_value_is_no_fit(self):
        assert fit_sweep([(1.0, 0.01, 'OK')], 'voltage')['R']['value'] is None
        same = fit_sweep([(1.0, 0.01, 'OK'), (1.0, 0.011, 'OK')], 'voltage')
        assert same['R']['value'] is None and 'one sourced value' in same['headline']

    def test_a_point_that_is_not_a_number_is_counted_out(self):
        fit = fit_sweep([(0.0, 0.0, 'OK'), (1.0, None, 'OK'), (2.0, 0.02, 'OK'),
                         (3.0, float('nan'), 'OK')], 'voltage')
        assert (fit['n'], fit['excluded_not_a_number']) == (2, 2)


def _sweep_file(header, source_function):
    return (f"# mode: sweep\n# params.source_function: {source_function}\n"
            f"# units: ,V,A,\n{header}\n"
            "0,0.0,0.0,OK\n1,0.1,0.001,OK\n2,0.2,0.002,OK\n3,5.0,0.003,COMP\n")


class TestResult:
    def test_resistance_mean_and_sd_without_the_rows_in_compliance(self):
        # 100, 102 and 104; 98 was in compliance.
        result = summarise(RESISTANCE)['result']
        assert result['mode'] == 'resistance'
        assert result['R'] == {'unit': 'Ω', 'mean': 102.0, 'sd': pytest.approx(2.0), 'n': 3,
                               'u_inst_per_reading': pytest.approx(0.06)}
        assert result['excluded_in_compliance'] == 1
        assert result['headline'] == ("R = 102 ± 2 Ω (mean ± SD of 3 readings; 1 row in "
                                      "compliance left out)")
        assert 'k = 1' in result['uncertainty']

    def test_source_v_gives_the_current_and_the_resistance(self):
        text = ("# mode: source_v\n# units: s,V,A,Ω,A,Ω,,\n"
                "elapsed_s,V_set,I_meas,R_calc,I_unc_A,R_calc_unc_ohm,compliance,event\n"
                "0.1,1,0.01,100,1e-6,0.02,OK,\n0.2,1,0.0102,98.04,1e-6,0.02,OK,\n")
        result = summarise(text)['result']
        assert result['I']['unit'] == 'A' and result['I']['mean'] == pytest.approx(0.0101)
        assert result['R']['unit'] == 'Ω' and result['R']['n'] == 2

    @pytest.mark.parametrize('header, source_function', [
        ('point,V_source,I_meas,compliance', 'voltage'),
        ('point,V_meas,I_source,compliance', 'current'),
        # Written before the names followed the source: the params say.
        ('point,V_source,I_meas,compliance', 'current'),
    ])
    def test_a_sweep_is_fitted_from_either_header(self, header, source_function):
        result = summarise(_sweep_file(header, source_function))['result']
        assert result['mode'] == 'sweep'
        assert result['sourced'] == source_function
        assert result['fit'] == ('I on V' if source_function == 'voltage' else 'V on I')
        assert result['R']['value'] == pytest.approx(100.0)
        assert (result['n'], result['excluded_in_compliance']) == (3, 1)
        assert 'scatter' in result['uncertainty']

    def test_a_four_point_run_takes_its_spot_statistics(self):
        text = ("# mode: four_point\n# units: s,V,A,Ω,Ω/□,Ω·cm,S/cm,V,A,,\n"
                "elapsed_s,V,I,V_over_I,Rs_ohm_sq,rho_ohm_cm,sigma_S_cm,V_unc_V,I_unc_A,"
                "compliance,event\n"
                "0.1,0.012,0.001,12,56.3,nan,nan,1e-6,1e-7,OK,\n"
                "# --- run completed ---\n"
                "# spot_stats.n: 10\n# spot_stats.n_excluded: 2\n"
                "# spot_stats.rs.n: 10\n# spot_stats.rs.mean: 56.31\n"
                "# spot_stats.rs.sd: 0.117\n# spot_stats.rs.u_stat: 0.037\n"
                "# spot_stats.rs.u_inst: 1.358\n# spot_stats.rs.u_total: 1.3585\n"
                "# spot_stats.rho.n: 0\n# spot_stats.rho.mean: nan\n")
        result = summarise(text)['result']
        assert result['Rs'] == {'unit': 'Ω/□', 'mean': 56.31, 'sd': 0.117, 'u_stat': 0.037,
                                'u_inst': 1.358, 'u_total': 1.3585, 'n': 10}
        # No thickness: no rho, and no sigma.
        assert 'rho' not in result and 'sigma' not in result
        assert result['excluded_in_compliance'] == 2
        assert result['headline'] == ("Rs = 56.31 ± 1.3585 Ω/□ (mean ± u_total, n = 10; "
                                      "2 samples in compliance left out)")
        assert 'u_inst' in result['uncertainty'] and 'k = 1' in result['uncertainty']

    def test_a_four_point_run_still_going_is_summarised_from_its_rows(self):
        text = ("# mode: four_point\n# units: s,V,A,Ω,Ω/□,Ω·cm,S/cm,V,A,,\n"
                "elapsed_s,V,I,V_over_I,Rs_ohm_sq,rho_ohm_cm,sigma_S_cm,V_unc_V,I_unc_A,"
                "compliance,event\n"
                "0.1,0,0,0,50,0.5,2,0,0,OK,\n0.2,0,0,0,52,0.52,1.9,0,0,OK,\n")
        result = summarise(text)['result']
        assert result['Rs'] == {'unit': 'Ω/□', 'mean': 51.0, 'sd': pytest.approx(math.sqrt(2)),
                                'n': 2}
        assert result['rho']['unit'] == 'Ω·cm'
        assert 'has not finished' in result['note']

    def test_a_van_der_pauw_result_with_units_and_the_homogeneity_criterion(self):
        text = ("# mode: vdp\nelapsed_s,geometry\n1.0,R_21_34\n# --- run completed ---\n"
                "# vdp_result.sheet_resistance: 0.00565\n"
                "# vdp_result.sheet_resistance_uncertainty: 4e-06\n"
                "# vdp_result.rho_avg: 5.65e-05\n# vdp_result.rho_avg_uncertainty: 4e-08\n"
                "# vdp_result.thickness_cm: 0.01\n# vdp_result.homogeneous: true\n"
                "# vdp_result.asymmetry_pct: 1.4\n")
        result = summarise(text)['result']
        assert result['R_s'] == {'unit': 'Ω/□', 'value': 0.00565, 'u': 4e-06}
        assert result['rho'] == {'unit': 'Ω·cm', 'value': 5.65e-05, 'u': 4e-08,
                                 'thickness_cm': 0.01}
        homogeneity = result['homogeneity']
        assert (homogeneity['homogeneous'], homogeneity['asymmetry_pct'],
                homogeneity['threshold_pct']) == (True, 1.4, 10.0)
        assert homogeneity['criterion'].startswith('|rho_A - rho_B| / rho_avg <= 10 %')
        assert result['headline'] == ("R_s = 0.00565 ± 4e-06 Ω/□; rho = 5.65e-05 ± 4e-08 "
                                      "Ω·cm; homogeneous by ASTM F76 (asymmetry 1.4 % <= 10 %)")
        assert 'k = 1' in result['uncertainty']

    def test_the_threshold_is_the_one_the_calculation_applies(self):
        from resistamet_gui import calculations_vdp
        result = summarise("# mode: vdp\na\n1\n# --- run completed ---\n"
                           "# vdp_result.sheet_resistance: 1.0\n"
                           "# vdp_result.homogeneous: false\n"
                           "# vdp_result.asymmetry_pct: 12.0\n"
                           "# vdp_result.thickness_cm: 0.0\n# vdp_result.rho_avg: nan\n")
        result = result['result']
        assert result['homogeneity']['threshold_pct'] == \
            calculations_vdp.F76_HOMOGENEITY_TOLERANCE_PCT
        assert 'NOT homogeneous' in result['headline'] and '12 % > 10 %' in result['headline']
        assert result['rho'] is None and 'no thickness' in result['rho_note']

    def test_a_van_der_pauw_run_without_a_result_says_so(self):
        assert 'no result' in summarise(VDP)['result']['note']

    def test_a_file_of_no_known_mode_has_no_result(self):
        assert 'result' not in summarise('elapsed_s\n0.1\n')

    def test_the_result_is_additive(self):
        summary = summarise(RESISTANCE)
        assert {'metadata', 'end', 'finalized', 'rows', 'columns', 'compliance', 'marks',
                'marks_total', 'result'} == set(summary)

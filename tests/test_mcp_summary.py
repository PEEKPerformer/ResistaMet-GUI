"""Run files as statistics, from literal file text with hand-computed answers.

No SDK and no network: ``summary`` is pure.
"""
import math

import pytest

from resistamet_gui.mcp.summary import (MAX_ROWS, column_stats, find_listed, read_slice,
                                        summarise)

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

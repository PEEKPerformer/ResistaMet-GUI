"""Assembling a map from run files, against files written by the exporters."""
import json
import math
import os

import pytest

from resistamet_gui.data_export import CsvExporter, Hdf5Exporter
from resistamet_gui.session.spot_map import (
    assemble_map, inter_spot_statistics, list_map_ids, map_summary_path,
    write_map_summary,
)


def _quantity(mean, n=5):
    return {'n': n, 'mean': mean, 'sd': mean * 0.01, 'rsd_pct': 1.0,
            'u_stat': mean * 0.004, 'u_inst': mean * 0.001, 'u_total': mean * 0.0042}


def _write_run(directory, stamp, map_id, index, rs_mean, label=None, *, mode='four_point',
               footer=True, n=5, exporter=CsvExporter, tag='4PP', compression='never',
               position=None, started_at=None, base_name=None):
    """One finished run file, as ContinuousRun would have left it."""
    meta = {
        'user': 'alice', 'sample': 'wafer7', 'mode': mode,
        'started_at': started_at or f'2026-09-19T12:{stamp:02d}:00',
    }
    if map_id is not None:
        meta['spot'] = {'map_id': map_id, 'index': index, 'label': label or f'spot {index}',
                        'x_mm': None, 'y_mm': None, 'angle_deg': 0.0}
        if position is not None:
            meta['spot'].update(position)
    base = directory / (base_name or f"{1000 + stamp}_wafer7_{tag}_1mA")
    kwargs = {'compression': compression} if exporter is CsvExporter else {}
    exp = exporter(base, meta, ['elapsed_s', 'Rs_ohm_sq'], ['s', 'ohm/sq'], **kwargs)
    exp.write_row([0.0, rs_mean])
    end = {'ended_at': 'later', 'total_samples': n}
    if footer:
        end['spot_stats'] = {
            'n': n, 'n_excluded': 0,
            'rs': _quantity(rs_mean, n) if n else {'n': 0, 'mean': float('nan')},
            'rho': _quantity(rs_mean * 1e-4, n), 'sigma': {'n': 0, 'mean': float('nan')},
        }
        exp.finalize(end)
    else:
        exp.flush()
        exp._csv_file.close()      # a run that died: rows, no end block
    return exp.output_paths[0]


class TestInterSpotStatistics:
    def test_by_hand(self):
        """100, 110, 120: mean 110, deviations -10, 0, 10, variance 100."""
        stats = inter_spot_statistics([100.0, 110.0, 120.0])
        assert (stats.n, stats.mean, stats.sd) == (3, 110.0, 10.0)
        assert stats.rsd_pct == pytest.approx(100.0 / 11.0)

    def test_nan_and_missing_means_do_not_count(self):
        stats = inter_spot_statistics([100.0, float('nan'), None, 120.0])
        assert (stats.n, stats.mean) == (2, 110.0)
        assert stats.sd == pytest.approx(math.sqrt(200.0))

    def test_one_spot_has_no_spread(self):
        stats = inter_spot_statistics([100.0])
        assert (stats.n, stats.mean, stats.sd, stats.rsd_pct) == (1, 100.0, None, None)

    def test_no_spots(self):
        assert inter_spot_statistics([]).model_dump() == {
            'n': 0, 'mean': None, 'sd': None, 'rsd_pct': None}


class TestAssembleMap:
    def test_spots_in_index_order_with_their_statistics(self, tmp_path):
        _write_run(tmp_path, 3, 'wafer7', 2, 120.0)
        _write_run(tmp_path, 1, 'wafer7', 0, 100.0, label='centre')
        _write_run(tmp_path, 2, 'wafer7', 1, 110.0)
        found = assemble_map(tmp_path, 'wafer7')
        assert [spot.index for spot in found.spots] == [0, 1, 2]
        assert found.spots[0].label == 'centre'
        assert found.spots[0].file == '1001_wafer7_4PP_1mA.csv'
        assert found.spots[0].sample == 'wafer7'
        assert found.spots[1].stats.rs.mean == 110.0
        assert found.spots[1].stats.rs.u_total == pytest.approx(110.0 * 0.0042)
        assert found.spots[1].stats.sigma.n == 0
        assert (found.rs.n, found.rs.mean, found.rs.sd) == (3, 110.0, 10.0)
        assert found.rho.mean == pytest.approx(110.0e-4)
        assert found.sigma.n == 0

    def test_the_newer_run_of_an_index_wins(self, tmp_path):
        _write_run(tmp_path, 1, 'wafer7', 0, 100.0)
        _write_run(tmp_path, 2, 'wafer7', 1, 999.0)
        _write_run(tmp_path, 3, 'wafer7', 2, 120.0)
        _write_run(tmp_path, 4, 'wafer7', 1, 110.0, label='spot 1 again')
        found = assemble_map(tmp_path, 'wafer7')
        assert [spot.stats.rs.mean for spot in found.spots] == [100.0, 110.0, 120.0]
        assert found.spots[1].label == 'spot 1 again'
        assert found.spots[1].superseded == ['1002_wafer7_4PP_1mA.csv']
        assert found.rs.sd == 10.0

    def test_other_maps_and_runs_without_a_spot_are_left_out(self, tmp_path):
        _write_run(tmp_path, 1, 'wafer7', 0, 100.0)
        _write_run(tmp_path, 2, 'wafer8', 0, 500.0)
        _write_run(tmp_path, 3, None, 0, 700.0)
        found = assemble_map(tmp_path, 'wafer7')
        assert [spot.stats.rs.mean for spot in found.spots] == [100.0]

    def test_a_run_that_never_finished_does_not_displace_a_good_one(self, tmp_path):
        _write_run(tmp_path, 1, 'wafer7', 0, 100.0)
        _write_run(tmp_path, 2, 'wafer7', 0, 555.0, footer=False)
        found = assemble_map(tmp_path, 'wafer7')
        assert [spot.stats.rs.mean for spot in found.spots] == [100.0]
        assert found.spots[0].superseded == []
        assert [(run.file, run.reason) for run in found.skipped] == [
            ('1002_wafer7_4PP_1mA.csv', 'no footer: the run did not finish')]

    def test_a_run_with_no_valid_sample_is_skipped(self, tmp_path):
        _write_run(tmp_path, 1, 'wafer7', 0, 100.0)
        _write_run(tmp_path, 2, 'wafer7', 0, 0.0, n=0)
        found = assemble_map(tmp_path, 'wafer7')
        assert [spot.stats.rs.mean for spot in found.spots] == [100.0]
        assert [run.reason for run in found.skipped] == ['no valid sample']

    def test_an_id_that_looks_like_a_number_still_matches(self, tmp_path):
        _write_run(tmp_path, 1, '12_3', 0, 100.0, label='007')
        found = assemble_map(tmp_path, '12_3')
        assert [spot.label for spot in found.spots] == ['007']
        assert assemble_map(tmp_path, '123').spots == []

    def test_the_position_effect_comes_along(self, tmp_path):
        _write_run(tmp_path, 1, 'wafer7', 0, 100.0, position={
            'x_mm': 20.0, 'y_mm': 0.0, 'relative_error': 0.024, 'edge_clearance_s': 5.0})
        spot = assemble_map(tmp_path, 'wafer7').spots[0]
        assert (spot.x_mm, spot.y_mm, spot.angle_deg) == (20.0, 0.0, 0.0)
        assert (spot.relative_error, spot.edge_clearance_s) == (0.024, 5.0)

    def test_gzipped_runs_are_read(self, tmp_path):
        path = _write_run(tmp_path, 1, 'wafer7', 0, 100.0, compression='always')
        assert path.name.endswith('.csv.gz')
        assert assemble_map(tmp_path, 'wafer7').spots[0].file == path.name

    def test_hdf5_runs_are_read(self, tmp_path):
        pytest.importorskip("h5py")
        _write_run(tmp_path, 1, 'wafer7', 0, 100.0, exporter=Hdf5Exporter)
        _write_run(tmp_path, 2, 'wafer7', 1, 120.0, exporter=Hdf5Exporter)
        found = assemble_map(tmp_path, 'wafer7')
        assert [spot.file for spot in found.spots] == [
            '1001_wafer7_4PP_1mA.h5', '1002_wafer7_4PP_1mA.h5']
        assert found.rs.mean == 110.0
        assert found.spots[0].x_mm is None
        assert found.spots[0].stats.sigma.n == 0

    def test_files_of_other_modes_are_not_opened(self, tmp_path):
        _write_run(tmp_path, 1, 'wafer7', 0, 100.0, tag='R', mode='resistance')
        assert assemble_map(tmp_path, 'wafer7').spots == []

    def test_subdirectories_are_not_searched(self, tmp_path):
        (tmp_path / 'bob').mkdir()
        _write_run(tmp_path / 'bob', 1, 'wafer7', 0, 100.0)
        assert assemble_map(tmp_path, 'wafer7').spots == []

    def test_an_empty_or_missing_directory_is_an_empty_map(self, tmp_path):
        found = assemble_map(tmp_path / 'nowhere', 'wafer7')
        assert (found.map_id, found.spots, found.rs.n) == ('wafer7', [], 0)

    @pytest.mark.parametrize("map_id", ['../wafer7', 'a/b', '..', '', 'a.b', 'x' * 65,
                                        'wafer7\n', '\nwafer7'])
    def test_an_id_that_could_build_a_path_is_refused(self, tmp_path, map_id):
        with pytest.raises(ValueError):
            assemble_map(tmp_path, map_id)
        with pytest.raises(ValueError):
            map_summary_path(tmp_path, map_id)
        with pytest.raises(ValueError):
            write_map_summary(tmp_path, map_id)


class TestOneBadFileDoesNotBreakTheMap:
    def _corrupt(self, path, old, new):
        text = path.read_text()
        assert old in text
        path.write_text(text.replace(old, new))

    def test_a_header_value_of_the_wrong_type(self, tmp_path):
        _write_run(tmp_path, 1, 'wafer7', 0, 100.0)
        bad = _write_run(tmp_path, 2, 'wafer7', 1, 120.0, position={'x_mm': 1.5, 'y_mm': 2.5})
        self._corrupt(bad, '# spot.x_mm: 1.5', '# spot.x_mm: abc')

        found = assemble_map(tmp_path, 'wafer7')
        assert [spot.index for spot in found.spots] == [0]
        assert [(run.file, run.reason.split(':')[0]) for run in found.skipped] == [
            (bad.name, 'unreadable spot block')]
        assert 'x_mm' in found.skipped[0].reason
        assert write_map_summary(tmp_path, 'wafer7').exists()

    def test_a_bad_newer_run_does_not_displace_a_good_older_one(self, tmp_path):
        good = _write_run(tmp_path, 1, 'wafer7', 0, 100.0)
        bad = _write_run(tmp_path, 2, 'wafer7', 0, 120.0)
        self._corrupt(bad, '# spot_stats.rs.mean: 120.0', '# spot_stats.rs.mean: lots')
        found = assemble_map(tmp_path, 'wafer7')
        assert [spot.file for spot in found.spots] == [good.name]
        assert [run.file for run in found.skipped] == [bad.name]

    def test_an_index_that_is_not_a_number(self, tmp_path):
        bad = _write_run(tmp_path, 1, 'wafer7', 0, 100.0)
        self._corrupt(bad, '# spot.index: 0', '# spot.index: first')
        found = assemble_map(tmp_path, 'wafer7')
        assert found.spots == []
        assert [run.file for run in found.skipped] == [bad.name]

    def test_hdf5_without_h5py_is_said_not_dropped(self, tmp_path, monkeypatch):
        from resistamet_gui.session import spot_map

        def no_h5py(path):
            raise ImportError("No module named 'h5py'")
        monkeypatch.setattr(spot_map, '_read_hdf5_attributes', no_h5py)
        _write_run(tmp_path, 1, 'wafer7', 0, 100.0)
        (tmp_path / '1002_wafer7_4PP_1mA.h5').write_bytes(b'not opened')

        found = assemble_map(tmp_path, 'wafer7')
        assert [spot.index for spot in found.spots] == [0]
        assert [(run.file, run.reason) for run in found.skipped] == [
            ('1002_wafer7_4PP_1mA.h5', 'h5py not installed')]
        assert list_map_ids(tmp_path) == ['wafer7']

    def test_a_file_that_cannot_be_read_at_all(self, tmp_path):
        _write_run(tmp_path, 1, 'wafer7', 0, 100.0)
        (tmp_path / '1002_wafer7_4PP_1mA.csv.gz').write_bytes(b'this is not gzip')
        found = assemble_map(tmp_path, 'wafer7')
        assert [spot.index for spot in found.spots] == [0]
        assert [run.reason.split(':')[0] for run in found.skipped] == ['unreadable']


class TestWhichRunIsNewest:
    def _files(self, found):
        return found.spots[0].file, found.spots[0].superseded

    def test_the_stamp_in_the_name_beats_the_local_start_time(self, tmp_path):
        """Across the end of daylight saving the later run has the earlier
        local time; the Unix stamp in the file name is not fooled."""
        _write_run(tmp_path, 1, 'wafer7', 0, 100.0, started_at='2026-11-01T01:50:00')
        _write_run(tmp_path, 2, 'wafer7', 0, 120.0, started_at='2026-11-01T01:10:00')
        assert self._files(assemble_map(tmp_path, 'wafer7')) == (
            '1002_wafer7_4PP_1mA.csv', ['1001_wafer7_4PP_1mA.csv'])

    def test_aware_and_naive_start_times_can_be_compared(self, tmp_path):
        _write_run(tmp_path, 1, 'wafer7', 0, 100.0, started_at='2026-09-19T12:00:00+02:00')
        _write_run(tmp_path, 2, 'wafer7', 0, 120.0, started_at='2026-09-19T12:00:00')
        _write_run(tmp_path, 3, 'wafer7', 0, 140.0, started_at='garbage')
        assert self._files(assemble_map(tmp_path, 'wafer7'))[0] == '1003_wafer7_4PP_1mA.csv'

    def test_within_one_second_the_start_time_decides(self, tmp_path):
        _write_run(tmp_path, 1, 'wafer7', 0, 100.0, base_name='1001_wafer7_4PP_1mA-2',
                   started_at='2026-09-19T12:00:00.100000')
        _write_run(tmp_path, 1, 'wafer7', 0, 120.0, base_name='1001_wafer7_4PP_1mA',
                   started_at='2026-09-19T12:00:00.900000')
        assert self._files(assemble_map(tmp_path, 'wafer7'))[0] == '1001_wafer7_4PP_1mA.csv'

    def test_then_the_repeat_number_the_exporter_added(self, tmp_path):
        """'-10' is newer than '-9' and than the unnumbered first; by name it
        would sort before '-2'."""
        same = '2026-09-19T12:00:00'
        for suffix, rs in (('', 100.0), ('-2', 110.0), ('-9', 120.0), ('-10', 130.0)):
            _write_run(tmp_path, 1, 'wafer7', 0, rs, started_at=same,
                       base_name=f'1001_wafer7_4PP_0.10mA{suffix}')
        newest, superseded = self._files(assemble_map(tmp_path, 'wafer7'))
        assert newest == '1001_wafer7_4PP_0.10mA-10.csv'
        assert superseded == ['1001_wafer7_4PP_0.10mA-9.csv', '1001_wafer7_4PP_0.10mA-2.csv',
                              '1001_wafer7_4PP_0.10mA.csv']

    def test_a_renamed_file_is_placed_by_its_start_time(self, tmp_path):
        from datetime import datetime
        early = datetime.fromtimestamp(1001).isoformat()
        late = datetime.fromtimestamp(1003).isoformat()
        _write_run(tmp_path, 2, 'wafer7', 0, 100.0, started_at=early)
        _write_run(tmp_path, 9, 'wafer7', 0, 120.0, started_at=late,
                   base_name='redo_of_wafer7_4PP_1mA')
        assert self._files(assemble_map(tmp_path, 'wafer7'))[0] == 'redo_of_wafer7_4PP_1mA.csv'


class TestListMapIds:
    def test_sorted_and_distinct(self, tmp_path):
        _write_run(tmp_path, 1, 'wafer8', 0, 100.0)
        _write_run(tmp_path, 2, 'wafer7', 0, 100.0)
        _write_run(tmp_path, 3, 'wafer7', 1, 100.0)
        _write_run(tmp_path, 4, None, 0, 100.0)
        assert list_map_ids(tmp_path) == ['wafer7', 'wafer8']

    def test_missing_directory(self, tmp_path):
        assert list_map_ids(tmp_path / 'nowhere') == []


class TestWriteMapSummary:
    def test_writes_json_beside_the_runs(self, tmp_path):
        _write_run(tmp_path, 1, 'wafer7', 0, 100.0)
        _write_run(tmp_path, 2, 'wafer7', 1, 120.0)
        path = write_map_summary(tmp_path, 'wafer7')
        assert path == tmp_path / 'wafer7_map.json'
        summary = json.loads(path.read_text())
        assert summary['map_id'] == 'wafer7'
        assert [spot['index'] for spot in summary['spots']] == [0, 1]
        assert summary['rs']['mean'] == 110.0
        # NaN has no JSON form; an absent number is null.
        assert summary['spots'][0]['stats']['sigma']['mean'] is None

    def test_replaces_the_previous_summary_and_leaves_no_temporary_file(self, tmp_path):
        _write_run(tmp_path, 1, 'wafer7', 0, 100.0)
        write_map_summary(tmp_path, 'wafer7')
        _write_run(tmp_path, 2, 'wafer7', 1, 120.0)
        path = write_map_summary(tmp_path, 'wafer7')
        assert len(json.loads(path.read_text())['spots']) == 2
        assert sorted(p.name for p in tmp_path.iterdir() if 'map' in p.name) == ['wafer7_map.json']

    def test_the_run_files_are_not_touched(self, tmp_path):
        runs = [_write_run(tmp_path, 1, 'wafer7', 0, 100.0),
                _write_run(tmp_path, 2, 'wafer7', 0, 120.0)]
        before = [(p.read_bytes(), os.stat(p).st_mtime_ns) for p in runs]
        write_map_summary(tmp_path, 'wafer7')
        assert [(p.read_bytes(), os.stat(p).st_mtime_ns) for p in runs] == before

    def test_a_failed_write_keeps_the_previous_summary(self, tmp_path, monkeypatch):
        _write_run(tmp_path, 1, 'wafer7', 0, 100.0)
        path = write_map_summary(tmp_path, 'wafer7')
        previous = path.read_text()

        def refuse(src, dst):
            raise OSError("disk full")
        monkeypatch.setattr(os, 'replace', refuse)
        with pytest.raises(OSError):
            write_map_summary(tmp_path, 'wafer7')
        assert path.read_text() == previous
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith('.tmp')] == []

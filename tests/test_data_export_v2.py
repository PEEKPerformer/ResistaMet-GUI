"""Tests for the v2.0 export pipeline (CsvExporter, Hdf5Exporter, parse_metadata, factory)."""

import gzip
from pathlib import Path

import pytest

from resistamet_gui.data_export import (
    CsvExporter,
    Hdf5Exporter,
    LegacyDualExporter,
    build_metadata,
    get_column_config,
    make_exporter,
    parse_metadata,
)


# --------------------------------- Fixtures ---------------------------------


@pytest.fixture
def base_path(tmp_path):
    return tmp_path / "run_001"


@pytest.fixture
def basic_meta():
    return {
        'user': 'brenden',
        'sample': 'Si_wafer_001',
        'mode': 'resistance',
        'started_at': '2026-05-13T14:22:01',
        'params': {
            'test_current_A': 1.0e-3,
            'voltage_compliance_V': 5.0,
            'measurement_type': '2-wire',
            'auto_range': True,
        },
    }


# --------------------------------- CsvExporter ------------------------------


class TestCsvExporter:
    def test_writes_metadata_header_and_columns(self, base_path, basic_meta):
        exp = CsvExporter(base_path, basic_meta, ['elapsed_s', 'R_ohm'], ['s', 'Ω'])
        exp.write_row([0.0, 1.05])
        exp.finalize()

        text = exp.output_paths[0].read_text(encoding='utf-8')
        assert "# resistamet_format_version: 2.0" in text
        assert "# user: brenden" in text
        assert "# params.test_current_A: 0.001" in text
        assert "# units: s,Ω" in text
        # Column header on the first non-comment line.
        lines = [ln for ln in text.splitlines() if ln and not ln.startswith('#')]
        assert lines[0] == "elapsed_s,R_ohm"
        assert lines[1].startswith("0,")

    def test_streams_rows_immediately(self, base_path, basic_meta):
        exp = CsvExporter(base_path, basic_meta, ['elapsed_s', 'R_ohm'])
        exp.write_row([0.0, 1.05])
        exp.flush()
        # The CSV is itself the crash-recovery artifact: partial rows must be on
        # disk before finalize().
        text = exp.csv_path.read_text(encoding='utf-8')
        assert "0,1.05" in text
        exp.finalize()

    def test_appends_end_metadata_block(self, base_path, basic_meta):
        exp = CsvExporter(base_path, basic_meta, ['elapsed_s', 'R_ohm'])
        exp.write_row([0.0, 1.05])
        exp.finalize({'ended_at': '2026-05-13T14:30:00', 'total_samples': 1})
        text = exp.output_paths[0].read_text(encoding='utf-8')
        assert "# --- run completed ---" in text
        assert "# ended_at: 2026-05-13T14:30:00" in text
        assert "# total_samples: 1" in text

    def test_row_count(self, base_path, basic_meta):
        exp = CsvExporter(base_path, basic_meta, ['elapsed_s', 'R_ohm'])
        for i in range(5):
            exp.write_row([float(i), float(i) * 1.05])
        assert exp.row_count == 5
        exp.finalize()


# --------------------------------- Compression ------------------------------


class TestCompressionPolicy:
    def test_never_keeps_csv(self, base_path, basic_meta):
        exp = CsvExporter(base_path, basic_meta, ['elapsed_s', 'R_ohm'],
                          compression='never')
        exp.write_row([0.0, 1.05])
        exp.finalize()
        assert exp.output_paths[0].suffix == '.csv'
        assert not exp.csv_path.with_suffix('.csv.gz').exists()

    def test_always_compresses(self, base_path, basic_meta):
        exp = CsvExporter(base_path, basic_meta, ['elapsed_s', 'R_ohm'],
                          compression='always')
        exp.write_row([0.0, 1.05])
        exp.finalize()
        final = exp.output_paths[0]
        assert final.name.endswith('.csv.gz')
        assert not exp.csv_path.exists()  # original removed
        with gzip.open(final, 'rt', encoding='utf-8') as f:
            text = f.read()
        assert "# user: brenden" in text
        assert "0,1.05" in text

    def test_auto_below_threshold_keeps_csv(self, base_path, basic_meta):
        exp = CsvExporter(base_path, basic_meta, ['elapsed_s', 'R_ohm'],
                          compression='auto', threshold_mb=100.0)
        exp.write_row([0.0, 1.05])
        exp.finalize()
        assert exp.output_paths[0].suffix == '.csv'

    def test_auto_above_threshold_compresses(self, base_path, basic_meta):
        exp = CsvExporter(base_path, basic_meta, ['elapsed_s', 'R_ohm'],
                          compression='auto', threshold_mb=0.0)
        exp.write_row([0.0, 1.05])
        exp.finalize()
        assert exp.output_paths[0].name.endswith('.csv.gz')

    def test_on_compress_callback_fires(self, base_path, basic_meta):
        calls = []

        def cb(orig, gz, orig_mb, gz_mb):
            calls.append((orig.name, gz.name, orig_mb, gz_mb))

        exp = CsvExporter(base_path, basic_meta, ['elapsed_s', 'R_ohm'],
                          compression='always', on_compress=cb)
        exp.write_row([0.0, 1.05])
        exp.finalize()
        assert len(calls) == 1
        assert calls[0][0].endswith('.csv')
        assert calls[0][1].endswith('.csv.gz')


class TestLargeFileNotification:
    """The passive nudge that fires when a finalized run leaves a big uncompressed .csv."""

    def test_fires_when_csv_exceeds_threshold(self, base_path, basic_meta):
        calls = []
        exp = CsvExporter(
            base_path, basic_meta, ['elapsed_s', 'R_ohm'],
            compression='never',
            on_large_file=lambda p, mb: calls.append((p.name, mb)),
            large_file_notify_mb=0.0,  # any non-empty file counts as "large"
        )
        exp.write_row([0.0, 1.05])
        exp.finalize()
        assert len(calls) == 1
        assert calls[0][0].endswith('.csv')
        assert calls[0][1] > 0

    def test_silent_below_threshold(self, base_path, basic_meta):
        calls = []
        exp = CsvExporter(
            base_path, basic_meta, ['elapsed_s', 'R_ohm'],
            compression='never',
            on_large_file=lambda p, mb: calls.append((p.name, mb)),
            large_file_notify_mb=100.0,  # well above anything this test writes
        )
        exp.write_row([0.0, 1.05])
        exp.finalize()
        assert calls == []

    def test_silent_when_compressed(self, base_path, basic_meta):
        # Compressed final artifacts already surface via on_compress — the
        # large-file nudge must stay quiet to avoid double-pinging the user.
        calls = []
        exp = CsvExporter(
            base_path, basic_meta, ['elapsed_s', 'R_ohm'],
            compression='always',
            on_large_file=lambda p, mb: calls.append((p.name, mb)),
            large_file_notify_mb=0.0,
        )
        exp.write_row([0.0, 1.05])
        exp.finalize()
        assert calls == []


# --------------------------------- parse_metadata ---------------------------


class TestParseMetadata:
    def test_roundtrips_plain_csv(self, base_path, basic_meta):
        exp = CsvExporter(base_path, basic_meta, ['elapsed_s', 'R_ohm'], ['s', 'Ω'])
        exp.write_row([0.0, 1.05])
        exp.finalize({'ended_at': '2026-05-13T14:30:00', 'total_samples': 1})

        meta = parse_metadata(exp.output_paths[0])
        assert meta['user'] == 'brenden'
        assert meta['sample'] == 'Si_wafer_001'
        assert meta['params.test_current_A'] == 0.001
        assert meta['params.auto_range'] is True
        assert meta['units'] == ['s', 'Ω']
        assert meta['ended_at'] == '2026-05-13T14:30:00'
        assert meta['total_samples'] == 1

    def test_roundtrips_gzipped_csv(self, base_path, basic_meta):
        exp = CsvExporter(base_path, basic_meta, ['elapsed_s', 'R_ohm'],
                          compression='always')
        exp.write_row([0.0, 1.05])
        exp.finalize({'ended_at': '2026-05-13T14:30:00'})
        meta = parse_metadata(exp.output_paths[0])
        assert meta['user'] == 'brenden'
        assert meta['ended_at'] == '2026-05-13T14:30:00'

    def test_preserves_nan(self, base_path):
        meta_in = {'user': 'x', 'sample': 's', 'mode': 'four_point',
                   'params': {'temperature_c': float('nan')}}
        exp = CsvExporter(base_path, meta_in, ['elapsed_s', 'V'])
        exp.finalize()
        meta = parse_metadata(exp.output_paths[0])
        import math
        assert math.isnan(meta['params.temperature_c'])


class TestParseMetadataTextKeys:
    """Identifiers must come back as written, not as the literal they resemble."""

    def _file(self, base_path):
        meta = {'spot': {'map_id': '12_3', 'label': 'true', 'index': 4}}
        exp = CsvExporter(base_path, meta, ['elapsed_s'], ['s'])
        exp.write_row([0.0])
        exp.finalize({'total_samples': 1})
        return exp.output_paths[0]

    def test_coercion_mangles_an_id_that_looks_like_a_number(self, base_path):
        parsed = parse_metadata(self._file(base_path))
        assert parsed['spot.map_id'] == 123
        assert parsed['spot.label'] is True

    def test_text_keys_come_back_as_written(self, base_path):
        parsed = parse_metadata(self._file(base_path),
                                text_keys=('spot.map_id', 'spot.label'))
        assert parsed['spot.map_id'] == '12_3'
        assert parsed['spot.label'] == 'true'
        assert parsed['spot.index'] == 4
        assert parsed['total_samples'] == 1


# --------------------------------- All six modes ----------------------------


@pytest.mark.parametrize("mode", [
    'resistance', 'source_v', 'source_i', 'four_point', 'sweep', 'vdp',
])
class TestAllModesMetadata:
    def _settings_for(self, mode):
        return {
            'measurement': {
                'sampling_rate': 10.0, 'nplc': 1, 'settling_time': 0.2,
                'gpib_address': 'GPIB0::24::INSTR',
                # resistance
                'res_test_current': 1e-3, 'res_voltage_compliance': 5.0,
                'res_measurement_type': '2-wire', 'res_auto_range': True,
                # source_v
                'vsource_voltage': 1.0, 'vsource_current_compliance': 0.1,
                'vsource_duration_hours': 1.0,
                # source_i
                'isource_current': 1e-3, 'isource_voltage_compliance': 5.0,
                'isource_duration_hours': 1.0,
                # four_point
                'fpp_current': 1e-4, 'fpp_voltage_compliance': 5.0,
                'fpp_spacing_cm': 0.1016, 'fpp_thickness_um': 0.0,
                'fpp_k_factor': 4.532, 'fpp_alpha': 1.0, 'fpp_model': 'thin_film',
                'fpp_samples': 0,
                # vdp
                'vdp_current': 1e-3, 'vdp_voltage_compliance': 5.0,
                'vdp_thickness_cm': 1e-4, 'vdp_settling_s': 0.2,
                'vdp_readings_per_polarity': 1,
                # sweep
                'sweep_source': 'voltage', 'sweep_start': 0.0, 'sweep_stop': 1.0,
                'sweep_step': 0.05, 'sweep_compliance': 0.1, 'sweep_delay': 0.01,
                'sweep_direction': 'up',
            }
        }

    def test_columns_and_metadata_match(self, base_path, mode):
        settings = self._settings_for(mode)
        columns, units = get_column_config(mode, settings['measurement'])
        meta = build_metadata('brenden', 'sample_A', mode, settings,
                              instrument_idn='Keithley 2400')
        exp = CsvExporter(base_path, meta, columns, units)
        exp.write_row([0.0] + [0.1] * (len(columns) - 1))
        exp.finalize()

        parsed = parse_metadata(exp.output_paths[0])
        assert parsed['mode'] == mode
        assert parsed['user'] == 'brenden'
        assert parsed['sample'] == 'sample_A'
        # Each mode contributes its own params block.
        param_keys = [k for k in parsed if k.startswith('params.')]
        assert param_keys, f"no params.* metadata for mode={mode}"


# --------------------------------- HDF5 -------------------------------------
# Skip the whole class when h5py isn't installed rather than at module scope
# (module-scope importorskip silently skips every test in this file).


class TestHdf5Exporter:
    def setup_method(self):
        pytest.importorskip("h5py")

    def test_writes_and_reads_back(self, base_path, basic_meta):
        import h5py
        exp = Hdf5Exporter(base_path, basic_meta, ['elapsed_s', 'R_ohm'], ['s', 'Ω'])
        exp.write_row([0.0, 1.05])
        exp.write_row([1.0, 1.04])
        exp.finalize({'total_samples': 2, 'ended_at': '2026-05-13T14:30:00'})

        with h5py.File(exp.output_paths[0], 'r') as f:
            ds = f['data']
            assert ds.shape == (2,)
            assert f.attrs['user'] == 'brenden'
            assert f.attrs['mode'] == 'resistance'
            assert f.attrs['ended_at'] == '2026-05-13T14:30:00'
            assert list(f.attrs['columns']) == ['elapsed_s', 'R_ohm']

    def test_gzip_compression_present(self, base_path, basic_meta):
        import h5py
        exp = Hdf5Exporter(base_path, basic_meta, ['elapsed_s', 'R_ohm'])
        exp.write_row([0.0, 1.05])
        exp.finalize()
        with h5py.File(exp.output_paths[0], 'r') as f:
            assert f['data'].compression == 'gzip'


# --------------------------------- Factory ----------------------------------


class TestMakeExporter:
    def test_defaults_to_csv(self, base_path, basic_meta):
        exp = make_exporter(base_path, basic_meta, ['elapsed_s', 'R_ohm'])
        assert isinstance(exp, CsvExporter)
        exp.finalize()

    def test_selects_hdf5(self, base_path, basic_meta):
        pytest.importorskip("h5py")
        exp = make_exporter(base_path, basic_meta, ['elapsed_s', 'R_ohm'],
                            output_settings={'format': 'hdf5'})
        assert isinstance(exp, Hdf5Exporter)
        exp.finalize()

    def test_selects_legacy_dual(self, base_path, basic_meta):
        exp = make_exporter(base_path, basic_meta, ['elapsed_s', 'R_ohm'],
                            output_settings={'format': 'csv+legacy_json'})
        assert isinstance(exp, LegacyDualExporter)
        exp.finalize()

    def test_unknown_format_falls_back_to_csv(self, base_path, basic_meta):
        exp = make_exporter(base_path, basic_meta, ['elapsed_s', 'R_ohm'],
                            output_settings={'format': 'parquet_pretty_please'})
        assert isinstance(exp, CsvExporter)
        exp.finalize()

    def test_threads_compression_settings(self, base_path, basic_meta):
        exp = make_exporter(
            base_path, basic_meta, ['elapsed_s', 'R_ohm'],
            output_settings={'format': 'csv', 'compression': 'always'},
        )
        exp.write_row([0.0, 1.05])
        exp.finalize()
        assert exp.output_paths[0].name.endswith('.csv.gz')


# ------------------------------- Spot block ---------------------------------


SPOT_BLOCK = {
    'map_id': 'wafer7', 'index': 2, 'label': 'rim', 'x_mm': 20.0, 'y_mm': 0.0,
    'angle_deg': 90.0,
    'sample': {'shape': 'circle', 'diameter_mm': 50.8, 'width_mm': None, 'length_mm': None},
    'position_correction': 'warn', 'edge_warn_pct': 1.0,
    'factor_here': 4.41, 'factor_centre': 4.5171, 'relative_error': 0.0243,
    'edge_clearance_s': 5.06,
}


def _four_point_meta(**kwargs):
    from datetime import datetime
    return build_metadata(user='alice', sample_name='wafer7', mode='four_point',
                          settings={'measurement': {'fpp_current': 1e-3}},
                          start_time=datetime(2026, 9, 19, 12, 0, 0), **kwargs)


class TestSpotBlock:
    def test_a_run_without_a_spot_has_no_spot_keys(self):
        assert 'spot' not in _four_point_meta()
        assert _four_point_meta(spot=None) == _four_point_meta()

    def test_the_block_is_the_only_difference(self):
        with_spot = _four_point_meta(spot=SPOT_BLOCK)
        assert with_spot.pop('spot') == SPOT_BLOCK
        assert with_spot == _four_point_meta()

    def test_csv_header_round_trip(self, base_path):
        exp = CsvExporter(base_path, _four_point_meta(spot=SPOT_BLOCK), ['elapsed_s'], ['s'])
        exp.write_row([0.0])
        exp.finalize({'total_samples': 1})
        text = exp.output_paths[0].read_text()
        assert '# spot.map_id: wafer7\n' in text
        assert '# spot.sample.shape: circle\n' in text
        assert '# spot.sample.width_mm: \n' in text

        parsed = parse_metadata(exp.output_paths[0])
        assert parsed['spot.map_id'] == 'wafer7'
        assert parsed['spot.index'] == 2
        assert parsed['spot.label'] == 'rim'
        assert (parsed['spot.x_mm'], parsed['spot.y_mm']) == (20.0, 0.0)
        assert parsed['spot.sample.diameter_mm'] == 50.8
        assert parsed['spot.sample.width_mm'] is None
        assert parsed['spot.relative_error'] == 0.0243
        assert parsed['spot.position_correction'] == 'warn'

    def test_hdf5_attributes(self, base_path):
        h5py = pytest.importorskip("h5py")
        exp = Hdf5Exporter(base_path, _four_point_meta(spot=SPOT_BLOCK), ['elapsed_s'], ['s'])
        exp.write_row([0.0])
        exp.finalize({'total_samples': 1})
        with h5py.File(exp.output_paths[0], 'r') as f:
            assert f.attrs['spot.map_id'] == 'wafer7'
            assert f.attrs['spot.index'] == 2
            assert f.attrs['spot.sample.diameter_mm'] == 50.8
            assert f.attrs['spot.sample.width_mm'] == ""
            assert f.attrs['spot.edge_clearance_s'] == 5.06


class TestClientBlock:
    """``settings['client']``: the program that asked for the run."""

    CLIENT = {'name': 'resistamet-desktop', 'version': '2.0.0-1'}

    def _meta(self, mode, settings):
        from datetime import datetime
        return build_metadata(user='alice', sample_name='wafer7', mode=mode,
                              settings=settings, start_time=datetime(2026, 9, 19, 12, 0, 0))

    @pytest.mark.parametrize("mode", ['resistance', 'four_point', 'sweep', 'vdp'])
    def test_the_block_is_the_only_difference(self, mode):
        plain = self._meta(mode, {'measurement': {}})
        with_client = self._meta(mode, {'measurement': {}, 'client': self.CLIENT})
        assert 'client' not in plain
        assert with_client.pop('client') == self.CLIENT
        assert with_client == plain

    def test_csv_header_lines(self, base_path):
        meta = self._meta('resistance', {'measurement': {}, 'client': self.CLIENT})
        exp = CsvExporter(base_path, meta, ['elapsed_s', 'R_ohm'])
        exp.finalize()
        text = exp.output_paths[0].read_text(encoding='utf-8')
        assert "# client.name: resistamet-desktop\n" in text
        assert "# client.version: 2.0.0-1\n" in text


# ------------------------------- File names ---------------------------------


class TestDottedBaseName:
    """A base name ends in the source value, which has a decimal point.

    ``Path.with_suffix`` read ``.10mA`` as a suffix and replaced it, so
    ``..._4PP_0.10mA`` was written as ``..._4PP_0.csv``.
    """

    DOTTED = '1789000000_wafer_4PP_0.10mA'

    def test_csv_keeps_the_whole_name(self, tmp_path, basic_meta):
        exp = CsvExporter(tmp_path / self.DOTTED, basic_meta, ['elapsed_s', 'R_ohm'])
        exp.finalize()
        assert exp.output_paths[0].name == self.DOTTED + '.csv'

    def test_gzipped_csv_keeps_the_whole_name(self, tmp_path, basic_meta):
        exp = CsvExporter(tmp_path / self.DOTTED, basic_meta, ['elapsed_s', 'R_ohm'],
                          compression='always')
        exp.write_row([0.0, 1.05])
        exp.finalize()
        assert exp.output_paths[0].name == self.DOTTED + '.csv.gz'
        assert [p.name for p in tmp_path.iterdir()] == [self.DOTTED + '.csv.gz']

    def test_hdf5_keeps_the_whole_name(self, tmp_path, basic_meta):
        pytest.importorskip("h5py")
        exp = Hdf5Exporter(tmp_path / self.DOTTED, basic_meta, ['elapsed_s', 'R_ohm'])
        exp.finalize()
        assert exp.output_paths[0].name == self.DOTTED + '.h5'

    def test_legacy_pair_keeps_the_whole_name(self, tmp_path, basic_meta):
        exp = LegacyDualExporter(tmp_path / self.DOTTED, basic_meta, ['elapsed_s', 'R_ohm'])
        exp.write_row([0.0, 1.05])
        exp.flush()
        assert (tmp_path / (self.DOTTED + '.json.tmp')).exists()
        exp.finalize()
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            self.DOTTED + '.csv', self.DOTTED + '.json']


class TestExistingFilesAreNeverOpenedForWriting:
    """The stamp in a base name has one-second resolution, so a second run of
    the same sample in the same second asks for the same base path. It gets
    ``name-2`` and the first run's file is left exactly as it was."""

    COLUMNS = ['elapsed_s', 'R_ohm']

    def test_second_csv_run_gets_a_new_name(self, base_path, basic_meta):
        first = CsvExporter(base_path, basic_meta, self.COLUMNS)
        first.write_row([0.0, 1.05])
        first.finalize({'total_samples': 1})
        before = first.output_paths[0].read_bytes()

        second = CsvExporter(base_path, basic_meta, self.COLUMNS)
        second.write_row([0.0, 2.10])
        second.finalize({'total_samples': 1})

        assert first.output_paths[0].name == 'run_001.csv'
        assert second.output_paths[0].name == 'run_001-2.csv'
        assert first.output_paths[0].read_bytes() == before

    def test_a_run_still_being_written_is_not_truncated(self, base_path, basic_meta):
        first = CsvExporter(base_path, basic_meta, self.COLUMNS)
        first.write_row([0.0, 1.05])
        first.flush()
        second = CsvExporter(base_path, basic_meta, self.COLUMNS)
        assert second.csv_path != first.csv_path
        assert "0,1.05" in first.csv_path.read_text(encoding='utf-8')
        first.finalize()
        second.finalize()

    def test_third_run_counts_on(self, base_path, basic_meta):
        names = []
        for _ in range(3):
            exp = CsvExporter(base_path, basic_meta, self.COLUMNS)
            exp.finalize()
            names.append(exp.output_paths[0].name)
        assert names == ['run_001.csv', 'run_001-2.csv', 'run_001-3.csv']

    def test_a_compressed_first_run_also_holds_the_name(self, base_path, basic_meta):
        first = CsvExporter(base_path, basic_meta, self.COLUMNS, compression='always')
        first.write_row([0.0, 1.05])
        first.finalize()
        before = first.output_paths[0].read_bytes()

        second = CsvExporter(base_path, basic_meta, self.COLUMNS, compression='always')
        second.write_row([0.0, 2.10])
        second.finalize()

        assert first.output_paths[0].name == 'run_001.csv.gz'
        assert second.output_paths[0].name == 'run_001-2.csv.gz'
        assert first.output_paths[0].read_bytes() == before

    def test_second_hdf5_run_gets_a_new_name(self, base_path, basic_meta):
        pytest.importorskip("h5py")
        first = Hdf5Exporter(base_path, basic_meta, self.COLUMNS)
        first.write_row([0.0, 1.05])
        first.finalize()
        before = first.output_paths[0].read_bytes()

        second = Hdf5Exporter(base_path, basic_meta, self.COLUMNS)
        second.write_row([0.0, 2.10])
        second.finalize()

        assert second.output_paths[0].name == 'run_001-2.h5'
        assert first.output_paths[0].read_bytes() == before

    def test_second_legacy_run_gets_a_new_pair(self, base_path, basic_meta):
        first = LegacyDualExporter(base_path, basic_meta, self.COLUMNS)
        first.write_row([0.0, 1.05])
        first.finalize()
        before = [p.read_bytes() for p in first.output_paths]

        second = LegacyDualExporter(base_path, basic_meta, self.COLUMNS)
        second.write_row([0.0, 2.10])
        second.finalize()

        assert [p.name for p in second.output_paths] == ['run_001-2.csv', 'run_001-2.json']
        assert [p.read_bytes() for p in first.output_paths] == before

    def test_a_crashed_legacy_run_keeps_its_checkpoint(self, base_path, basic_meta):
        # A leftover checkpoint is the crashed run's data: the name is taken.
        crashed = LegacyDualExporter(base_path, basic_meta, self.COLUMNS)
        crashed.write_row([0.0, 1.05])
        crashed.flush()
        crashed._csv_file.close()  # the process died; nothing finalized it
        checkpoint = base_path.with_name('run_001.json.tmp')
        before = checkpoint.read_bytes()

        second = LegacyDualExporter(base_path, basic_meta, self.COLUMNS)
        second.write_row([0.0, 2.10])
        second.flush()
        second.finalize()

        assert checkpoint.read_bytes() == before

    @staticmethod
    def _lose_the_race(monkeypatch, taken: Path):
        """Another process creates ``taken`` just after the name was found
        free and just before this run's exclusive create."""
        import resistamet_gui.data_export as data_export
        real = data_export._name_in_use

        def check_then_lose(candidate, extensions):
            in_use = real(candidate, extensions)
            if not in_use and not taken.exists():
                taken.write_bytes(b"someone else's data\n")
            return in_use
        monkeypatch.setattr(data_export, '_name_in_use', check_then_lose)

    def test_a_csv_name_taken_after_the_check_moves_to_the_next_name(
            self, base_path, basic_meta, monkeypatch):
        taken = base_path.with_name('run_001.csv')
        self._lose_the_race(monkeypatch, taken)
        exp = CsvExporter(base_path, basic_meta, self.COLUMNS)
        exp.write_row([0.0, 2.10])
        exp.finalize()
        assert exp.output_paths[0].name == 'run_001-2.csv'
        assert "0,2.1" in exp.output_paths[0].read_text(encoding='utf-8')
        assert taken.read_bytes() == b"someone else's data\n"

    def test_a_gz_name_taken_during_the_run_moves_to_the_next_name(
            self, base_path, basic_meta):
        exp = CsvExporter(base_path, basic_meta, self.COLUMNS, compression='always')
        exp.write_row([0.0, 2.10])
        taken = base_path.with_name('run_001.csv.gz')
        taken.write_bytes(b"someone else's data\n")
        exp.finalize()
        assert exp.output_paths[0].name == 'run_001-2.csv.gz'
        with gzip.open(exp.output_paths[0], 'rt', encoding='utf-8') as f:
            assert "0,2.1" in f.read()
        assert not base_path.with_name('run_001.csv').exists()
        assert taken.read_bytes() == b"someone else's data\n"

    def test_a_gz_name_is_not_taken_from_another_runs_csv(self, base_path, basic_meta):
        exp = CsvExporter(base_path, basic_meta, self.COLUMNS, compression='always')
        exp.write_row([0.0, 2.10])
        base_path.with_name('run_001.csv.gz').write_bytes(b"someone else's data\n")
        other = base_path.with_name('run_001-2.csv')
        other.write_bytes(b"a third run, still being written\n")
        exp.finalize()
        assert exp.output_paths[0].name == 'run_001-3.csv.gz'
        assert other.read_bytes() == b"a third run, still being written\n"

    def test_an_hdf5_name_taken_after_the_check_moves_to_the_next_name(
            self, base_path, basic_meta, monkeypatch):
        pytest.importorskip("h5py")
        taken = base_path.with_name('run_001.h5')
        self._lose_the_race(monkeypatch, taken)
        exp = Hdf5Exporter(base_path, basic_meta, self.COLUMNS)
        exp.write_row([0.0, 2.10])
        exp.finalize()
        assert exp.output_paths[0].name == 'run_001-2.h5'
        assert taken.read_bytes() == b"someone else's data\n"

    def test_a_legacy_name_taken_after_the_check_moves_the_pair(
            self, base_path, basic_meta, monkeypatch):
        taken = base_path.with_name('run_001.csv')
        self._lose_the_race(monkeypatch, taken)
        exp = LegacyDualExporter(base_path, basic_meta, self.COLUMNS)
        exp.write_row([0.0, 2.10])
        exp.finalize()
        assert [p.name for p in exp.output_paths] == ['run_001-2.csv', 'run_001-2.json']
        assert taken.read_bytes() == b"someone else's data\n"
        assert not base_path.with_name('run_001.json').exists()

    def test_the_search_for_a_name_is_bounded(self, base_path, basic_meta, monkeypatch):
        import resistamet_gui.data_export as data_export
        monkeypatch.setattr(data_export, 'MAX_NAME_ATTEMPTS', 3)
        monkeypatch.setattr(data_export, '_name_in_use', lambda candidate, exts: True)
        with pytest.raises(FileExistsError):
            CsvExporter(base_path, basic_meta, self.COLUMNS)
        assert list(base_path.parent.iterdir()) == []

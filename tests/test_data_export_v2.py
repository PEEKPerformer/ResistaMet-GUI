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

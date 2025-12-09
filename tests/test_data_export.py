"""
Unit tests for the DualExporter class.

Tests cover:
- Dual export initialization
- Row writing
- Finalization with metadata
- Column configuration
"""

import json
import os
import pytest
from pathlib import Path

from resistamet_gui.data_export import (
    DualExporter,
    get_column_config,
    build_metadata,
)


@pytest.fixture
def temp_export_dir(tmp_path):
    """Create a temporary directory for exports."""
    return tmp_path


@pytest.fixture
def basic_metadata():
    """Basic metadata for testing."""
    return {
        'user': 'test_user',
        'sample': 'test_sample',
        'mode': 'resistance',
    }


class TestDualExporterInit:
    """Tests for DualExporter initialization."""

    def test_creates_csv_file(self, temp_export_dir, basic_metadata):
        """Test that CSV file is created on init."""
        base_path = temp_export_dir / "test_measurement"
        exporter = DualExporter(
            base_path=base_path,
            metadata=basic_metadata,
            columns=['elapsed_s', 'R_ohm'],
        )

        assert exporter.csv_path.exists()
        exporter.finalize()

    def test_csv_has_headers(self, temp_export_dir, basic_metadata):
        """Test that CSV file has column headers."""
        base_path = temp_export_dir / "test_measurement"
        exporter = DualExporter(
            base_path=base_path,
            metadata=basic_metadata,
            columns=['elapsed_s', 'R_ohm'],
        )
        exporter.finalize()

        with open(exporter.csv_path) as f:
            header = f.readline().strip()
            assert header == 'elapsed_s,R_ohm'


class TestRowWriting:
    """Tests for writing data rows."""

    def test_write_row_to_csv(self, temp_export_dir, basic_metadata):
        """Test that rows are written to CSV."""
        base_path = temp_export_dir / "test_measurement"
        exporter = DualExporter(
            base_path=base_path,
            metadata=basic_metadata,
            columns=['elapsed_s', 'R_ohm'],
        )

        exporter.write_row([1.0, 100.5])
        exporter.write_row([2.0, 101.3])
        exporter.finalize()

        with open(exporter.csv_path) as f:
            lines = f.readlines()
            assert len(lines) == 3  # Header + 2 data rows

    def test_row_count_tracking(self, temp_export_dir, basic_metadata):
        """Test that row count is tracked correctly."""
        base_path = temp_export_dir / "test_measurement"
        exporter = DualExporter(
            base_path=base_path,
            metadata=basic_metadata,
            columns=['elapsed_s', 'R_ohm'],
        )

        assert exporter.row_count == 0
        exporter.write_row([1.0, 100.5])
        assert exporter.row_count == 1
        exporter.write_row([2.0, 101.3])
        assert exporter.row_count == 2
        exporter.finalize()


class TestFinalization:
    """Tests for export finalization."""

    def test_creates_json_on_finalize(self, temp_export_dir, basic_metadata):
        """Test that JSON file is created on finalize."""
        base_path = temp_export_dir / "test_measurement"
        exporter = DualExporter(
            base_path=base_path,
            metadata=basic_metadata,
            columns=['elapsed_s', 'R_ohm'],
            units=['s', 'Ohm'],
        )

        exporter.write_row([1.0, 100.5])
        exporter.finalize()

        assert exporter.json_path.exists()

    def test_json_contains_metadata(self, temp_export_dir, basic_metadata):
        """Test that JSON file contains metadata."""
        base_path = temp_export_dir / "test_measurement"
        exporter = DualExporter(
            base_path=base_path,
            metadata=basic_metadata,
            columns=['elapsed_s', 'R_ohm'],
        )

        exporter.write_row([1.0, 100.5])
        exporter.finalize()

        with open(exporter.json_path) as f:
            data = json.load(f)

        assert 'meta' in data
        assert data['meta']['user'] == 'test_user'
        assert data['meta']['sample'] == 'test_sample'

    def test_json_contains_data(self, temp_export_dir, basic_metadata):
        """Test that JSON file contains data array."""
        base_path = temp_export_dir / "test_measurement"
        exporter = DualExporter(
            base_path=base_path,
            metadata=basic_metadata,
            columns=['elapsed_s', 'R_ohm'],
        )

        exporter.write_row([1.0, 100.5])
        exporter.write_row([2.0, 101.3])
        exporter.finalize()

        with open(exporter.json_path) as f:
            data = json.load(f)

        assert 'data' in data
        assert len(data['data']) == 2
        assert data['data'][0] == [1.0, 100.5]
        assert data['row_count'] == 2

    def test_end_metadata_merged(self, temp_export_dir, basic_metadata):
        """Test that end metadata is merged on finalize."""
        base_path = temp_export_dir / "test_measurement"
        exporter = DualExporter(
            base_path=base_path,
            metadata=basic_metadata,
            columns=['elapsed_s', 'R_ohm'],
        )

        exporter.write_row([1.0, 100.5])
        exporter.finalize(end_metadata={'ended_at': '2025-01-01T12:00:00'})

        with open(exporter.json_path) as f:
            data = json.load(f)

        assert data['meta']['ended_at'] == '2025-01-01T12:00:00'


class TestContextManager:
    """Tests for context manager support."""

    def test_context_manager_finalizes(self, temp_export_dir, basic_metadata):
        """Test that context manager finalizes on exit."""
        base_path = temp_export_dir / "test_measurement"

        with DualExporter(
            base_path=base_path,
            metadata=basic_metadata,
            columns=['elapsed_s', 'R_ohm'],
        ) as exporter:
            exporter.write_row([1.0, 100.5])

        # JSON should exist after context exit
        assert (base_path.with_suffix('.json')).exists()


class TestColumnConfig:
    """Tests for column configuration helper."""

    def test_resistance_mode_columns(self):
        """Test column config for resistance mode."""
        columns, units = get_column_config('resistance')
        assert 'elapsed_s' in columns
        assert 'R_ohm' in columns

    def test_four_point_mode_columns(self):
        """Test column config for four_point mode."""
        columns, units = get_column_config('four_point')
        assert 'elapsed_s' in columns
        assert 'Rs_ohm_sq' in columns
        assert 'rho_ohm_cm' in columns
        assert 'sigma_S_cm' in columns

    def test_unknown_mode_fallback(self):
        """Test fallback for unknown mode."""
        columns, units = get_column_config('unknown_mode')
        assert len(columns) >= 2  # At least elapsed and value


class TestBuildMetadata:
    """Tests for metadata building helper."""

    def test_basic_metadata_fields(self):
        """Test that basic metadata fields are present."""
        meta = build_metadata(
            user='test_user',
            sample_name='test_sample',
            mode='resistance',
            settings={'measurement': {}},
        )

        assert meta['user'] == 'test_user'
        assert meta['sample'] == 'test_sample'
        assert meta['mode'] == 'resistance'
        assert 'started_at' in meta
        assert 'software_version' in meta

    def test_mode_specific_params(self):
        """Test that mode-specific params are included."""
        settings = {
            'measurement': {
                'res_test_current': 0.001,
                'res_voltage_compliance': 21.0,
            }
        }
        meta = build_metadata(
            user='test_user',
            sample_name='test_sample',
            mode='resistance',
            settings=settings,
        )

        assert 'params' in meta
        assert meta['params']['test_current_A'] == 0.001

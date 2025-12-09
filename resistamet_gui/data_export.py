"""
Data Export Module

Provides dual-format export for measurement data:
- JSON: Complete record with metadata, LLM-readable, programmatic access
- CSV: Clean data only, Excel-friendly for quick viewing

Usage:
    exporter = DualExporter(base_path, metadata, columns)
    exporter.write_row([0.0, 0.00105, 0.001, 1.05])
    exporter.write_row([1.0, 0.00104, 0.001, 1.04])
    exporter.finalize()
"""

import csv
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


class DualExporter:
    """Exports measurement data to both JSON and CSV formats simultaneously.

    The JSON file contains:
    - Complete metadata (instrument, parameters, user, sample, etc.)
    - Column definitions with units
    - All data rows in compact array format

    The CSV file contains:
    - Header row with column names
    - Data rows only (no metadata clutter)
    - Opens cleanly in Excel

    Example JSON output:
    {
        "format_version": "1.0",
        "meta": {
            "user": "brenden",
            "sample": "Si_wafer_001",
            "mode": "four_point",
            ...
        },
        "columns": ["t_s", "V", "I", "R"],
        "units": ["s", "V", "A", "Ω"],
        "data": [
            [0.0, 0.00105, 0.001, 1.05],
            [1.0, 0.00104, 0.001, 1.04]
        ]
    }

    Example CSV output:
    t_s,V,I,R
    0.0,0.00105,0.001,1.05
    1.0,0.00104,0.001,1.04
    """

    FORMAT_VERSION = "1.0"

    def __init__(
        self,
        base_path: Union[str, Path],
        metadata: Dict[str, Any],
        columns: List[str],
        units: Optional[List[str]] = None
    ):
        """Initialize the dual exporter.

        Args:
            base_path: Base file path without extension (e.g., "/data/measurement_001")
            metadata: Dictionary of metadata (user, sample, mode, parameters, etc.)
            columns: List of column names (e.g., ["t_s", "V", "I", "R"])
            units: Optional list of units for each column (e.g., ["s", "V", "A", "Ω"])
        """
        self.base_path = Path(base_path)
        self.json_path = self.base_path.with_suffix('.json')
        self.csv_path = self.base_path.with_suffix('.csv')

        self.metadata = metadata
        self.columns = columns
        self.units = units or []

        self._data_rows: List[List[Any]] = []
        self._csv_file = None
        self._csv_writer = None
        self._finalized = False

        self._init_csv()

    def _init_csv(self) -> None:
        """Initialize the CSV file with headers."""
        try:
            # Ensure directory exists
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)

            self._csv_file = open(self.csv_path, 'w', newline='', encoding='utf-8')
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow(self.columns)
            self._csv_file.flush()

            logger.debug(f"Initialized CSV export: {self.csv_path}")
        except Exception as e:
            logger.error(f"Failed to initialize CSV export: {e}")
            raise

    def write_row(self, row: List[Any]) -> None:
        """Write a data row to both formats.

        Args:
            row: List of values matching the column order
        """
        if self._finalized:
            raise RuntimeError("Cannot write to finalized exporter")

        # Store for JSON (written at end)
        self._data_rows.append(row)

        # Write to CSV immediately (streaming)
        if self._csv_writer:
            # Format floats nicely for CSV
            formatted_row = [
                f"{v:.6g}" if isinstance(v, float) else str(v)
                for v in row
            ]
            self._csv_writer.writerow(formatted_row)

    def flush(self) -> None:
        """Flush CSV to disk (for auto-save during measurement)."""
        if self._csv_file:
            try:
                self._csv_file.flush()
                os.fsync(self._csv_file.fileno())
            except Exception as e:
                logger.warning(f"Failed to flush CSV: {e}")

    def finalize(self, end_metadata: Optional[Dict[str, Any]] = None) -> None:
        """Finalize export and write JSON file.

        Args:
            end_metadata: Optional additional metadata to add at end
                         (e.g., end_time, total_samples)
        """
        if self._finalized:
            return

        # Close CSV
        if self._csv_file:
            try:
                self._csv_file.flush()
                self._csv_file.close()
            except Exception as e:
                logger.warning(f"Error closing CSV: {e}")
            finally:
                self._csv_file = None
                self._csv_writer = None

        # Build final metadata
        final_meta = dict(self.metadata)
        if end_metadata:
            final_meta.update(end_metadata)

        # Write JSON
        json_data = {
            "format_version": self.FORMAT_VERSION,
            "meta": final_meta,
            "columns": self.columns,
            "units": self.units,
            "row_count": len(self._data_rows),
            "data": self._data_rows
        }

        try:
            with open(self.json_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)
            logger.info(f"Saved JSON export: {self.json_path}")
        except Exception as e:
            logger.error(f"Failed to write JSON: {e}")
            raise

        self._finalized = True
        logger.info(f"Export finalized: {len(self._data_rows)} rows")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self._finalized:
            self.finalize()
        return False

    @property
    def row_count(self) -> int:
        """Number of data rows written."""
        return len(self._data_rows)


def get_column_config(mode: str) -> tuple:
    """Get column names and units for a measurement mode.

    Args:
        mode: Measurement mode ('resistance', 'source_v', 'source_i', 'four_point')

    Returns:
        Tuple of (columns, units)
    """
    configs = {
        'resistance': (
            ['elapsed_s', 'R_ohm', 'compliance', 'event'],
            ['s', 'Ω', '', '']
        ),
        'source_v': (
            ['elapsed_s', 'V_set', 'I_meas', 'R_calc', 'compliance', 'event'],
            ['s', 'V', 'A', 'Ω', '', '']
        ),
        'source_i': (
            ['elapsed_s', 'V_meas', 'I_set', 'R_calc', 'compliance', 'event'],
            ['s', 'V', 'A', 'Ω', '', '']
        ),
        'four_point': (
            ['elapsed_s', 'V', 'I', 'V_over_I', 'Rs_ohm_sq', 'rho_ohm_cm', 'sigma_S_cm', 'compliance', 'event'],
            ['s', 'V', 'A', 'Ω', 'Ω/□', 'Ω·cm', 'S/cm', '', '']
        ),
    }
    return configs.get(mode, (['elapsed_s', 'value'], ['s', '']))


def build_metadata(
    user: str,
    sample_name: str,
    mode: str,
    settings: Dict[str, Any],
    instrument_idn: str = "",
    start_time: Optional[datetime] = None
) -> Dict[str, Any]:
    """Build metadata dictionary for export.

    Args:
        user: Username
        sample_name: Sample identifier
        mode: Measurement mode
        settings: Full settings dictionary
        instrument_idn: Instrument identification string
        start_time: Measurement start time (defaults to now)

    Returns:
        Metadata dictionary
    """
    from .constants import __version__

    start_time = start_time or datetime.now()
    measurement_settings = settings.get('measurement', {})

    meta = {
        'user': user,
        'sample': sample_name,
        'mode': mode,
        'started_at': start_time.isoformat(),
        'software_version': __version__,
        'instrument': instrument_idn,
        'gpib_address': measurement_settings.get('gpib_address', ''),
        'sampling_rate_hz': measurement_settings.get('sampling_rate', 10.0),
        'nplc': measurement_settings.get('nplc', 1.0),
        'settling_time_s': measurement_settings.get('settling_time', 0.2),
    }

    # Add mode-specific parameters
    if mode == 'resistance':
        meta['params'] = {
            'test_current_A': measurement_settings.get('res_test_current'),
            'voltage_compliance_V': measurement_settings.get('res_voltage_compliance'),
            'measurement_type': measurement_settings.get('res_measurement_type'),
            'auto_range': measurement_settings.get('res_auto_range'),
        }
    elif mode == 'source_v':
        meta['params'] = {
            'source_voltage_V': measurement_settings.get('vsource_voltage'),
            'current_compliance_A': measurement_settings.get('vsource_current_compliance'),
            'duration_hours': measurement_settings.get('vsource_duration_hours'),
        }
    elif mode == 'source_i':
        meta['params'] = {
            'source_current_A': measurement_settings.get('isource_current'),
            'voltage_compliance_V': measurement_settings.get('isource_voltage_compliance'),
            'duration_hours': measurement_settings.get('isource_duration_hours'),
        }
    elif mode == 'four_point':
        meta['params'] = {
            'source_current_A': measurement_settings.get('fpp_current'),
            'voltage_compliance_V': measurement_settings.get('fpp_voltage_compliance'),
            'probe_spacing_cm': measurement_settings.get('fpp_spacing_cm'),
            'thickness_um': measurement_settings.get('fpp_thickness_um'),
            'k_factor': measurement_settings.get('fpp_k_factor'),
            'alpha': measurement_settings.get('fpp_alpha'),
            'model': measurement_settings.get('fpp_model'),
            'target_samples': measurement_settings.get('fpp_samples'),
        }

    return meta

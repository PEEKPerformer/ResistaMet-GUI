"""
Data Export Module

Pluggable backends for measurement-data export. A single ``make_exporter()``
factory selects the implementation from the ``output`` config section:

- ``csv`` (default)        single ``.csv`` with ``#``-prefixed metadata header
- ``hdf5``                 single ``.h5``, gzip-compressed, metadata in attrs
- ``csv+legacy_json``      pre-2.0 dual ``.csv`` + ``.json`` emit (back-compat)

Compression is orthogonal to format: the CSV path can optionally be gzipped at
finalize per ``output.compression``. HDF5 is always internally compressed.

Usage:

    exporter = make_exporter(base_path, metadata, columns, units, output_settings)
    exporter.write_row([0.0, 0.00105, 0.001, 1.05])
    exporter.flush()
    exporter.finalize({'ended_at': ..., 'total_samples': 1})
"""

import ast
import csv
import gzip
import json
import logging
import math
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

FORMAT_VERSION = "2.0"
LEGACY_FORMAT_VERSION = "1.0"

_CSV_END_MARKER = "# --- run completed ---"

# Continuous polling modes that support auxiliary-sensor co-logging. Sweep
# (atomic :READ?) and vdP (manual geometry protocol) are excluded.
_AUX_LOG_MODES = ('resistance', 'source_v', 'source_i', 'four_point')

# Threshold above which CsvExporter fires the large-file notification when
# the final artifact landed uncompressed. Aggressive on purpose — most
# overnight runs cross 20 MB, so users discover the compression setting
# without it ever blocking them.
LARGE_FILE_NOTIFY_MB = 20.0


# -------------------------- Metadata serialization --------------------------


def _flatten_metadata(meta: Dict[str, Any], prefix: str = "") -> List[Tuple[str, Any]]:
    items: List[Tuple[str, Any]] = []
    for key, value in meta.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            items.extend(_flatten_metadata(value, full))
        else:
            items.append((full, value))
    return items


def _format_scalar(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
    return repr(value)


def _parse_scalar(value: str) -> Any:
    if value == "":
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "NaN":
        return float('nan')
    if value == "Infinity":
        return float('inf')
    if value == "-Infinity":
        return float('-inf')
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return value


def _write_metadata_block(f, meta: Dict[str, Any], units: Optional[List[str]] = None) -> None:
    f.write(f"# resistamet_format_version: {FORMAT_VERSION}\n")
    for key, value in _flatten_metadata(meta):
        f.write(f"# {key}: {_format_scalar(value)}\n")
    if units:
        f.write(f"# units: {','.join(units)}\n")


def parse_metadata(path: Union[str, Path]) -> Dict[str, Any]:
    """Parse the ``#`` metadata header (and trailing end block, if present) from a CSV.

    Supports plain ``.csv`` and ``.csv.gz``. Returns a flat dict of key/value
    pairs with values coerced back to native Python types. The ``units`` line
    is exposed as a list. End-metadata fields (``ended_at``, ``total_samples``,
    ``duration_s``) merge into the same dict with no special prefix.
    """
    path = Path(path)
    is_gz = path.suffix == '.gz'
    opener = gzip.open if is_gz else open
    meta: Dict[str, Any] = {}

    def absorb(line: str) -> None:
        body = line[1:].strip()
        if not body or body.startswith('---'):
            return
        if ':' not in body:
            return
        key, _, value = body.partition(':')
        key = key.strip()
        value = value.strip()
        if key == 'units':
            meta['units'] = value.split(',')
        else:
            meta.setdefault(key, _parse_scalar(value))

    # Head pass: read leading # lines until the column-header row.
    with opener(path, 'rt', encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\n')
            if not line:
                continue
            if not line.startswith('#'):
                break
            absorb(line)

    # Tail pass: pick up any trailing # lines written at finalize.
    # For plain CSVs we seek the last 8 KiB so this is cheap on large files.
    # For .gz we fall back to a full streaming read since gzip can't seek.
    try:
        if is_gz:
            with opener(path, 'rt', encoding='utf-8') as f:
                tail_lines = f.readlines()[-64:]
        else:
            size = path.stat().st_size
            with open(path, 'rb') as fb:
                fb.seek(max(0, size - 8192))
                tail_text = fb.read().decode('utf-8', errors='replace')
            tail_lines = tail_text.splitlines()
        for line in reversed(tail_lines):
            line = line.rstrip()
            if not line:
                continue
            if not line.startswith('#'):
                break
            absorb(line)
    except Exception as e:
        logger.debug(f"parse_metadata tail pass failed (non-fatal): {e}")

    return meta


# --------------------------- Column config helpers --------------------------


def get_column_config(mode: str, measurement_settings: Optional[Dict[str, Any]] = None) -> tuple:
    """Get column names and units for a measurement mode.

    Args:
        mode: Measurement mode ('resistance', 'source_v', 'source_i', 'four_point',
            'sweep', 'vdp').
        measurement_settings: Optional settings dict. When provided and 4PP
            delta mode is enabled, the column list expands to include the
            per-polarity values V+, V-, R_f, R_r (F84 §11.2.2.2 diagnostic).
    """
    configs = {
        # V_meas, I_meas, R_unc added 2026-05: raw V and I are pulled
        # alongside R so downstream analysis can carry per-reading
        # uncertainty (Σ propagated in quadrature from 2400 per-range V
        # and I accuracy specs — see accuracy.resistance_uncertainty). R
        # remains the instrument-reported ohms reading (not V/I computed
        # here), so any ohms-mode features (offset compensation, source
        # readback) are preserved in the R column.
        'resistance': (
            ['elapsed_s', 'V_meas', 'I_meas', 'R_ohm', 'R_unc_ohm', 'compliance', 'event'],
            ['s', 'V', 'A', 'Ω', 'Ω', '', '']
        ),
        # I_unc and R_calc_unc are 1-year accuracies. I_unc is the
        # measure-side spec on I; R_calc_unc combines I_unc and the V-source
        # spec via RSS of relative uncertainties through R = V_set/I_meas.
        'source_v': (
            ['elapsed_s', 'V_set', 'I_meas', 'R_calc', 'I_unc_A', 'R_calc_unc_ohm', 'compliance', 'event'],
            ['s', 'V', 'A', 'Ω', 'A', 'Ω', '', '']
        ),
        # V_unc is measure-side on V; R_calc_unc combines V_unc and the
        # I-source spec via RSS through R = V_meas/I_set.
        'source_i': (
            ['elapsed_s', 'V_meas', 'I_set', 'R_calc', 'V_unc_V', 'R_calc_unc_ohm', 'compliance', 'event'],
            ['s', 'V', 'A', 'Ω', 'V', 'Ω', '', '']
        ),
        # V_unc and I_unc are 1-year measurement accuracies on the raw V
        # and I (accuracy.voltage_uncertainty / current_uncertainty). Each
        # row's Rs/ρ/σ uncertainty is recoverable downstream as σ_X/X =
        # √((V_unc/V)² + (I_unc/I)²); matches the per-spot u_inst term
        # shown in the live results panel.
        'four_point': (
            ['elapsed_s', 'V', 'I', 'V_over_I', 'Rs_ohm_sq', 'rho_ohm_cm', 'sigma_S_cm', 'V_unc_V', 'I_unc_A', 'compliance', 'event'],
            ['s', 'V', 'A', 'Ω', 'Ω/□', 'Ω·cm', 'S/cm', 'V', 'A', '', '']
        ),
        'sweep': (
            ['point', 'V_source', 'I_meas', 'compliance'],
            ['', 'V', 'A', '']
        ),
        # Van der Pauw (F76 Method A): 4 rows, one per physical geometry.
        # Each row captures both polarities of that geometry; the summary
        # rho/Rs lands in metadata at finalize().
        'vdp': (
            ['elapsed_s', 'geometry', 'group',
             'source_high', 'source_low', 'sense_high', 'sense_low',
             'label_pos', 'V_pos', 'label_neg', 'V_neg', 'current_A'],
            ['s', '', '',
             '', '', '', '',
             '', 'V', '', 'V', 'A']
        ),
    }
    cols, units = configs.get(mode, (['elapsed_s', 'value'], ['s', '']))

    # In 4PP delta mode, splice per-polarity columns before compliance/event.
    if mode == 'four_point' and measurement_settings is not None:
        if measurement_settings.get('fpp_delta_mode'):
            cols = list(cols)
            units = list(units)
            insert_at = cols.index('compliance')
            cols[insert_at:insert_at] = ['V_plus', 'V_minus', 'R_f', 'R_r']
            units[insert_at:insert_at] = ['V', 'V', 'Ω', 'Ω']

    # Auxiliary-sensor co-logging applies to every continuous polling mode
    # (anchored to the Keithley run): splice one column per declared sensor
    # channel before compliance/event (after any delta columns). The worker
    # stashes the channel names/units (from the opened sensor's channels())
    # here so the schema follows the sensor, not a hardcoded list. When logging
    # is off, nothing is spliced and the CSV is byte-identical to a plain run.
    if (mode in _AUX_LOG_MODES and measurement_settings is not None
            and measurement_settings.get('aux_log_enabled')):
        aux_cols = measurement_settings.get('_aux_columns') or []
        aux_units = measurement_settings.get('_aux_units') or ([''] * len(aux_cols))
        if aux_cols:
            cols = list(cols)
            units = list(units)
            insert_at = cols.index('compliance')
            cols[insert_at:insert_at] = list(aux_cols)
            units[insert_at:insert_at] = list(aux_units)

    return (cols, units)


def build_metadata(
    user: str,
    sample_name: str,
    mode: str,
    settings: Dict[str, Any],
    instrument_idn: str = "",
    start_time: Optional[datetime] = None
) -> Dict[str, Any]:
    """Build metadata dictionary for export. Shared schema across all backends."""
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

    # Range / auto-range modes are recorded here so downstream analysis can
    # recover which accuracy-spec row the inference used. accuracy.py picks
    # the range via the Keithley's 105% overrange rule, so the raw V and I
    # in the CSV are sufficient for forensic recomputation — but capturing
    # the configured mode makes the audit trail explicit instead of
    # reverse-engineered.
    if mode == 'resistance':
        # offset_compensated_ohms records whether Enhanced-accuracy R
        # mode was active (:SENS:RES:OCOM ON). Materially affects how
        # downstream code should interpret σ_R: when True, accuracy.py
        # served the datasheet's Enhanced R-spec column; when False, σ_R
        # came from V/I propagation. The two are *not* directly
        # comparable across measurements — record per-run for audit.
        meta['params'] = {
            'test_current_A': measurement_settings.get('res_test_current'),
            'voltage_compliance_V': measurement_settings.get('res_voltage_compliance'),
            'measurement_type': measurement_settings.get('res_measurement_type'),
            'auto_range': measurement_settings.get('res_auto_range'),
            'auto_zero': measurement_settings.get('auto_zero'),
            'offset_compensated_ohms': bool(measurement_settings.get('res_offset_comp', False)),
        }
    elif mode == 'source_v':
        meta['params'] = {
            'source_voltage_V': measurement_settings.get('vsource_voltage'),
            'current_compliance_A': measurement_settings.get('vsource_current_compliance'),
            'current_auto_range': measurement_settings.get('vsource_current_range_auto'),
            'duration_hours': measurement_settings.get('vsource_duration_hours'),
            'auto_zero': measurement_settings.get('auto_zero'),
        }
    elif mode == 'source_i':
        meta['params'] = {
            'source_current_A': measurement_settings.get('isource_current'),
            'voltage_compliance_V': measurement_settings.get('isource_voltage_compliance'),
            'voltage_auto_range': measurement_settings.get('isource_voltage_range_auto'),
            'duration_hours': measurement_settings.get('isource_duration_hours'),
            'auto_zero': measurement_settings.get('auto_zero'),
        }
    elif mode == 'four_point':
        meta['params'] = {
            'source_current_A': measurement_settings.get('fpp_current'),
            'voltage_compliance_V': measurement_settings.get('fpp_voltage_compliance'),
            'voltage_auto_range': measurement_settings.get('fpp_voltage_range_auto'),
            'probe_spacing_cm': measurement_settings.get('fpp_spacing_cm'),
            'thickness_um': measurement_settings.get('fpp_thickness_um'),
            'k_factor': measurement_settings.get('fpp_k_factor'),
            'alpha': measurement_settings.get('fpp_alpha'),
            'model': measurement_settings.get('fpp_model'),
            'target_samples': measurement_settings.get('fpp_samples'),
            'auto_zero': measurement_settings.get('auto_zero'),
        }
    elif mode == 'vdp':
        meta['params'] = {
            'source_current_A': measurement_settings.get('vdp_current'),
            'voltage_compliance_V': measurement_settings.get('vdp_voltage_compliance'),
            'voltage_auto_range': measurement_settings.get('vdp_voltage_range_auto'),
            'thickness_cm': measurement_settings.get('vdp_thickness_cm'),
            'settling_s': measurement_settings.get('vdp_settling_s'),
            'readings_per_polarity': measurement_settings.get('vdp_readings_per_polarity'),
            'auto_zero': measurement_settings.get('auto_zero'),
            'standard': 'ASTM F76-08 Method A',
        }
    elif mode == 'sweep':
        meta['params'] = {
            'source_function': measurement_settings.get('sweep_source'),
            'start': measurement_settings.get('sweep_start'),
            'stop': measurement_settings.get('sweep_stop'),
            'step': measurement_settings.get('sweep_step'),
            'compliance': measurement_settings.get('sweep_compliance'),
            'delay_s': measurement_settings.get('sweep_delay'),
            'direction': measurement_settings.get('sweep_direction'),
        }

    # Auxiliary-sensor provenance (any continuous mode) — only recorded when
    # co-logging is on, so a non-sensor run's metadata header is unchanged.
    if (mode in _AUX_LOG_MODES
            and measurement_settings.get('aux_log_enabled')
            and 'params' in meta):
        meta['params']['aux_sensor_driver'] = measurement_settings.get('aux_driver')
        meta['params']['aux_sensor_address'] = measurement_settings.get('aux_address')
        meta['params']['aux_channels'] = measurement_settings.get('_aux_columns')

    return meta


# --------------------------------- Backends ---------------------------------


class _BaseExporter:
    """Minimal interface every backend implements."""

    def write_row(self, row: List[Any]) -> None:
        raise NotImplementedError

    def flush(self, checkpoint: bool = True) -> None:
        pass

    def finalize(self, end_metadata: Optional[Dict[str, Any]] = None) -> None:
        raise NotImplementedError

    @property
    def output_paths(self) -> List[Path]:
        return []

    @property
    def row_count(self) -> int:
        return 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.finalize()
        except Exception:
            pass
        return False


class CsvExporter(_BaseExporter):
    """Single CSV with #-prefixed metadata header.

    Layout::

        # resistamet_format_version: 2.0
        # user: brenden
        # sample: ...
        # mode: ...
        # ... (flattened metadata, nested via dots)
        # units: s,V,A,Ω,Ω/□,...
        elapsed_s,V,I,...        ← column header row
        0.1,0.00105,0.001,...    ← streamed data rows
        # --- run completed ---
        # ended_at: ...
        # total_samples: ...

    Crash safety: rows are written and fsync'd as they arrive; the partial
    CSV is itself the recovery artifact, so no checkpoint sidecar is needed.

    Compression: if ``compression == 'always'`` the file is gzipped on
    finalize. ``auto`` only gzips if the file is larger than
    ``threshold_mb``. ``never`` leaves the .csv alone. ``on_compress`` (if
    set) is invoked with ``(original_path, compressed_path,
    original_size_mb, compressed_size_mb)`` so the UI can surface a status-
    bar message.
    """

    def __init__(
        self,
        base_path: Union[str, Path],
        metadata: Dict[str, Any],
        columns: List[str],
        units: Optional[List[str]] = None,
        compression: str = "never",
        threshold_mb: float = 5.0,
        on_compress: Optional[Callable[[Path, Path, float, float], None]] = None,
        on_large_file: Optional[Callable[[Path, float], None]] = None,
        large_file_notify_mb: float = LARGE_FILE_NOTIFY_MB,
    ):
        self.base_path = Path(base_path)
        self.csv_path = self.base_path.with_suffix('.csv')
        self.metadata = metadata
        self.columns = list(columns)
        self.units = list(units or [])
        self.compression = compression
        self.threshold_mb = float(threshold_mb)
        self.on_compress = on_compress
        self.on_large_file = on_large_file
        self.large_file_notify_mb = float(large_file_notify_mb)

        self._row_count = 0
        self._csv_file = None
        self._csv_writer = None
        self._finalized = False
        self._final_path = self.csv_path

        self._init_csv()

    def _init_csv(self) -> None:
        try:
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
            self._csv_file = open(self.csv_path, 'w', newline='', encoding='utf-8')
            _write_metadata_block(self._csv_file, self.metadata, self.units)
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow(self.columns)
            self._csv_file.flush()
            logger.debug(f"Initialized CSV export: {self.csv_path}")
        except Exception as e:
            logger.error(f"Failed to initialize CSV export: {e}")
            raise

    def write_row(self, row: List[Any]) -> None:
        if self._finalized:
            raise RuntimeError("Cannot write to finalized exporter")
        if self._csv_writer:
            formatted = [
                f"{v:.6g}" if isinstance(v, float) else str(v)
                for v in row
            ]
            self._csv_writer.writerow(formatted)
            self._row_count += 1

    def flush(self, checkpoint: bool = True) -> None:
        if self._csv_file:
            try:
                self._csv_file.flush()
                os.fsync(self._csv_file.fileno())
            except Exception as e:
                logger.warning(f"Failed to flush CSV: {e}")

    def finalize(self, end_metadata: Optional[Dict[str, Any]] = None) -> None:
        if self._finalized:
            return
        if self._csv_file:
            try:
                if end_metadata:
                    self._csv_file.write(f"{_CSV_END_MARKER}\n")
                    for key, value in _flatten_metadata(end_metadata):
                        self._csv_file.write(f"# {key}: {_format_scalar(value)}\n")
                self._csv_file.flush()
                self._csv_file.close()
            except Exception as e:
                logger.warning(f"Error closing CSV: {e}")
            finally:
                self._csv_file = None
                self._csv_writer = None

        self._final_path = self._maybe_compress()
        self._finalized = True
        logger.info(f"Export finalized: {self._row_count} rows -> {self._final_path}")
        # Passive nudge: if the final artifact is an uncompressed .csv that's
        # crossed the notification threshold, tell the user. Compressed runs
        # already surface through on_compress, so we skip them here.
        if (
            self.on_large_file is not None
            and self._final_path.suffix == '.csv'
        ):
            try:
                size_mb = self._final_path.stat().st_size / (1024 * 1024)
            except OSError:
                size_mb = 0.0
            if size_mb >= self.large_file_notify_mb:
                try:
                    self.on_large_file(self._final_path, size_mb)
                except Exception as e:
                    logger.debug(f"on_large_file callback raised (ignored): {e}")

    def _maybe_compress(self) -> Path:
        if self.compression == "never":
            return self.csv_path
        try:
            size_mb = self.csv_path.stat().st_size / (1024 * 1024)
        except OSError:
            return self.csv_path
        if self.compression == "auto" and size_mb < self.threshold_mb:
            return self.csv_path
        gz_path = self.csv_path.with_suffix('.csv.gz')
        try:
            with open(self.csv_path, 'rb') as src, gzip.open(gz_path, 'wb', compresslevel=6) as dst:
                shutil.copyfileobj(src, dst)
            self.csv_path.unlink()
            gz_size_mb = gz_path.stat().st_size / (1024 * 1024)
            if self.on_compress:
                try:
                    self.on_compress(self.csv_path, gz_path, size_mb, gz_size_mb)
                except Exception as e:
                    logger.debug(f"on_compress callback raised (ignored): {e}")
            return gz_path
        except Exception as e:
            logger.warning(f"Failed to compress CSV (keeping uncompressed): {e}")
            return self.csv_path

    @property
    def output_paths(self) -> List[Path]:
        return [self._final_path]

    @property
    def row_count(self) -> int:
        return self._row_count


class Hdf5Exporter(_BaseExporter):
    """Single ``.h5`` with chunked gzip-compressed dataset and metadata in attrs.

    Lazy-imports ``h5py``; raises a clear ``ImportError`` if not installed.
    All columns are stored as variable-length UTF-8 strings in a compound
    dtype, so mixed-type modes (vdP labels, compliance flags) work without
    a separate schema per mode. Numeric callers can re-cast on read.
    """

    DATASET_NAME = "data"
    CHUNK_ROWS = 1024

    def __init__(
        self,
        base_path: Union[str, Path],
        metadata: Dict[str, Any],
        columns: List[str],
        units: Optional[List[str]] = None,
    ):
        try:
            import h5py
        except ImportError as e:
            raise ImportError(
                "HDF5 export requires the optional 'h5py' package. "
                "Install with: pip install h5py"
            ) from e
        self._h5py = h5py

        self.base_path = Path(base_path)
        self.h5_path = self.base_path.with_suffix('.h5')
        self.metadata = metadata
        self.columns = list(columns)
        self.units = list(units or [])

        self._row_count = 0
        self._finalized = False

        self._init_h5()

    def _init_h5(self) -> None:
        self.h5_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._h5py.File(self.h5_path, 'w')
        vlen_str = self._h5py.string_dtype(encoding='utf-8')
        dtype = [(c, vlen_str) for c in self.columns]
        self._dataset = self._file.create_dataset(
            self.DATASET_NAME,
            shape=(0,),
            maxshape=(None,),
            chunks=(self.CHUNK_ROWS,),
            dtype=dtype,
            compression='gzip',
            compression_opts=6,
        )
        self._set_attrs(self.metadata)
        if self.units:
            self._file.attrs['units'] = self.units
        self._file.attrs['columns'] = self.columns
        self._file.attrs['resistamet_format_version'] = FORMAT_VERSION

    def _set_attrs(self, meta: Dict[str, Any]) -> None:
        for key, value in _flatten_metadata(meta):
            try:
                if value is None:
                    self._file.attrs[key] = ""
                elif isinstance(value, (str, bool, int, float)):
                    self._file.attrs[key] = value
                elif isinstance(value, (list, tuple)):
                    self._file.attrs[key] = list(value)
                else:
                    self._file.attrs[key] = repr(value)
            except (TypeError, ValueError):
                self._file.attrs[key] = repr(value)

    def write_row(self, row: List[Any]) -> None:
        if self._finalized:
            raise RuntimeError("Cannot write to finalized exporter")
        new_size = self._row_count + 1
        self._dataset.resize((new_size,))
        record = tuple(
            ("" if v is None else (f"{v:.10g}" if isinstance(v, float) else str(v)))
            for v in row
        )
        self._dataset[self._row_count] = record
        self._row_count = new_size

    def flush(self, checkpoint: bool = True) -> None:
        try:
            self._file.flush()
        except Exception as e:
            logger.warning(f"Failed to flush HDF5: {e}")

    def finalize(self, end_metadata: Optional[Dict[str, Any]] = None) -> None:
        if self._finalized:
            return
        if end_metadata:
            self._set_attrs(end_metadata)
        try:
            self._file.flush()
            self._file.close()
        except Exception as e:
            logger.warning(f"Error closing HDF5: {e}")
        self._finalized = True
        logger.info(f"HDF5 export finalized: {self._row_count} rows -> {self.h5_path}")

    @property
    def output_paths(self) -> List[Path]:
        return [self.h5_path]

    @property
    def row_count(self) -> int:
        return self._row_count


class LegacyDualExporter(_BaseExporter):
    """Pre-2.0 dual JSON+CSV exporter. Selectable via output.format = csv+legacy_json.

    Identical behavior to the original ``DualExporter`` so anyone with pipelines
    parsing the ``.json`` file keeps working through one or two more releases.
    """

    FORMAT_VERSION = LEGACY_FORMAT_VERSION

    def __init__(
        self,
        base_path: Union[str, Path],
        metadata: Dict[str, Any],
        columns: List[str],
        units: Optional[List[str]] = None,
    ):
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
        self._last_checkpoint_count = 0
        self._init_csv()

    def _init_csv(self) -> None:
        try:
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
            self._csv_file = open(self.csv_path, 'w', newline='', encoding='utf-8')
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow(self.columns)
            self._csv_file.flush()
        except Exception as e:
            logger.error(f"Failed to initialize CSV export: {e}")
            raise

    def write_row(self, row: List[Any]) -> None:
        if self._finalized:
            raise RuntimeError("Cannot write to finalized exporter")
        self._data_rows.append(row)
        if self._csv_writer:
            formatted = [
                f"{v:.6g}" if isinstance(v, float) else str(v)
                for v in row
            ]
            self._csv_writer.writerow(formatted)

    def flush(self, checkpoint: bool = True) -> None:
        if self._csv_file:
            try:
                self._csv_file.flush()
                os.fsync(self._csv_file.fileno())
            except Exception as e:
                logger.warning(f"Failed to flush CSV: {e}")
        if checkpoint and len(self._data_rows) > self._last_checkpoint_count:
            self._write_checkpoint()

    def _write_checkpoint(self) -> None:
        checkpoint_path = self.base_path.with_suffix('.json.tmp')
        try:
            checkpoint_data = {
                "format_version": self.FORMAT_VERSION,
                "meta": {
                    **self.metadata,
                    "_checkpoint": True,
                    "_checkpoint_time": datetime.now().isoformat(),
                },
                "columns": self.columns,
                "units": self.units,
                "row_count": len(self._data_rows),
                "data": self._data_rows
            }
            temp_path = self.base_path.with_suffix('.json.tmp.writing')
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(checkpoint_data, f, indent=2, ensure_ascii=False)
            temp_path.replace(checkpoint_path)
            self._last_checkpoint_count = len(self._data_rows)
        except Exception as e:
            logger.warning(f"Failed to write checkpoint: {e}")

    def finalize(self, end_metadata: Optional[Dict[str, Any]] = None) -> None:
        if self._finalized:
            return
        if self._csv_file:
            try:
                self._csv_file.flush()
                self._csv_file.close()
            except Exception as e:
                logger.warning(f"Error closing CSV: {e}")
            finally:
                self._csv_file = None
                self._csv_writer = None
        final_meta = dict(self.metadata)
        if end_metadata:
            final_meta.update(end_metadata)
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
            checkpoint_path = self.base_path.with_suffix('.json.tmp')
            if checkpoint_path.exists():
                try:
                    checkpoint_path.unlink()
                except Exception as e:
                    logger.warning(f"Failed to remove checkpoint file: {e}")
        except Exception as e:
            logger.error(f"Failed to write JSON: {e}")
            raise
        self._finalized = True

    @property
    def output_paths(self) -> List[Path]:
        return [self.csv_path, self.json_path]

    @property
    def row_count(self) -> int:
        return len(self._data_rows)

    @staticmethod
    def recover_from_checkpoint(checkpoint_path: Union[str, Path]) -> Optional[Dict[str, Any]]:
        checkpoint_path = Path(checkpoint_path)
        if not checkpoint_path.exists():
            logger.warning(f"Checkpoint file not found: {checkpoint_path}")
            return None
        try:
            with open(checkpoint_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if 'meta' in data:
                data['meta']['_recovered'] = True
                data['meta']['_recovered_from'] = str(checkpoint_path)
                data['meta'].pop('_checkpoint', None)
                data['meta'].pop('_checkpoint_time', None)
            return data
        except Exception as e:
            logger.error(f"Failed to recover from checkpoint: {e}")
            return None

    @staticmethod
    def find_checkpoints(directory: Union[str, Path]) -> List[Path]:
        directory = Path(directory)
        if not directory.is_dir():
            return []
        return list(directory.glob('**/*.json.tmp'))


# Back-compat alias for code that imports DualExporter directly.
# New call sites should use make_exporter() instead.
DualExporter = LegacyDualExporter


# --------------------------------- Factory ---------------------------------


def make_exporter(
    base_path: Union[str, Path],
    metadata: Dict[str, Any],
    columns: List[str],
    units: Optional[List[str]] = None,
    output_settings: Optional[Dict[str, Any]] = None,
    on_compress: Optional[Callable[[Path, Path, float, float], None]] = None,
    on_large_file: Optional[Callable[[Path, float], None]] = None,
) -> _BaseExporter:
    """Construct the exporter chosen by ``output_settings['format']``.

    Supported formats:

    - ``csv`` (default) — ``CsvExporter`` with optional gzip on finalize
    - ``hdf5`` — ``Hdf5Exporter`` (requires optional ``h5py``)
    - ``csv+legacy_json`` — ``LegacyDualExporter`` (pre-2.0 dual emit)

    ``output_settings`` shape::

        {
            "format": "csv" | "hdf5" | "csv+legacy_json",
            "compression": "never" | "always" | "auto",
            "compression_threshold_mb": 5,
        }

    Unknown formats fall back to ``csv`` with a logged warning.
    """
    output_settings = output_settings or {}
    fmt = output_settings.get('format', 'csv')
    if fmt == 'hdf5':
        return Hdf5Exporter(base_path, metadata, columns, units)
    if fmt == 'csv+legacy_json':
        return LegacyDualExporter(base_path, metadata, columns, units)
    if fmt != 'csv':
        logger.warning(f"Unknown output format '{fmt}', falling back to 'csv'")
    return CsvExporter(
        base_path,
        metadata,
        columns,
        units,
        compression=output_settings.get('compression', 'never'),
        threshold_mb=float(output_settings.get('compression_threshold_mb', 5.0)),
        on_compress=on_compress,
        on_large_file=on_large_file,
    )

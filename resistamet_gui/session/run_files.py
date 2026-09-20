"""Where a run's data files go and which exporter writes them.

Split out of ``workers.py`` unchanged. Pure path and exporter construction: no
Qt, no instrument, so the headless session builds a run's files the same way
the GUI worker does.
"""
import re
import time
from datetime import datetime
from pathlib import Path

from ..data_export import build_metadata, get_column_config, make_exporter


MODE_FILE_TAGS = {
    'resistance': 'R',
    'source_v': 'VSRC',
    'source_i': 'ISRC',
    'four_point': '4PP',
    'vdp': 'vdP',
}


def sanitize_path_component(name: str) -> str:
    """Sanitize a string for safe use in file paths.

    Removes path traversal characters and special characters that could
    cause security issues or file system problems.
    """
    # Remove path traversal sequences
    sanitized = re.sub(r'\.\.+', '', name)
    sanitized = re.sub(r'[/\\]', '', sanitized)
    # Replace non-alphanumeric characters with underscores
    sanitized = ''.join(c if c.isalnum() or c in '-_' else '_' for c in sanitized)
    # Remove leading/trailing underscores and collapse multiple underscores
    sanitized = re.sub(r'_+', '_', sanitized).strip('_')
    return sanitized or 'unnamed'


def create_base_path(data_directory, username, sample_name, mode, source_value_str,
                      timestamp=None) -> Path:
    """Base path for one run's data files, without an extension.

    Username and sample name are sanitized against path traversal and
    cross-platform filename rules; the exporter adds the extension(s).
    """
    base_dir = Path(data_directory)
    base_dir.mkdir(parents=True, exist_ok=True)

    user_dir = base_dir / sanitize_path_component(username)
    user_dir.mkdir(exist_ok=True)

    stamp = int(time.time()) if timestamp is None else int(timestamp)
    mode_tag = MODE_FILE_TAGS.get(mode, 'DATA')
    base_name = f"{stamp}_{sanitize_path_component(sample_name)}_{mode_tag}_{source_value_str}"
    return user_dir / base_name


def open_exporter(base_path, mode, settings, measurement_settings, username, sample_name,
                   instrument_idn, start_time, aux_columns, aux_units,
                   on_compress=None, on_large_file=None, effective=None, spot=None):
    """Build the run's exporter. Returns (exporter, primary filename).

    ``effective``: settings the instrument reported back after configuration,
    recorded beside the requested ones (see ``build_metadata``).

    ``spot``: the header block of a four-point run that is one placement of a
    map; None for every other run.
    """
    columns, units = get_column_config(
        mode, measurement_settings,
        aux_columns=aux_columns or None,
        aux_units=aux_units or None,
    )
    export_metadata = build_metadata(
        user=username,
        sample_name=sample_name,
        mode=mode,
        settings=settings,
        instrument_idn=instrument_idn,
        start_time=datetime.fromtimestamp(start_time),
        aux_columns=aux_columns or None,
        effective=effective,
        spot=spot,
    )

    exporter = make_exporter(
        base_path=base_path,
        metadata=export_metadata,
        columns=columns,
        units=units,
        output_settings=settings.get('output'),
        on_compress=on_compress,
        on_large_file=on_large_file,
    )
    try:
        primary_paths = exporter.output_paths
        filename = str(primary_paths[0]) if primary_paths else str(base_path)
    except Exception:
        # The caller gets no exporter back, so nobody else can close the
        # file that was just created.
        try:
            exporter.finalize()
        except Exception:
            pass
        raise
    return exporter, filename

"""A four-point map, assembled from the run files that share a ``map_id``.

A spot is a run; a map is the set of runs whose headers carry the same
``spot.map_id`` (``docs/design/four_point_probe_spots.md``). Nothing else
holds a map together, so it is rebuilt from the files whenever it is asked
for: read the headers and footers, keep the newest run of each spot index,
and take the spread between the spots' means.

The summary is also written beside the runs as ``<map_id>_map.json`` so the
archive holds the map and not only whichever UI happened to be open. The
summary is derived data and is replaced whole; the run files are only ever
read. Nothing here deletes anything.

Runs are found in one directory, not its subdirectories: the runs of a map
are one operator's, written side by side. Only the v2 CSV (plain or gzipped)
and HDF5 files carry the header this reads; the legacy CSV+JSON pair does not.

No Qt, no instrument.
"""
import logging
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
from pydantic import Field

from ..data_export import parse_metadata
from ..schema.spots import MAP_ID_PATTERN
from .events import EventModel, SpotStats
from .run_files import MODE_FILE_TAGS

logger = logging.getLogger(__name__)

#: Files a four-point run writes that carry the v2 header.
RUN_SUFFIXES = ('.csv', '.csv.gz', '.h5')

#: Every run file is named ``<stamp>_<sample>_<mode tag>_<source>``. Only
#: four-point files are opened: reading the footer of a gzipped file means
#: decompressing all of it, and a data directory holds overnight runs of the
#: other modes that are far larger than any spot.
_FOUR_POINT_TAG = f"_{MODE_FILE_TAGS['four_point']}_"

#: The end of a map summary's file name: ``<map_id>_map.json``. No run ends
#: this way (a run's name ends in its source value), so it also tells a
#: summary from a legacy run's ``.json`` when listing a data directory.
MAP_SUMMARY_SUFFIX = '_map.json'

#: Header values that are identifiers, read back as written.
_TEXT_KEYS = ('spot.map_id', 'spot.label', 'sample', 'started_at', 'spot_stats.end_reason')

QUANTITIES = ('rs', 'rho', 'sigma')


class MapSpot(EventModel):
    """One spot of a map: the newest run with that index."""

    index: int
    label: str
    x_mm: Optional[float] = None
    y_mm: Optional[float] = None
    angle_deg: Optional[float] = None
    #: Present when the run knew the spot's position on a bounded sample:
    #: against the outline's centre, and against the factor the rows applied.
    relative_error: Optional[float] = None
    relative_error_rows: Optional[float] = None
    edge_clearance_s: Optional[float] = None
    sample: Optional[str] = None
    started_at: Optional[str] = None
    #: File name of the run, relative to the map's directory.
    file: str
    #: Older runs of the same index, newest first. Kept on disk, not used.
    superseded: List[str] = Field(default_factory=list)
    stats: SpotStats


class InterSpotStats(EventModel):
    """The spread of one quantity between the spots of a map.

    Over the spots' means, each spot counting once. ``sd`` is the sample
    standard deviation and needs two spots; ``rsd_pct`` is ``sd / |mean|``.
    """

    n: int
    mean: Optional[float] = None
    sd: Optional[float] = None
    rsd_pct: Optional[float] = None


class SkippedRun(EventModel):
    """A run that names this map but cannot stand for a spot."""

    file: str
    reason: str


class SpotMap(EventModel):
    """A map: its spots in index order and the spread between them."""

    map_id: str
    spots: List[MapSpot] = Field(default_factory=list)
    rs: InterSpotStats
    rho: InterSpotStats
    sigma: InterSpotStats
    skipped: List[SkippedRun] = Field(default_factory=list)


def inter_spot_statistics(means: List[Optional[float]]) -> InterSpotStats:
    """n, mean, sample SD and RSD over the finite values of ``means``."""
    finite = [float(m) for m in means if isinstance(m, (int, float)) and math.isfinite(m)]
    n = len(finite)
    if n == 0:
        return InterSpotStats(n=0)
    mean = math.fsum(finite) / n
    if n < 2:
        return InterSpotStats(n=n, mean=mean)
    sd = math.sqrt(math.fsum((m - mean) ** 2 for m in finite) / (n - 1))
    rsd_pct = sd / abs(mean) * 100.0 if mean != 0 else None
    return InterSpotStats(n=n, mean=mean, sd=sd, rsd_pct=rsd_pct)


def read_run_metadata(path: Path) -> Dict[str, Any]:
    """The flat header-and-footer dict of one run file, CSV or HDF5."""
    if path.name.endswith('.h5'):
        return _read_hdf5_attributes(path)
    return parse_metadata(path, text_keys=_TEXT_KEYS)


def _read_hdf5_attributes(path: Path) -> Dict[str, Any]:
    """HDF5 keeps the same flat keys as file attributes, already typed."""
    import h5py  # optional; the caller reports ImportError as 'h5py not installed'

    with h5py.File(path, 'r') as handle:
        meta = {}
        for key, value in handle.attrs.items():
            if isinstance(value, np.generic):
                value = value.item()
            if isinstance(value, bytes):
                value = value.decode('utf-8', errors='replace')
            # The exporter writes None as an empty string.
            meta[key] = None if isinstance(value, str) and value == '' else value
        return meta


def _four_point_files(directory: Path) -> List[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir()
                  if path.is_file() and path.name.endswith(RUN_SUFFIXES)
                  and _FOUR_POINT_TAG in path.name)


def _map_runs(directory: Path) -> Tuple[List[Tuple[Path, Dict[str, Any]]], List[SkippedRun]]:
    """The four-point runs in ``directory`` that name a map, and the files
    that could not be read at all.

    An unreadable file cannot say which map it belongs to, so it is reported
    with every map of the directory rather than dropped from all of them: an
    HDF5 run on a machine without h5py is a spot that is missing from the
    map, and the map should say why.
    """
    runs, unreadable = [], []
    for path in _four_point_files(directory):
        try:
            meta = read_run_metadata(path)
        except ImportError:
            unreadable.append(SkippedRun(file=path.name, reason='h5py not installed'))
            continue
        except Exception as exc:
            unreadable.append(SkippedRun(file=path.name, reason=f"unreadable: {_brief(exc)}"))
            continue
        if meta.get('mode') == 'four_point' and meta.get('spot.map_id') not in (None, ''):
            runs.append((path, meta))
    return runs, unreadable


def list_map_ids(directory: Union[str, Path]) -> List[str]:
    """The map ids named by the four-point runs in ``directory``, sorted."""
    runs, _ = _map_runs(Path(directory))
    ids = {str(meta['spot.map_id']) for _, meta in runs}
    return sorted(map_id for map_id in ids if re.fullmatch(MAP_ID_PATTERN, map_id))


#: ``<unix stamp>_...`` at the front of a run's file name, and the ``-2``,
#: ``-3`` the exporter appends when that name is already taken.
_NAME_STAMP = re.compile(r'^(\d+)_')
_NAME_REPEAT = re.compile(r'-(\d+)$')


def _started_timestamp(meta: Dict[str, Any]) -> float:
    """The header's ``started_at`` as Unix seconds; -inf when it has none.

    A float, so a header written with a UTC offset and one written without
    can be compared (two aware/naive datetimes cannot).
    """
    try:
        return datetime.fromisoformat(str(meta.get('started_at'))).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return float('-inf')


def _newest_first_key(path: Path, meta: Dict[str, Any]) -> Tuple[float, float, int, str]:
    """What "newest" means between two runs of one spot.

    The Unix stamp at the front of the file name first: it is UTC seconds, so
    it orders runs across a daylight-saving change and between two PCs with
    different zone settings, where the header's local ``started_at`` does
    not. Then ``started_at``, which has the sub-second part. Then the ``-N``
    the exporter adds to a name that was taken, compared as a number so
    ``-10`` follows ``-9``. Then the name. A file renamed so that it has no
    stamp is placed by its ``started_at``.
    """
    name = path.name
    for suffix in RUN_SUFFIXES:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    started = _started_timestamp(meta)
    stamp = _NAME_STAMP.match(name)
    repeat = _NAME_REPEAT.search(name)
    return (float(stamp.group(1)) if stamp else started, started,
            int(repeat.group(1)) if repeat else 1, path.name)


def _brief(exc: Exception) -> str:
    """One line of an exception, for a ``skipped`` reason."""
    errors = exc.errors() if hasattr(exc, 'errors') else None
    if errors:
        first = errors[0]
        where = '.'.join(str(part) for part in first.get('loc', ()))
        return f"{where}: {first.get('msg')}" if where else str(first.get('msg'))
    text = str(exc).strip().splitlines()
    return text[0] if text else type(exc).__name__


def _spot_stats(meta: Dict[str, Any]) -> Optional[SpotStats]:
    """The footer's statistics block, or None when the run never wrote one."""
    if 'spot_stats.n' not in meta:
        return None
    block: Dict[str, Any] = {
        'n': meta['spot_stats.n'],
        'n_excluded': meta.get('spot_stats.n_excluded') or 0,
        # Absent in files written before it was recorded.
        'end_reason': meta.get('spot_stats.end_reason') or None,
    }
    for quantity in QUANTITIES:
        prefix = f'spot_stats.{quantity}.'
        block[quantity] = {key[len(prefix):]: value for key, value in meta.items()
                           if key.startswith(prefix)}
        block[quantity].setdefault('n', 0)
    return SpotStats.model_validate(block)


def _map_spot(path: Path, meta: Dict[str, Any], stats: SpotStats) -> MapSpot:
    """One run as a spot. Raises when the header's values are not what they
    should be (``spot.x_mm: abc``)."""
    return MapSpot(
        index=int(meta['spot.index']),
        label=str(meta.get('spot.label') or ''),
        x_mm=meta.get('spot.x_mm'),
        y_mm=meta.get('spot.y_mm'),
        angle_deg=meta.get('spot.angle_deg'),
        relative_error=meta.get('spot.relative_error'),
        relative_error_rows=meta.get('spot.relative_error_rows'),
        edge_clearance_s=meta.get('spot.edge_clearance_s'),
        sample=None if meta.get('sample') is None else str(meta.get('sample')),
        started_at=None if meta.get('started_at') is None else str(meta.get('started_at')),
        file=path.name,
        stats=stats,
    )


def assemble_map(directory: Union[str, Path], map_id: str) -> SpotMap:
    """The map ``map_id`` as the run files in ``directory`` describe it.

    The newest run of each spot index stands for the spot; older ones are
    listed as superseded. A run that cannot stand for a spot -- it has no
    footer because it never finished, or no sample outside compliance -- is
    listed under ``skipped`` and does not displace a good earlier run.
    """
    if not re.fullmatch(MAP_ID_PATTERN, map_id):
        raise ValueError(f"'{map_id}' is not a valid map id")
    runs, skipped = _map_runs(Path(directory))
    candidates: Dict[int, List[Tuple[Tuple, MapSpot]]] = {}
    for path, meta in runs:
        if str(meta['spot.map_id']) != map_id:
            continue
        # Everything read from the file is validated here, inside the guard:
        # one run with a header somebody edited must cost the map that run,
        # not the whole map.
        try:
            stats = _spot_stats(meta)
            spot = None if stats is None else _map_spot(path, meta, stats)
        except Exception as exc:
            skipped.append(SkippedRun(file=path.name,
                                      reason=f"unreadable spot block: {_brief(exc)}"))
            continue
        if stats is None:
            skipped.append(SkippedRun(file=path.name, reason='no footer: the run did not finish'))
        elif stats.rs.n < 1:
            skipped.append(SkippedRun(file=path.name, reason='no valid sample'))
        else:
            candidates.setdefault(spot.index, []).append((_newest_first_key(path, meta), spot))

    spots = []
    for index in sorted(candidates):
        ordered = [spot for _, spot in sorted(candidates[index], key=lambda run: run[0],
                                              reverse=True)]
        spots.append(ordered[0].model_copy(
            update={'superseded': [older.file for older in ordered[1:]]}))
    return SpotMap(
        map_id=map_id,
        spots=spots,
        rs=inter_spot_statistics([spot.stats.rs.mean for spot in spots]),
        rho=inter_spot_statistics([spot.stats.rho.mean for spot in spots]),
        sigma=inter_spot_statistics([spot.stats.sigma.mean for spot in spots]),
        skipped=sorted(skipped, key=lambda run: run.file),
    )


def map_summary_path(directory: Union[str, Path], map_id: str) -> Path:
    if not re.fullmatch(MAP_ID_PATTERN, map_id):
        raise ValueError(f"'{map_id}' is not a valid map id")
    return Path(directory) / f"{map_id}{MAP_SUMMARY_SUFFIX}"


def write_map_summary(directory: Union[str, Path], map_id: str) -> Path:
    """Assemble the map and write ``<map_id>_map.json`` beside the runs.

    Written to a temporary file in the same directory and moved into place,
    so a reader never sees half a summary and a crash leaves the previous one.
    """
    target = map_summary_path(directory, map_id)
    text = assemble_map(directory, map_id).model_dump_json(indent=2) + "\n"
    # A plain open, not mkstemp: the summary should get the permissions every
    # other file in the data directory has, not a private 0600.
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        # Only ever our own temporary file.
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target

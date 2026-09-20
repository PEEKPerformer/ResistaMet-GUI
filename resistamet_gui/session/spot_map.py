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
import hashlib
import json
import logging
import math
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
from pydantic import Field

from ..data_export import parse_metadata
from ..schema.spots import MAP_ID_PATTERN, MapImageRegistration
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

#: The photograph of a map's sample is kept beside the runs as
#: ``<map_id>_sample.<ext>``, and what is known about it -- its hash and where
#: it sits on the sample -- as ``<map_id>_map_image.json``. Neither name can be
#: a run's (a run ends in ``.csv``, ``.csv.gz`` or ``.h5``) or a map summary's.
MAP_IMAGE_STEM = '_sample'
MAP_IMAGE_SIDECAR_SUFFIX = '_map_image.json'

#: Content type -> the extension the stored file gets.
IMAGE_EXTENSIONS = {
    'image/png': 'png',
    'image/jpeg': 'jpg',
    'image/webp': 'webp',
    'image/tiff': 'tif',
}

MAX_IMAGE_BYTES = 25 * 1024 * 1024


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


class MapImage(EventModel):
    """The photograph kept beside a map's runs.

    ``sha256`` and ``bytes`` are those of the file as it is on disk now.
    ``registration`` is absent until a client has stored one, and when the
    stored one was fitted to a different image than the file holds.
    """

    file: str
    sha256: str
    bytes: int
    registration: Optional[MapImageRegistration] = None


class SpotMap(EventModel):
    """A map: its spots in index order and the spread between them."""

    map_id: str
    #: None when no photograph was stored for the map.
    image: Optional[MapImage] = None
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
        image=read_map_image(directory, map_id),
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


# --- the photograph ----------------------------------------------------------

class UnsupportedImage(ValueError):
    """Not one of ``IMAGE_EXTENSIONS``, or the bytes are not what they claim."""


class ImageTooLarge(ValueError):
    """More than ``MAX_IMAGE_BYTES``."""


class MapImageConflict(Exception):
    """The map already has a different image, or the registration names one
    the map does not hold."""


#: One server process writes here; the lock makes "is there an image already"
#: and "put this one in place" a single step between its request threads.
_IMAGE_LOCK = threading.Lock()


def image_content_type(data: bytes) -> Optional[str]:
    """The image type the first bytes of ``data`` announce, or None.

    The signature only: nothing is decoded, so a file that starts like a PNG
    and is damaged further on is stored as the PNG the operator chose.
    """
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data[:4] in (b'II*\x00', b'MM\x00*', b'II+\x00', b'MM\x00+'):
        return 'image/tiff'
    return None


def _check_map_id(map_id: str) -> None:
    if not re.fullmatch(MAP_ID_PATTERN, map_id):
        raise ValueError(f"'{map_id}' is not a valid map id")


def map_image_sidecar_path(directory: Union[str, Path], map_id: str) -> Path:
    _check_map_id(map_id)
    return Path(directory) / f"{map_id}{MAP_IMAGE_SIDECAR_SUFFIX}"


def find_map_image(directory: Union[str, Path], map_id: str) -> Optional[Path]:
    """The map's image file, or None.

    Only a regular file that really is in ``directory``: a link planted under
    the image's name is not followed to whatever it points at.
    """
    _check_map_id(map_id)
    directory = Path(directory)
    for extension in IMAGE_EXTENSIONS.values():
        path = directory / f"{map_id}{MAP_IMAGE_STEM}.{extension}"
        if path.is_symlink() or not path.is_file():
            continue
        if path.resolve().parent != directory.resolve():
            continue
        return path
    return None


def map_image_media_type(path: Path) -> str:
    """The content type a stored image is served with."""
    for content_type, extension in IMAGE_EXTENSIONS.items():
        if path.name.endswith(f".{extension}"):
            return content_type
    return 'application/octet-stream'


def _sha256_of_file(path: Path) -> Tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _read_sidecar(directory: Path, map_id: str) -> Dict[str, Any]:
    try:
        with open(map_image_sidecar_path(directory, map_id), 'r', encoding='utf-8') as handle:
            found = json.load(handle)
        return found if isinstance(found, dict) else {}
    except (OSError, ValueError):
        return {}


def read_map_image(directory: Union[str, Path], map_id: str) -> Optional[MapImage]:
    """What the files say about the map's image; None when it has none.

    The hash is taken from the image file each time, not read back from the
    sidecar, so the block describes the picture that is there. A registration
    fitted to another picture (the file was exchanged by hand) is left out
    rather than shown against the wrong pixels; so is one that does not parse.
    """
    directory = Path(directory)
    path = find_map_image(directory, map_id)
    if path is None:
        return None
    try:
        sha256, size = _sha256_of_file(path)
    except OSError as exc:
        logger.warning("map image %s could not be read: %s", path.name, exc)
        return None
    registration = None
    raw = _read_sidecar(directory, map_id).get('registration')
    if isinstance(raw, dict) and raw.get('sha256') == sha256:
        try:
            registration = MapImageRegistration.model_validate(raw)
        except ValueError as exc:
            logger.warning("registration of map %s ignored: %s", map_id, _brief(exc))
    return MapImage(file=path.name, sha256=sha256, bytes=size, registration=registration)


def _write_atomically(target: Path, data: bytes) -> None:
    """Temporary file in the same directory, flushed, then moved into place."""
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(temporary, 'wb') as handle:
            handle.write(data)
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


def _write_sidecar(directory: Path, map_id: str, image: MapImage) -> None:
    text = image.model_dump_json(indent=2) + "\n"
    _write_atomically(map_image_sidecar_path(directory, map_id), text.encode('utf-8'))


def _set_aside(path: Path) -> Path:
    """Rename a replaced image to ``<name>.replaced-<UTC stamp>``; never delete."""
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target = path.with_name(f"{path.name}.replaced-{stamp}")
    repeat = 1
    while target.exists() or target.is_symlink():
        repeat += 1
        target = path.with_name(f"{path.name}.replaced-{stamp}-{repeat}")
    os.rename(path, target)
    return target


def store_map_image(directory: Union[str, Path], map_id: str, data: bytes,
                    content_type: str, *, replace: bool = False
                    ) -> Tuple[MapImage, bool, Optional[str]]:
    """Keep ``data`` beside the map's runs as ``<map_id>_sample.<ext>``.

    Returns the image, whether anything was written, and the new name of the
    image this one replaced. The same bytes again change nothing. Different
    bytes raise ``MapImageConflict`` unless ``replace``, and then the old file
    is renamed, not deleted: a figure somebody made from it can still be
    traced to its picture.
    """
    _check_map_id(map_id)
    if content_type not in IMAGE_EXTENSIONS:
        raise UnsupportedImage(f"'{content_type}' is not one of {', '.join(IMAGE_EXTENSIONS)}")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageTooLarge(f"the image is {len(data)} bytes; the limit is {MAX_IMAGE_BYTES}")
    announced = image_content_type(data)
    if announced != content_type:
        raise UnsupportedImage(
            f"the data is not {content_type}"
            + (f": it starts like {announced}" if announced else ""))
    directory = Path(directory)
    sha256 = hashlib.sha256(data).hexdigest()
    with _IMAGE_LOCK:
        current = read_map_image(directory, map_id)
        if current is not None and current.sha256 == sha256:
            return current, False, None
        if current is not None and not replace:
            raise MapImageConflict(
                f"map '{map_id}' already has a different image ({current.file}, "
                f"sha256 {current.sha256})")
        directory.mkdir(parents=True, exist_ok=True)
        set_aside = None if current is None else _set_aside(directory / current.file).name
        target = directory / f"{map_id}{MAP_IMAGE_STEM}.{IMAGE_EXTENSIONS[content_type]}"
        if target.exists() or target.is_symlink():
            # Not an image this module would serve (a link, a directory), so
            # it was not set aside above; it is not ours to write through.
            raise MapImageConflict(f"'{target.name}' exists and is not a stored image")
        _write_atomically(target, data)
        # A registration belongs to the picture it was fitted to: a new
        # picture starts without one.
        image = MapImage(file=target.name, sha256=sha256, bytes=len(data))
        _write_sidecar(directory, map_id, image)
        return image, True, set_aside


def store_map_registration(directory: Union[str, Path], map_id: str,
                           registration: MapImageRegistration) -> MapImage:
    """Record where the map's image sits on the sample, in the sidecar.

    Raises ``LookupError`` when the map has no image and ``MapImageConflict``
    when the registration was fitted to a different one.
    """
    directory = Path(directory)
    with _IMAGE_LOCK:
        current = read_map_image(directory, map_id)
        if current is None:
            raise LookupError(f"map '{map_id}' has no image")
        if current.sha256 != registration.sha256:
            raise MapImageConflict(
                f"the registration is for image {registration.sha256}; map '{map_id}' "
                f"holds {current.sha256}")
        image = current.model_copy(update={'registration': registration})
        _write_sidecar(directory, map_id, image)
        return image

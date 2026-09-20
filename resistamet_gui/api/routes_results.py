"""Browse what runs have written.

The Results Viewer's backing routes: list the files under the data directory
and read one back. Paths are validated against the data directory before
anything is opened — a client asking for ``../config.json`` gets 400, not the
config.

Which directory: a run is written to ``<data_directory>/<username>``, with
the directory taken from the operator's own profile and the name sanitized
(``run_files.create_base_path``), and ``/maps`` reads it back the same way.
So do these routes. Given a ``user`` they look in that operator's directory;
given none they look in every known operator's, and in the shared default.
"""
import os
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ..session.run_files import sanitize_path_component
from ..session.spot_map import MAP_IMAGE_SIDECAR_SUFFIX, MAP_SUMMARY_SUFFIX
from .app import require_token

router = APIRouter(prefix="/results", tags=["results"])

#: What a run can produce; anything else in the directory is ignored.
RESULT_SUFFIXES = ('.csv', '.csv.gz', '.h5', '.json')

#: Cap on a single read: a 17-hour run is ~13 MB and that is what a browser
#: can hold comfortably. Larger files are still listed, just not previewed.
MAX_PREVIEW_BYTES = 32 * 1024 * 1024


class ResultFile(BaseModel):
    path: str
    name: str
    user: Optional[str]
    size: int
    modified: float


def _directory_of(section: dict) -> Path:
    return Path(str(section.get('data_directory', 'measurement_data'))).resolve()


def _data_directory(request: Request, user: Optional[str] = None) -> Path:
    """Where ``user``'s runs go; without one, the last operator's, else the default."""
    state = request.app.state.api
    config = state.stored_config
    user = user or (config.get_last_user() if config is not None else None)
    if user:
        return _directory_of(state.profile_provider(user).get('file', {}))
    return _directory_of(config.config.get('file', {}) if config is not None else {})


def _data_directories(request: Request, user: Optional[str] = None) -> List[Path]:
    """Every directory a listed file can be in, without repeats."""
    if user:
        return [_data_directory(request, user)]
    config = request.app.state.api.stored_config
    if config is None:
        return [_data_directory(request)]
    roots = [_data_directory(request, name) for name in config.get_users()]
    roots.append(_directory_of(config.config.get('file', {})))
    return list(dict.fromkeys(roots))


def _safe_path(request: Request, relative: str, user: Optional[str] = None) -> Path:
    """Resolve a client path inside a data directory or refuse.

    A path names a file relative to the directory it was listed from. It is
    tried against each in turn; one that leaves every directory is refused.
    """
    inside = []
    for root in _data_directories(request, user):
        candidate = (root / relative).resolve()
        if root == candidate or root in candidate.parents:
            inside.append(candidate)
    if not inside:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                             detail="path is outside the data directory")
    return next((candidate for candidate in inside if candidate.is_file()), inside[0])


@router.get("")
def list_results(request: Request, user: Optional[str] = Query(default=None),
                  limit: int = Query(default=500, ge=1, le=5000),
                  role: str = Depends(require_token)) -> dict:
    """Newest first. ``user`` narrows to one operator's directory.

    ``user`` on a file is the name of the folder it is in, which is the
    operator's name as ``sanitize_path_component`` wrote it ("Anna Lee" is
    ``Anna_Lee``), so the filter compares that form.
    """
    roots = _data_directories(request, user)
    folder = sanitize_path_component(user) if user else None
    files: List[ResultFile] = []
    seen = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob('*'):
            if not path.is_file() or not path.name.endswith(RESULT_SUFFIXES):
                continue
            if path.name.endswith((MAP_SUMMARY_SUFFIX, MAP_IMAGE_SIDECAR_SUFFIX)):
                # A four-point map's summary or its image record, not a run:
                # both are served by /maps. '.json' is listed for the legacy
                # CSV+JSON pair, which is how these got in.
                continue
            if path in seen:
                continue  # one directory inside another
            seen.add(path)
            relative = path.relative_to(root)
            owner = relative.parts[0] if len(relative.parts) > 1 else None
            if folder and owner != folder:
                continue
            info = path.stat()
            # Forward slashes on every platform: the path is a key the client
            # hands back, and Path accepts either separator when resolving it.
            files.append(ResultFile(path=relative.as_posix(), name=path.name, user=owner,
                                    size=info.st_size, modified=info.st_mtime))
    files.sort(key=lambda f: f.modified, reverse=True)
    return {"root": str(roots[0]), "files": [f.model_dump() for f in files[:limit]]}


@router.get("/file", response_class=PlainTextResponse)
def read_result(request: Request, path: str = Query(min_length=1),
                 user: Optional[str] = Query(default=None),
                 role: str = Depends(require_token)) -> str:
    """The file's text. CSV only; HDF5 is binary and gets 415."""
    target = _safe_path(request, path, user)
    if not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such file")
    if not target.name.endswith('.csv'):
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                             detail="only .csv files can be previewed")
    if target.stat().st_size > MAX_PREVIEW_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                             detail="file too large to preview; open it from the data directory")
    with open(target, 'r', encoding='utf-8', errors='replace') as handle:
        return handle.read()


@router.get("/directory")
def data_directory(request: Request, user: Optional[str] = Query(default=None),
                    role: str = Depends(require_token)) -> dict:
    """Where the files are, absolute, so a shell can open the folder."""
    root = _data_directory(request, user)
    return {"root": str(root), "exists": root.exists(), "separator": os.sep}

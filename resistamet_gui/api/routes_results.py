"""Browse what runs have written.

The Results Viewer's backing routes: list the files under the data directory
and read one back. Paths are validated against the data directory before
anything is opened — a client asking for ``../config.json`` gets 400, not the
config.
"""
import os
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

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


def _data_directory(request: Request) -> Path:
    config = request.app.state.api.config
    directory = Path(str(config.config.get('file', {}).get('data_directory', 'measurement_data')))
    return directory.resolve()


def _safe_path(request: Request, relative: str) -> Path:
    """Resolve a client path inside the data directory or refuse."""
    root = _data_directory(request)
    candidate = (root / relative).resolve()
    if root != candidate and root not in candidate.parents:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                             detail="path is outside the data directory")
    return candidate


@router.get("")
def list_results(request: Request, user: Optional[str] = Query(default=None),
                  limit: int = Query(default=500, ge=1, le=5000),
                  role: str = Depends(require_token)) -> dict:
    """Newest first. ``user`` narrows to one operator's directory."""
    root = _data_directory(request)
    if not root.exists():
        return {"root": str(root), "files": []}
    files: List[ResultFile] = []
    for path in root.rglob('*'):
        if not path.is_file() or not path.name.endswith(RESULT_SUFFIXES):
            continue
        relative = path.relative_to(root)
        owner = relative.parts[0] if len(relative.parts) > 1 else None
        if user and owner != user:
            continue
        info = path.stat()
        # Forward slashes on every platform: the path is a key the client hands
        # back, and Path accepts either separator when resolving it.
        files.append(ResultFile(path=relative.as_posix(), name=path.name, user=owner,
                                size=info.st_size, modified=info.st_mtime))
    files.sort(key=lambda f: f.modified, reverse=True)
    return {"root": str(root), "files": [f.model_dump() for f in files[:limit]]}


@router.get("/file", response_class=PlainTextResponse)
def read_result(request: Request, path: str = Query(min_length=1),
                 role: str = Depends(require_token)) -> str:
    """The file's text. CSV only; HDF5 is binary and gets 415."""
    target = _safe_path(request, path)
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
def data_directory(request: Request, role: str = Depends(require_token)) -> dict:
    """Where the files are, absolute, so a shell can open the folder."""
    root = _data_directory(request)
    return {"root": str(root), "exists": root.exists(), "separator": os.sep}

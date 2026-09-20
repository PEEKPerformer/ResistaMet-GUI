"""Four-point maps, read from the run files.

A map is the set of four-point runs that share a ``map_id``; nothing stores
it, so these routes assemble it from the operator's run files each time
(``session/spot_map.py``). The spots are read-only: a map changes by
measuring a spot, not by calling this.

The one thing a client puts here is the photograph of the sample and where it
sits on it (``PUT .../image``, ``PUT .../registration``): the picture is data,
so it is kept beside the runs and not in whichever browser was open. A
different image never takes an image's place unasked, and a replaced image is
renamed, not deleted.

The directory is the one a run by that operator writes into --
``<data_directory>/<username>`` from the operator's own profile -- so a map is
looked for exactly where its runs were put. The map id is validated as a
plain token before it gets near a path, and the username goes through the
same sanitizer the run files' directory did.
"""
from pathlib import Path as FilePath

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import FileResponse

from ..schema.spots import MAP_ID_PATTERN, MapImageRegistration
from ..session.run_files import sanitize_path_component
from ..session.spot_map import (
    IMAGE_EXTENSIONS,
    MAX_IMAGE_BYTES,
    ImageTooLarge,
    MapImageConflict,
    UnsupportedImage,
    assemble_map,
    find_map_image,
    list_map_ids,
    map_image_media_type,
    store_map_image,
    store_map_registration,
    write_map_summary,
)
from .app import require_token

logger = logging.getLogger(__name__)

#: By number: starlette renamed the constant for 413 between releases.
_TOO_LARGE = 413

router = APIRouter(prefix="/maps", tags=["maps"])


def _operator_directory(request: Request, user: str) -> FilePath:
    """Where ``user``'s runs are written (``run_files.create_base_path``)."""
    profile = request.app.state.api.profile_provider(user)
    root = FilePath(str(profile.get('file', {}).get('data_directory', 'measurement_data')))
    return (root / sanitize_path_component(user)).resolve()


@router.get("")
def list_maps(request: Request, user: str = Query(min_length=1),
               role: str = Depends(require_token)) -> dict:
    """The map ids the operator's four-point runs name."""
    return {"user": user, "maps": list_map_ids(_operator_directory(request, user))}


@router.get("/{map_id}")
def read_map(request: Request, map_id: str = Path(pattern=MAP_ID_PATTERN),
              user: str = Query(min_length=1),
              role: str = Depends(require_token)) -> dict:
    """The map as the run files describe it now: a ``SpotMap``."""
    found = assemble_map(_operator_directory(request, user), map_id)
    # A photograph may be stored before the first spot is measured.
    if not found.spots and not found.skipped and found.image is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                             detail=f"no run of '{user}' names the map '{map_id}'")
    return found.model_dump()


def _refresh_summary(directory: FilePath, map_id: str) -> None:
    """``<map_id>_map.json`` names the image, so it follows a change to it.

    The image is stored by then; a summary that could not be rewritten is
    rebuilt when the next spot completes, and is not a reason to fail.
    """
    try:
        write_map_summary(directory, map_id)
    except OSError as exc:
        logger.warning("map summary of %s not refreshed: %s", map_id, exc)


async def _body_up_to(request: Request, limit: int) -> bytes:
    """The request body, refused as soon as it is known to exceed ``limit``."""
    too_large = HTTPException(status_code=_TOO_LARGE,
                              detail=f"an image may be at most {limit} bytes")
    declared = request.headers.get('content-length', '')
    if declared.isdigit() and int(declared) > limit:
        raise too_large
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > limit:
            raise too_large
    return bytes(received)


@router.put("/{map_id}/image")
async def store_image(request: Request, response: Response,
                      map_id: str = Path(pattern=MAP_ID_PATTERN),
                      user: str = Query(min_length=1), replace: bool = Query(default=False),
                      role: str = Depends(require_token)) -> dict:
    """Keep the sample's photograph beside the map's runs.

    The body is the image itself and ``Content-Type`` says which kind. 201
    when it was stored, 200 when the map already holds these exact bytes, 409
    when it holds others and ``replace`` was not asked for.
    """
    content_type = request.headers.get('content-type', '').split(';')[0].strip().lower()
    if content_type not in IMAGE_EXTENSIONS:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                            detail=f"Content-Type must be one of {', '.join(IMAGE_EXTENSIONS)}")
    data = await _body_up_to(request, MAX_IMAGE_BYTES)
    directory = _operator_directory(request, user)
    try:
        image, written, set_aside = store_map_image(directory, map_id, data, content_type,
                                                    replace=replace)
    except UnsupportedImage as exc:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc))
    except ImageTooLarge as exc:
        raise HTTPException(status_code=_TOO_LARGE, detail=str(exc))
    except MapImageConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if written:
        _refresh_summary(directory, map_id)
        response.status_code = status.HTTP_201_CREATED
    return {"file": image.file, "sha256": image.sha256, "bytes": image.bytes,
            "replaced": set_aside}


@router.get("/{map_id}/image")
def read_image(request: Request, map_id: str = Path(pattern=MAP_ID_PATTERN),
               user: str = Query(min_length=1),
               role: str = Depends(require_token)) -> FileResponse:
    """The stored photograph, as it was given."""
    path = find_map_image(_operator_directory(request, user), map_id)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"map '{map_id}' of '{user}' has no image")
    return FileResponse(path, media_type=map_image_media_type(path))


@router.put("/{map_id}/registration")
def store_registration(body: MapImageRegistration, request: Request,
                       map_id: str = Path(pattern=MAP_ID_PATTERN),
                       user: str = Query(min_length=1),
                       role: str = Depends(require_token)) -> dict:
    """Record where the stored photograph sits on the sample.

    404 when the map has no image yet; 409 when ``sha256`` names another
    image than the one stored. Returns the map's ``image`` block.
    """
    directory = _operator_directory(request, user)
    try:
        image = store_map_registration(directory, map_id, body)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except MapImageConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    _refresh_summary(directory, map_id)
    return image.model_dump()

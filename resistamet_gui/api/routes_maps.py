"""Four-point maps, read from the run files.

A map is the set of four-point runs that share a ``map_id``; nothing stores
it, so these routes assemble it from the operator's run files each time
(``session/spot_map.py``). Read-only: a map changes by measuring a spot, not
by calling this.

The directory is the one a run by that operator writes into --
``<data_directory>/<username>`` from the operator's own profile -- so a map is
looked for exactly where its runs were put. The map id is validated as a
plain token before it gets near a path, and the username goes through the
same sanitizer the run files' directory did.
"""
from pathlib import Path as FilePath

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from ..schema.spots import MAP_ID_PATTERN
from ..session.run_files import sanitize_path_component
from ..session.spot_map import assemble_map, list_map_ids
from .app import require_token

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
    if not found.spots and not found.skipped:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                             detail=f"no run of '{user}' names the map '{map_id}'")
    return found.model_dump()

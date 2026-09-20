"""A four-point spot, checked before it is measured.

``POST /spots/preflight`` answers what a run would record about a spot --
edge clearance, the geometry factor there and at the centre, the error the
rows would carry, the warning or refusal the run would raise -- without a
run: no instrument, no lock, no events, nothing written. The settings are
resolved as ``POST /session/start`` resolves a four-point request and handed
to the same functions the run calls (``session/spot_record.py``), so the
operator sees before Start the numbers the file will hold.

Its own path rather than ``/maps/preflight``: a pre-flight needs no map to
exist, and ``preflight`` is a valid map id, so under ``/maps`` it would sit
in the namespace of ``/maps/{map_id}``.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..schema.resolve import resolve_run_settings
from ..schema.spots import SpotPreflightRequest
from ..session.spot_record import spot_preflight
from .app import require_token

router = APIRouter(prefix="/spots", tags=["spots"])


@router.post("/preflight")
def preflight(body: SpotPreflightRequest, request: Request,
              role: str = Depends(require_token)) -> dict:
    """A ``SpotPreflight``; 422 where a Start with this body would be 422."""
    profile = request.app.state.api.profile_provider(body.username)
    resolved = resolve_run_settings(profile, 'four_point', body.overrides, strict=True)
    if not resolved.ok:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='; '.join(f"{issue.key}: {issue.message}" for issue in resolved.issues
                             if issue.severity == 'error'))
    if body.spot is not None:
        resolved.settings['spot'] = body.spot.model_dump()
    try:
        return spot_preflight(resolved.settings).model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

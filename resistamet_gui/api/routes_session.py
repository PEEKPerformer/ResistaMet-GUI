"""Session commands over HTTP.

One route per session method. Each validates its input with a pydantic model,
calls the session, and maps the two failure modes: ``SessionBusy`` is 409
(the instrument is doing something else) and a rejected run request is 422
(the settings could not be resolved).
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..session.manager import MeasurementSession, SessionBusy
from .app import UI_ROLE, busy_as_conflict, get_session, require_token

router = APIRouter(prefix="/session", tags=["session"])


class StartRequest(BaseModel):
    mode: str
    sample_name: str = Field(min_length=1)
    username: str = Field(min_length=1)
    overrides: Dict[str, Any] = Field(default_factory=dict)
    prompt_timeout_s: float = Field(default=900.0, gt=0.0)


class AnswerRequest(BaseModel):
    prompt_id: str
    choice: str
    fields: Dict[str, Any] = Field(default_factory=dict)


class MarkRequest(BaseModel):
    label: str = 'MARK'


@router.get("")
def read_status(session: MeasurementSession = Depends(get_session),
                 role: str = Depends(require_token)):
    return session.status()


@router.post("/start", status_code=status.HTTP_202_ACCEPTED)
def start(body: StartRequest, request: Request,
           session: MeasurementSession = Depends(get_session),
           role: str = Depends(require_token)):
    profile = request.app.state.api.profile_provider(body.username)
    try:
        run_id = session.start(profile, body.mode, body.sample_name, body.username,
                                overrides=body.overrides,
                                prompt_timeout_s=body.prompt_timeout_s)
    except SessionBusy as exc:
        raise busy_as_conflict(exc)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                             detail=str(exc))
    return {"run_id": run_id}


@router.post("/stop")
def stop(session: MeasurementSession = Depends(get_session),
          role: str = Depends(require_token)):
    session.stop()
    return session.status()


@router.post("/abort")
def abort(session: MeasurementSession = Depends(get_session),
           role: str = Depends(require_token)):
    session.abort()
    return session.status()


@router.post("/pause")
def pause(session: MeasurementSession = Depends(get_session),
           role: str = Depends(require_token)):
    try:
        session.pause()
    except SessionBusy as exc:
        raise busy_as_conflict(exc)
    return session.status()


@router.post("/resume")
def resume(session: MeasurementSession = Depends(get_session),
            role: str = Depends(require_token)):
    try:
        session.resume()
    except SessionBusy as exc:
        raise busy_as_conflict(exc)
    return session.status()


@router.post("/mark")
def mark(body: MarkRequest, session: MeasurementSession = Depends(get_session),
          role: str = Depends(require_token)):
    try:
        session.mark_event(body.label)
    except SessionBusy as exc:
        raise busy_as_conflict(exc)
    return session.status()


@router.post("/prompt")
def answer_prompt(body: AnswerRequest,
                   session: MeasurementSession = Depends(get_session),
                   role: str = Depends(require_token)):
    pending = session.status()['pending_prompt']
    if pending is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                             detail="no prompt is pending")
    if pending['requires_human'] and role != UI_ROLE:
        # D4: a client that is not a person at the bench may not assert that
        # leads were rewired or that a hazardous voltage was acknowledged.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                             detail="this prompt requires a human answer")
    try:
        accepted = session.answer_prompt(body.prompt_id, body.choice, body.fields)
    except SessionBusy as exc:
        raise busy_as_conflict(exc)
    if not accepted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                             detail="prompt_id is stale or already answered")
    return session.status()

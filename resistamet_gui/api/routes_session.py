"""Session commands over HTTP.

One route per session method. Each validates its input with a pydantic model,
calls the session, and maps the two failure modes: ``SessionBusy`` is 409
(the instrument is doing something else) and a rejected run request is 422
(the settings could not be resolved).
"""
from typing import Annotated, Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, StringConstraints

from ..schema.settings_modes import RunRequest
from ..schema.spots import LABEL_PATTERN
from ..session.instrument_lock import InstrumentBusy
from ..session.manager import MeasurementSession, SessionBusy
from .app import UI_ROLE, busy_as_conflict, get_session, require_token

router = APIRouter(prefix="/session", tags=["session"])


class AnswerRequest(BaseModel):
    prompt_id: str
    choice: str
    fields: Dict[str, Any] = Field(default_factory=dict)
    # Prompt ids repeat from run to run ("safety_voltage_ack-1"). A client
    # that says which run it is answering cannot have a dialog left over from
    # the last run answer this one's question.
    run_id: Optional[str] = None


class MarkRequest(BaseModel):
    # Written into a row of the data file: one line, and short, like the
    # label of a spot.
    label: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=80, pattern=LABEL_PATTERN)] = 'MARK'


@router.get("")
def read_status(session: MeasurementSession = Depends(get_session),
                 role: str = Depends(require_token)):
    return session.status()


@router.post("/shutdown")
def shutdown(request: Request, session: MeasurementSession = Depends(get_session),
              role: str = Depends(require_token)):
    """Ask the sidecar to stop. The run gets its grace period first."""
    session.stop()
    server = getattr(request.app.state.api, 'server', None)
    if server is not None:
        server.should_exit = True
    return {"status": "stopping"}


@router.get("/events")
def read_events(request: Request, since_seq: int = 0, run_id: str = "",
                 limit: int = 500, role: str = Depends(require_token)):
    """Poll for events. The same stream the WebSocket carries.

    Request/response clients — the MCP layer among them — should not have to
    hold a socket open to follow a run.
    """
    hub = request.app.state.api.hub
    events, gap = hub.history(run_id or None, since_seq, limit)
    return {
        'events': [event.model_dump() for event in events],
        'gap': gap,
        'last_seq': events[-1].seq if events else since_seq,
    }


@router.post("/start", status_code=status.HTTP_202_ACCEPTED)
def start(body: RunRequest, request: Request,
           session: MeasurementSession = Depends(get_session),
           role: str = Depends(require_token)):
    """Start a run. The body is the exported ``RunRequest`` contract itself.

    Not a look-alike of it: the desktop's types are generated from that
    model, and it forbids unknown fields, so a misspelt one is a 422 naming
    it rather than a run that quietly ignored what the client asked for.
    """
    profile = request.app.state.api.profile_provider(body.username)
    try:
        run_id = session.start(profile, body.mode, body.sample_name, body.username,
                                overrides=body.overrides,
                                prompt_timeout_s=body.prompt_timeout_s,
                                spot=body.spot, client=body.client)
    except SessionBusy as exc:
        raise busy_as_conflict(exc)
    except InstrumentBusy as exc:
        # Another process holds the bus. Refused now, not failed later.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
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
    """Answer the pending prompt.

    409 when there is nothing to answer, or the answer is for another prompt
    or another run; 403 when the prompt needs a person and the caller is not
    one; 422 when the choice is not one the prompt offered, with the options
    in the detail. A refused answer leaves the prompt pending.
    """
    current = session.status()
    pending = current['pending_prompt']
    if pending is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                             detail="no prompt is pending")
    if body.run_id is not None and body.run_id != current['run_id']:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                             detail=f"the pending prompt belongs to {current['run_id']}, "
                                    f"not {body.run_id}")
    if pending['requires_human'] and role != UI_ROLE:
        # D4: a client that is not a person at the bench may not assert that
        # leads were rewired or that a hazardous voltage was acknowledged.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                             detail="this prompt requires a human answer")
    if body.prompt_id == pending['prompt_id'] and body.choice not in pending['options']:
        # Checked here as well as in the control, which refuses it too: the
        # session reports that refusal as a plain False, and "not one of the
        # options" is a different thing to tell a client than "too late".
        raise _not_an_option(body.choice, pending['options'])
    try:
        accepted = session.answer_prompt(body.prompt_id, body.choice, body.fields)
    except SessionBusy as exc:
        raise busy_as_conflict(exc)
    except ValueError:
        raise _not_an_option(body.choice, pending['options'])
    if not accepted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                             detail="prompt_id is stale or already answered")
    return session.status()


def _not_an_option(choice: str, options) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"{choice!r} is not an answer to this prompt; "
               f"the options are: {', '.join(options)}")

"""The WebSocket a client watches a run through.

Nothing but transport: authenticate, replay from the client's cursor,
subscribe to the hub, forward events as JSON. Every decision about *what* to
send lives in ``event_hub``.

The handler runs three tasks: one forwards events, one waits on the socket so
a disconnect is noticed immediately, and one waits for the hub to give up on a
client that fell too far behind. Without the second, the handler would sit in
``await stream.get()`` after the client vanished and the connection would only
be reaped when the server shut down. Without the third, a client the hub has
stopped feeding would keep an open, silent socket and go on believing the run
it last heard about.
"""
import asyncio
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from .event_hub import OVERFLOW_CLOSE_CODE

logger = logging.getLogger(__name__)

router = APIRouter()

#: How long closing an overflowed client's socket may take.
CLOSE_TIMEOUT_S = 5.0


async def _forward_events(websocket: WebSocket, stream) -> None:
    while True:
        event = await stream.get()
        await websocket.send_text(event.model_dump_json())


async def _watch_for_disconnect(websocket: WebSocket) -> None:
    """Consume whatever the client sends; returns when it goes away."""
    while True:
        await websocket.receive()


@router.websocket("/session/events/ws")
async def stream_events(websocket: WebSocket, token: str = Query(default=""),
                         run_id: str = Query(default=""),
                         since_seq: int = Query(default=0)):
    state = websocket.app.state.api
    if token != state.token:
        await websocket.close(code=4401)  # application-level "unauthorized"
        return

    await websocket.accept()
    hub = state.hub
    stream = hub.add_client()
    try:
        if since_seq:
            # Resume where the client left off. Subscribing before replaying
            # means a live event during the replay is queued rather than lost;
            # the client dedupes on seq, which is what seq is for.
            missed, gap = hub.history(run_id or None, since_seq)
            if gap:
                await websocket.send_json({
                    'type': 'gap',
                    'detail': 'history no longer covers since_seq; refetch the file',
                    'since_seq': since_seq,
                })
            for event in missed:
                await websocket.send_text(event.model_dump_json())

        overflow = asyncio.create_task(stream.overflow.wait())
        tasks = [asyncio.create_task(_forward_events(websocket, stream)),
                  asyncio.create_task(_watch_for_disconnect(websocket)),
                  overflow]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
        finally:
            for task in tasks:
                task.cancel()
        if overflow in done:
            # The stream has a hole the client cannot see. Hang up, so it
            # reconnects and resumes from its cursor. Bounded: the reason it
            # overflowed may be that it is not reading at all.
            await asyncio.wait_for(websocket.close(code=OVERFLOW_CLOSE_CODE),
                                   timeout=CLOSE_TIMEOUT_S)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("event stream closed", exc_info=True)
    finally:
        hub.remove_client(stream)

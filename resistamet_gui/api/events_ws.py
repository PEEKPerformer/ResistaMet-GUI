"""The WebSocket a client watches a run through.

Nothing but transport: authenticate, replay from the client's cursor,
subscribe to the hub, forward events as JSON. Every decision about *what* to
send lives in ``event_hub``.

The handler runs two tasks: one forwards events, one waits on the socket so a
disconnect is noticed immediately. Without the second, the handler would sit
in ``await stream.get()`` after the client vanished and the connection would
only be reaped when the server shut down.
"""
import asyncio
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter()


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

        tasks = [asyncio.create_task(_forward_events(websocket, stream)),
                  asyncio.create_task(_watch_for_disconnect(websocket))]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
        finally:
            for task in tasks:
                task.cancel()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("event stream closed", exc_info=True)
    finally:
        hub.remove_client(stream)

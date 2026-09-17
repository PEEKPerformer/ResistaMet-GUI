"""The WebSocket a client watches a run through.

Nothing but transport: authenticate, subscribe to the hub, forward events as
JSON. Every decision about what to send lives in ``event_hub``.
"""
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/session/events/ws")
async def stream_events(websocket: WebSocket, token: str = Query(default="")):
    state = websocket.app.state.api
    if token != state.token:
        await websocket.close(code=4401)  # application-level "unauthorized"
        return

    await websocket.accept()
    hub = state.hub
    stream = hub.add_client()
    try:
        while True:
            event = await stream.get()
            await websocket.send_text(event.model_dump_json())
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("event stream closed", exc_info=True)
    finally:
        hub.remove_client(stream)

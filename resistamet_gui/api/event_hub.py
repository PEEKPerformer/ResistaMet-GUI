"""Hand run events from the acquisition thread to WebSocket clients.

Two rules shape this:

* **The acquisition thread never waits on a client.** publish() is called from
  the run thread and only ever hands off; a slow or stalled client can never
  slow down sampling or delay a stop.
* **When a client cannot keep up, drop the cheap events, not the load-bearing
  ones.** Samples and progress logs are droppable — the file is the record, and
  a client that missed one can ask for the file. run_ended, errors, prompts and
  the lifecycle events are not: losing those leaves a client believing a run is
  still going when the instrument has been off for ten minutes.

A client whose queue cannot even take a non-droppable event is disconnected
rather than fed a stream with holes in it; it reconnects and resumes. The hub
stops feeding it and sets ``ClientStream.overflow``; the WebSocket handler
waits on that and closes the socket with :data:`OVERFLOW_CLOSE_CODE`.
"""
import asyncio
import logging
import threading
from collections import deque
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

#: Events safe to drop under pressure: high-rate, and recoverable from the file.
DROPPABLE_TYPES = frozenset({'sample', 'log'})

#: Per-client queue size, and the point above which droppable events are shed
#: so the remaining room is reserved for events that must not be lost.
QUEUE_CAPACITY = 2000
DROPPABLE_HIGH_WATER = 1900

#: WebSocket close code for "you fell too far behind; reconnect and resume".
#: In the application range (4000-4999), after HTTP 408.
OVERFLOW_CLOSE_CODE = 4408

#: Progress logs are cosmetic; a few per second is plenty for any UI.
PROGRESS_INTERVAL_S = 0.5

#: How much history a reconnecting client can ask for. A dropped connection
#: over a lunch-length run should be resumable; beyond that the file is the
#: record, and the client is told there is a gap rather than shown a partial
#: stream it cannot tell apart from a complete one.
HISTORY_SIZE = 10000


class ClientStream:
    """One subscriber's queue, with the drop policy applied on the way in."""

    def __init__(self, capacity: int = QUEUE_CAPACITY,
                  high_water: int = DROPPABLE_HIGH_WATER):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=capacity)
        self._high_water = high_water
        self.dropped = 0
        self.overflowed = False
        #: Set, on the loop, when the hub gives up on this client.
        self.overflow = asyncio.Event()

    def offer(self, event) -> bool:
        """Queue an event. False means this client has to go."""
        droppable = event.type in DROPPABLE_TYPES
        if droppable and self._queue.qsize() >= self._high_water:
            self.dropped += 1
            return True
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            if droppable:
                self.dropped += 1
                return True
            # No room even in the reserve: a stream with holes in it is worse
            # than an honest disconnect.
            self.overflowed = True
            self.overflow.set()
            return False
        return True

    async def get(self):
        return await self._queue.get()


class EventHub:
    """Fan-out from the run thread to any number of WebSocket clients."""

    def __init__(self, clock=None, capacity: int = QUEUE_CAPACITY,
                 high_water: int = DROPPABLE_HIGH_WATER):
        import time as _time

        self._clock = clock or _time.monotonic
        self._capacity = capacity
        self._high_water = high_water
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._clients: Set[ClientStream] = set()
        self._last_compliance: Dict[str, Optional[str]] = {}
        #: None until the first progress log, so the first one is never eaten.
        self._last_progress: Optional[float] = None
        self._history: deque = deque(maxlen=HISTORY_SIZE)
        self._history_lock = threading.Lock()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach to the serving loop. Called once at app startup."""
        self._loop = loop

    def add_client(self) -> ClientStream:
        stream = ClientStream(self._capacity, self._high_water)
        self._clients.add(stream)
        return stream

    def remove_client(self, stream: ClientStream) -> None:
        self._clients.discard(stream)

    # --- producer side (run thread) --------------------------------------

    def publish(self, event) -> None:
        """Called from the acquisition thread. Hands off and returns."""
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._deliver, event)
        except RuntimeError:
            # Loop already closed (shutdown races a final event).
            logger.debug("event hub loop closed; dropping %s", event.type)

    # --- consumer side (event loop) --------------------------------------

    def _deliver(self, event) -> None:
        if not self._forward(event):
            return
        with self._history_lock:
            self._history.append(event)
        for stream in list(self._clients):
            if not stream.offer(event):
                self.remove_client(stream)

    def history(self, run_id: Optional[str] = None, since_seq: int = 0,
                 limit: Optional[int] = None) -> Tuple[List[Any], bool]:
        """Events after ``since_seq``, and whether anything was lost first.

        The second value is True when the client asked to resume from a point
        the ring no longer holds: it has a gap, and being told so is the
        difference between an incomplete record and a wrong one.
        """
        with self._history_lock:
            kept = [e for e in self._history if run_id is None or e.run_id == run_id]
        events = [e for e in kept if e.seq > since_seq]
        oldest = kept[0].seq if kept else None
        gap = bool(since_seq and oldest is not None and oldest > since_seq + 1)
        if limit is not None:
            events = events[:limit]
        return events, gap

    def _forward(self, event) -> bool:
        """Apply the per-stream filters. False means "do not send"."""
        if event.type == 'compliance':
            # Compliance fires on every out-of-compliance sample; a client only
            # needs the transitions, and sample.compliance still carries the
            # per-sample truth.
            run = event.run_id or ''
            kind = event.payload.get('kind')
            if self._last_compliance.get(run) == kind:
                return False
            self._last_compliance[run] = kind
            return True
        if event.type == 'sample':
            if event.payload.get('compliance') == 'OK':
                self._last_compliance[event.run_id or ''] = None
            return True
        if event.type == 'log' and event.payload.get('code') == 'progress':
            now = self._clock()
            if self._last_progress is not None and now - self._last_progress < PROGRESS_INTERVAL_S:
                return False
            self._last_progress = now
        if event.type == 'run_started':
            self._last_compliance[event.run_id or ''] = None
        return True

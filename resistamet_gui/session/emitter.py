"""Numbering and delivery for run events.

The emitter owns nothing but the sequence counter: it stamps an event and hands
it to one sink. The sink is whatever the caller is — a Qt adapter forwarding to
Signals, a list in a test, the WebSocket hub later.

Two rules, both about the acquisition thread:

* **The sink is called with no lock held.** Only the counter is guarded. A sink
  that blocks (a slow client, a Qt queued connection) must never be able to
  stall the run thread behind a lock, and a sink that emits again must not
  deadlock.
* **A failing sink cannot kill the run.** Delivery errors are logged and
  swallowed, the way the existing defensive blocks around emits in
  ``workers.py`` do.
"""
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .events import Event, PAYLOAD_MODELS

logger = logging.getLogger(__name__)


class EventEmitter:
    """Stamps run events with a sequence number and delivers them."""

    def __init__(self, sink: Callable[[Event], None], run_id: Optional[str] = None,
                 clock: Callable[[], float] = time.time):
        self._sink = sink
        self._run_id = run_id
        self._clock = clock
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> Event:
        """Stamp and deliver one event; returns what was sent."""
        payload = dict(payload or {})
        model = PAYLOAD_MODELS.get(event_type)
        if model is not None:
            # Validate here so a wrong payload fails at its emit site, not in
            # whichever client eventually reads it.
            payload = model(**payload).model_dump()

        with self._lock:
            self._seq += 1
            seq = self._seq
        event = Event(type=event_type, run_id=self._run_id, seq=seq,
                      t=self._clock(), payload=payload)

        try:
            self._sink(event)
        except Exception as exc:
            logger.error(f"Event sink failed on {event_type}: {exc}")
        return event

    def log(self, code: str, message: str, level: str = 'info') -> Event:
        return self.emit('log', {'level': level, 'code': code, 'message': message})

    def error(self, code: str, source: str, message: str, fatal: bool = True) -> Event:
        return self.emit('error', {'code': code, 'source': source,
                                    'message': message, 'fatal': fatal})


class ListSink:
    """Collects events in order. For tests and short-lived inspection."""

    def __init__(self):
        self.events: List[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> List[str]:
        return [event.type for event in self.events]

    def of_type(self, event_type: str) -> List[Event]:
        return [event for event in self.events if event.type == event_type]

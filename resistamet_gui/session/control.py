"""The mutable state a run shares with whoever is driving it.

Today each worker keeps this inline: a lock, a running flag, a paused flag, the
event marker queue and (for vdP) the operator's proceed gate. It is the same
state either way, and the headless session needs it without QThread, so it
moves into one small object both the Qt adapters and the session can hold.

Deliberately literal: plain bools under one lock, no state machine, no
callbacks. The lock guards the fields only — never I/O, never an emit — so a
slow instrument read can never block whoever is trying to stop the run.
"""
import threading
from typing import List


class RunControl:
    """Start/stop/pause state plus the operator's proceed gate."""

    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self._paused = False
        self._event_markers: List[str] = []
        #: Set when the operator has answered a prompt, and by stop, so a
        #: waiting run always wakes.
        self.proceed_event = threading.Event()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @running.setter
    def running(self, value: bool) -> None:
        with self._lock:
            self._running = value

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        with self._lock:
            self._paused = value

    @property
    def event_marker(self) -> str:
        """The marks waiting for the next sample, joined in arrival order."""
        with self._lock:
            return "; ".join(self._event_markers)

    def mark_event(self, name: str = "MARK") -> None:
        """Queue a mark for the next sample.

        Marks queue rather than overwrite: two keystrokes between samples are
        two things the operator did, and dropping the first loses a record the
        run cannot reconstruct.
        """
        with self._lock:
            self._event_markers.append(name)

    def get_and_clear_event_marker(self) -> str:
        """Atomically take every pending mark."""
        with self._lock:
            marker = "; ".join(self._event_markers)
            self._event_markers = []
            return marker

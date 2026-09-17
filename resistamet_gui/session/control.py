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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class PendingPrompt:
    """A decision the run is blocked on.

    ``requires_human`` marks the ones a person must actually make — rewiring
    leads, acknowledging a hazardous voltage — so a non-UI client can be
    refused rather than allowed to wave them through.
    """

    prompt_id: str
    kind: str
    options: List[str]
    requires_human: bool = True
    detail: Dict[str, Any] = field(default_factory=dict)


class RunStopped(Exception):
    """Raised inside a run when a stop lands during a wait.

    Settling delays are the longest thing a run does between stop checks, so
    interrupting them is what makes a stop feel immediate. Unwinding by
    exception also means the read and the row that would have followed an
    unsettled wait are skipped rather than recorded.
    """


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
        #: Set by finish(); what the interruptible sleep waits on.
        self.stop_event = threading.Event()
        self._finish_reason: Optional[str] = None
        self._prompt: Optional[PendingPrompt] = None
        self._answer: Optional[str] = None
        self._answer_fields: Dict[str, Any] = {}
        self._prompt_count = 0

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
    def finish_reason(self) -> Optional[str]:
        """Why the run ended, or None while it is still going."""
        with self._lock:
            return self._finish_reason

    def finish(self, reason: str) -> None:
        """End the run, recording why. First writer wins.

        Several things can end a run within milliseconds of each other — a
        compliance stop the operator also clicked stop on, say. Keeping the
        first reason means the report says what actually happened rather than
        whichever code path ran last.
        """
        with self._lock:
            if self._finish_reason is None:
                self._finish_reason = reason
            self._running = False
        self.stop_event.set()
        self.proceed_event.set()

    def stopped(self) -> bool:
        return self.stop_event.is_set()

    def sleep(self, seconds: float) -> None:
        """Wait, unless the run is stopped first.

        Raises :class:`RunStopped` instead of returning when a stop lands, so
        the caller unwinds rather than carrying on with a wait it did not
        finish.
        """
        if self.stop_event.wait(max(0.0, seconds)):
            raise RunStopped()

    # --- operator prompts -------------------------------------------------

    @property
    def pending_prompt(self) -> Optional[PendingPrompt]:
        with self._lock:
            return self._prompt

    def raise_prompt(self, kind: str, options: List[str],
                      requires_human: bool = True, detail: Optional[Dict[str, Any]] = None
                      ) -> PendingPrompt:
        """Block the run on a decision. Returns the prompt to report."""
        with self._lock:
            self._prompt_count += 1
            prompt = PendingPrompt(
                prompt_id=f"{kind}-{self._prompt_count}",
                kind=kind, options=list(options),
                requires_human=requires_human, detail=dict(detail or {}),
            )
            self._prompt = prompt
            self._answer = None
        self.proceed_event.clear()
        return prompt

    def answer_prompt(self, prompt_id: str, choice: str,
                       fields: Optional[Dict[str, Any]] = None) -> bool:
        """Answer the pending prompt. First valid answer wins; stale ids lose.

        ``fields`` carries anything the answer needs beyond the choice — the
        safety dialog's "don't show again", for instance.
        """
        with self._lock:
            prompt = self._prompt
            if prompt is None or prompt.prompt_id != prompt_id or self._answer is not None:
                return False
            self._answer = choice
            self._answer_fields = dict(fields or {})
        self.proceed_event.set()
        return True

    def wait_for_prompt(self) -> Tuple[Optional[str], Dict[str, Any]]:
        """Block until the prompt is answered or the run is stopped.

        Returns ``(choice, fields)``; the choice is None when stop woke the
        wait instead — the caller decides what abandoning the run means for it.
        """
        self.proceed_event.wait()
        with self._lock:
            answer = self._answer
            fields = self._answer_fields
            self._prompt = None
            self._answer = None
            self._answer_fields = {}
        return answer, fields

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

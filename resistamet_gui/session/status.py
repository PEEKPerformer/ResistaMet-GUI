"""What ``MeasurementSession.status()`` reports, as a contract.

``GET /session`` and every session command answer with this. It was a plain
dict whose shape the desktop client copied by hand; as a model it is exported
with the other contracts (``tools/export_contracts.py``) and the client's type
is generated from it.

Every field is required, nullable where there may be nothing to report, so
the reply always has the same keys. No Qt.
"""
from typing import Any, Dict, List, Literal, Optional

from .events import EventModel

#: ``idle``; ``identifying`` while ``identify`` holds the bus; the rest belong
#: to a run. ``awaiting_prompt`` and ``paused`` are read off the run's control.
SessionState = Literal['idle', 'identifying', 'running', 'paused',
                       'awaiting_prompt', 'stopping']


class PendingPrompt(EventModel):
    """The question a run is parked on. Same fields as the ``prompt`` event."""

    prompt_id: str
    kind: Literal['vdp_geometry', 'safety_voltage_ack', 'cable_null_shorted']
    options: List[str]
    requires_human: bool
    detail: Dict[str, Any]


class SessionStatus(EventModel):
    """One session: its state and the run it is on, or was last on."""

    state: SessionState
    #: The current run, or the last one; None before the first.
    run_id: Optional[str]
    mode: Optional[Literal['resistance', 'source_v', 'source_i', 'four_point',
                           'sweep', 'vdp']]
    #: The run's data file once it has one.
    path: Optional[str]
    #: ``seq`` of the newest event, for a client resuming the stream.
    last_seq: int
    pending_prompt: Optional[PendingPrompt]

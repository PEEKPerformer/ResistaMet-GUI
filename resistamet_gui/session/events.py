"""The event envelope a run reports through, and its first payloads.

A run currently talks to exactly one listener, through Qt signals
(``workers.py``). The headless session needs the same information as data, so
one description serves the Qt UI, a WebSocket client and the MCP layer. See
``docs/design/tauri_backend_split.md`` section 3.4.

The envelope is identical in-process and on the wire, so an in-process sink and
a socket client see the same ordering and the same fields:

* ``seq`` is a plain per-run counter — a client that reconnects asks for
  everything after the last ``seq`` it saw.
* ``t`` is wall-clock emit time.
* ``payload`` is one model per event type, added in the PR that first emits it,
  so no payload exists that nothing produces.

Non-finite floats (NaN sigma, an unmeasured temperature) serialize as ``null``;
in-process sinks receive the real float. JSON has no NaN, and a client that
must special-case a non-standard token is a client that will get it wrong.
"""
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

#: Bumped when the envelope or an existing payload changes shape.
EVENT_SCHEMA_VERSION = 1


class EventModel(BaseModel):
    """Base: strict about keys, null for non-finite floats on the wire."""

    model_config = ConfigDict(extra='forbid', ser_json_inf_nan='null')


class LogPayload(EventModel):
    """Human-readable progress, one per current ``status_update`` site.

    ``code`` is a short closed set so a client can act on an event without
    parsing prose; ``message`` stays the exact text the GUI shows today.
    """

    level: Literal['info', 'warning', 'error'] = 'info'
    code: str
    message: str


class ErrorPayload(EventModel):
    """Something failed. ``source`` says which subsystem.

    ``source`` replaces the substring matching the UI does today to decide
    whether an error came from the instrument, the aux sensor or the file.
    ``fatal`` marks the errors that end the run.
    """

    code: str
    source: Literal['smu', 'aux', 'file', 'run']
    message: str
    fatal: bool = True


class Event(EventModel):
    """One thing that happened during a run."""

    v: int = EVENT_SCHEMA_VERSION
    type: str
    run_id: Optional[str] = None
    seq: int = Field(ge=0)
    t: float
    payload: Dict[str, Any] = Field(default_factory=dict)


#: Event type -> payload model, for validation and contract export.
PAYLOAD_MODELS = {
    'log': LogPayload,
    'error': ErrorPayload,
}

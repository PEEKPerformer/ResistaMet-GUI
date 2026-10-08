"""What ``MeasurementSession.status()`` reports, as a contract.

``GET /session`` and every session command answer with this. It was a plain
dict whose shape the desktop client copied by hand; as a model it is exported
with the other contracts (``tools/export_contracts.py``) and the client's type
is generated from it.

Every field is required, nullable where there may be nothing to report, so
the reply always has the same keys. No Qt.

``SafetyAckFields`` is the other half of a pending prompt: what an answer to
the touch-safety question may carry beside its choice.
"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field, StrictBool, model_validator

from ..safety import MAX_SILENCE_DAYS, SECONDS_PER_DAY
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


class SafetyAckFields(EventModel):
    """The ``fields`` of an answer to ``safety_voltage_ack``.

    ``silence_for_profile`` silences the warning on the run's profile for
    good; ``silence_for_days`` for that many days from the answer. Either
    is saved only with ``acknowledge``: a person who cancelled has not
    agreed to stop being asked. One or the other, not both. Unknown keys
    are refused, so a misspelt one is an error rather than a silence that
    quietly never happened.

    The silence applies to the runs a person starts; a run an agent
    started asks regardless (``docs/design/mcp_layer.md`` M5).
    """

    silence_for_profile: StrictBool = False
    silence_for_days: Optional[float] = Field(
        default=None, gt=0.0, le=MAX_SILENCE_DAYS, allow_inf_nan=False, strict=True)

    @model_validator(mode='after')
    def _one_silence_at_most(self) -> 'SafetyAckFields':
        if self.silence_for_profile and self.silence_for_days is not None:
            raise ValueError("send silence_for_profile or silence_for_days, not both")
        return self

    def profile_change(self, now: float) -> Dict[str, Any]:
        """The ``measurement`` keys to store for this answer; empty for none."""
        if self.silence_for_profile:
            return {'safety_voltage_warn_silenced': True}
        if self.silence_for_days is not None:
            return {'safety_voltage_warn_silenced_until':
                    now + self.silence_for_days * SECONDS_PER_DAY}
        return {}


class InstrumentInfo(EventModel):
    """The SourceMeter as last seen: what ``identify`` returns, and the data
    of a run's ``instrument_connected`` event. ``model`` is None when the
    *IDN? reply names a model the limits table does not know."""

    address: str
    idn: str
    model: Optional[str]
    max_source_v: Optional[float]
    max_source_i: Optional[float]
    max_power_w: Optional[float]


class SessionStatus(EventModel):
    """One session: its state and the run it is on, or was last on."""

    state: SessionState
    #: The current run, or the last one; None before the first.
    run_id: Optional[str]
    mode: Optional[Literal['resistance', 'source_v', 'source_i', 'four_point',
                           'sweep', 'vdp']]
    #: Who started that run: the API role of the token that asked for it
    #: (``ui``, ``agent``); None for a run started without one.
    started_by: Optional[str]
    #: The run's data file once it has one.
    path: Optional[str]
    #: ``seq`` of the newest event, for a client resuming the stream.
    last_seq: int
    pending_prompt: Optional[PendingPrompt]
    #: The instrument the last run connected to or the last ``identify``
    #: found, whichever is newer; None until one has happened. A client that
    #: reloads has lost the event that said so, and reads it back here.
    instrument: Optional[InstrumentInfo]

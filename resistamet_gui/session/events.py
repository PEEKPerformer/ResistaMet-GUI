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
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..schema.spots import SpotRequest

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


class InstrumentConnectedPayload(EventModel):
    """The SMU answered *IDN? and its limits are known."""

    address: str
    idn: str
    model: str
    max_source_v: Optional[float] = None
    max_source_i: Optional[float] = None
    max_power_w: Optional[float] = None


class LineFrequencyPayload(EventModel):
    """Mains frequency, queried or assumed. Continuous modes only."""

    hz: float
    assumed: bool = False


class DerivedPayload(EventModel):
    """The 4PP quantities computed for this sample, as written to the row.

    ``method`` says which correction path produced them: the ASTM F84
    decomposition or the legacy K*alpha form.
    """

    ratio: float
    rs: float
    rho: float
    sigma: float
    v_unc: float
    i_unc: float
    method: Literal['f84', 'legacy']


class DeltaPayload(EventModel):
    """Per-polarity values from a current-reversal (delta) reading."""

    v_plus: float
    v_minus: float
    r_f: float
    r_r: float


class SamplePayload(EventModel):
    """One acquired point.

    ``values`` is the mode's data dict exactly as the parse produced it, with
    no coercion — the aux-fault column is a string, and a client that wants
    numbers must say which key it means.
    """

    t_unix: float
    elapsed_s: float
    compliance: Literal['OK', 'V_COMP', 'I_COMP'] = 'OK'
    event_marker: str = ''
    values: Dict[str, Any] = Field(default_factory=dict)
    #: 4PP only; absent for modes that derive nothing.
    derived: Optional[DerivedPayload] = None
    #: 4PP delta mode only.
    delta: Optional[DeltaPayload] = None


class CompliancePayload(EventModel):
    """The source is in compliance on this sample."""

    kind: Literal['Voltage', 'Current']
    stop_on_compliance: bool = False


class OverpowerPayload(EventModel):
    """Measured V*I crossed the 4PP probe-safety hard stop."""

    measured_w: float
    stop_w: float


class SweepSegmentPayload(EventModel):
    """One completed sweep direction, returned by the instrument in bulk."""

    direction: Literal['forward', 'reverse'] = 'forward'
    voltages: List[float] = Field(default_factory=list)
    currents: List[float] = Field(default_factory=list)
    compliance: List[str] = Field(default_factory=list)


class GeometryWarningPayload(EventModel):
    """A four-point spot's position is a problem, said before the first sample.

    ``refused`` with ``off_sample``: a probe tip is on or beyond the edge and
    the run ends without touching the instrument. ``near_edge``: the position
    costs more than ``edge_warn_pct`` here; the run goes on and the values are
    recorded as measured, without a position correction.

    Two errors, both fractions and not percentages. ``relative_error_rows`` is
    ``factor_rows / factor_here - 1``, against the lateral factor the run's
    rows really apply (the table look-up, or K*alpha); it is the error in the
    file's Rs. ``relative_error`` is ``factor_centre / factor_here - 1``,
    against the closed-form centre of the sample outline. ``compared_with``
    says which one was held against the threshold: ``rows`` whenever the rows
    have a factor, else ``centre``. The factors are absent off the sample,
    where they diverge.
    """

    refused: bool
    reason: Literal['off_sample', 'near_edge']
    message: str
    spot: SpotRequest
    edge_clearance_s: float
    edge_warn_pct: float
    factor_here: Optional[float] = None
    factor_centre: Optional[float] = None
    relative_error: Optional[float] = None
    factor_rows: Optional[float] = None
    relative_error_rows: Optional[float] = None
    compared_with: Literal['rows', 'centre'] = 'centre'


class QuantityStats(EventModel):
    """One derived quantity over a spot's samples (``session.spot_stats``).

    ``n`` counts the finite values. Every other field is null on the wire when
    it does not exist: all of them with no finite value, ``sd`` and
    ``rsd_pct`` with fewer than two.
    """

    n: int
    mean: Optional[float] = None
    sd: Optional[float] = None
    rsd_pct: Optional[float] = None
    u_stat: Optional[float] = None
    u_inst: Optional[float] = None
    u_total: Optional[float] = None


class SpotStats(EventModel):
    """The statistics block of a four-point run, as its file footer has it.

    ``n`` samples entered the statistics; ``n_excluded`` were in compliance
    and left out, because they record a bound rather than a measurement.
    """

    n: int
    n_excluded: int = 0
    rs: QuantityStats
    rho: QuantityStats
    sigma: QuantityStats


class SpotCompletePayload(EventModel):
    """A four-point run's file is closed; these are the numbers in its footer.

    Emitted for every four-point run whose file was finalized, so a client
    shows the backend's statistics instead of computing its own. ``spot`` is
    null for a run that was not given one. How the run ended is in the
    ``run_ended`` event that follows.
    """

    spot: Optional[SpotRequest] = None
    path: Optional[str] = None
    stats: SpotStats


class AcquisitionFinishedPayload(EventModel):
    """The acquisition loop ended; cleanup and finalize still follow."""

    mode: str


class VdpGeometryCompletePayload(EventModel):
    """One F76 geometry measured: the +I and -I voltages at this wiring."""

    index: int
    name: str
    group: str
    label_pos: str
    v_pos: float
    label_neg: str
    v_neg: float
    current_a: float


class VdpResultPayload(EventModel):
    """The finished van der Pauw result, ASTM F76.

    Field for field what the GUI result panel has always received, including
    the f(Q) homogeneity check and the combined uncertainties, so the panel
    reads the same numbers the CSV metadata carries.
    """

    rho_a: float
    rho_b: float
    rho_avg: float
    sheet_resistance: float
    q_a: float
    q_b: float
    f_a: float
    f_b: float
    homogeneous: bool
    asymmetry_pct: float
    voltages: Dict[str, float] = Field(default_factory=dict)
    current_a: float
    thickness_cm: float
    sheet_resistance_uncertainty: Optional[float] = None
    rho_avg_uncertainty: Optional[float] = None


class PromptPayload(EventModel):
    """The run is blocked until someone answers."""

    prompt_id: str
    kind: Literal['vdp_geometry', 'safety_voltage_ack', 'cable_null_shorted']
    options: List[str] = Field(default_factory=list)
    requires_human: bool = True
    detail: Dict[str, Any] = Field(default_factory=dict)


class PromptResolvedPayload(EventModel):
    """How a prompt ended: answered, or released by a stop."""

    prompt_id: str
    choice: Optional[str] = None
    answered_by: Optional[str] = None


class RunStartedPayload(EventModel):
    """A run is beginning; the settings are exactly what it will use."""

    mode: str
    sample_name: str
    username: str
    settings: Dict[str, Any] = Field(default_factory=dict)
    started_at: float


class AuxConnectedPayload(EventModel):
    """The auxiliary sensor is open and has declared its channels."""

    driver: str
    address: str
    channels: List[Dict[str, Any]] = Field(default_factory=list)


class FileOpenedPayload(EventModel):
    """The run's output file exists and its column schema is fixed."""

    path: str
    columns: List[str] = Field(default_factory=list)
    units: List[str] = Field(default_factory=list)


class FileFinalizedPayload(EventModel):
    """The run's file is closed, with its end metadata written."""

    path: str
    end_metadata: Dict[str, Any] = Field(default_factory=dict)


class RunStatePayload(EventModel):
    """Paused, resumed or stopping, as observed by the acquisition thread."""

    reason: Optional[str] = None


class RunEndedPayload(EventModel):
    """Always the last event of a run, whatever ended it."""

    reason: str
    ok: bool = True
    samples: int = 0
    duration_s: float = 0.0
    path: Optional[str] = None


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
    'instrument_connected': InstrumentConnectedPayload,
    'line_frequency': LineFrequencyPayload,
    'sample': SamplePayload,
    'compliance': CompliancePayload,
    'overpower_trip': OverpowerPayload,
    'sweep_segment': SweepSegmentPayload,
    'geometry_warning': GeometryWarningPayload,
    'spot_complete': SpotCompletePayload,
    'acquisition_finished': AcquisitionFinishedPayload,
    'vdp_geometry_complete': VdpGeometryCompletePayload,
    'vdp_result': VdpResultPayload,
    'prompt': PromptPayload,
    'prompt_resolved': PromptResolvedPayload,
    'run_started': RunStartedPayload,
    'aux_connected': AuxConnectedPayload,
    'file_opened': FileOpenedPayload,
    'file_finalized': FileFinalizedPayload,
    'paused': RunStatePayload,
    'resumed': RunStatePayload,
    'stopping': RunStatePayload,
    'run_ended': RunEndedPayload,
}

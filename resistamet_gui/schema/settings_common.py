"""Typed models for the settings groups every mode shares.

Why this exists: settings are assembled today by reading live Qt widgets
(``ui/main_window.py`` ``gather_settings_for_mode``), so the only description of
a valid run is the widget set itself. A headless session — and the Tauri UI and
MCP layer after it — needs that description as data. See
``docs/design/tauri_backend_split.md`` section 4.

Two rules keep this from becoming a second source of truth:

* **Defaults are read from :data:`~resistamet_gui.constants.DEFAULT_SETTINGS`
  by reference**, never re-typed here. A default changes in one place.
* **Field names are the dict keys the workers already read.** The schema is a
  boundary contract; the run procedures keep reading the nested dict, so every
  field greps one-to-one against the code that consumes it.

Validation is deliberately lenient about *extra* keys (``extra='allow'``),
matching ``config.py``'s merge behaviour: an unknown key in a stored profile is
preserved, not an error. Range violations in stored profiles are reported as
issues by the resolver rather than raised — a lab profile that has drifted out
of range must still open.

No Qt, no pyvisa: this module is importable from anywhere.
"""
import re
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from ..constants import DEFAULT_SETTINGS

_M = DEFAULT_SETTINGS['measurement']
_F = DEFAULT_SETTINGS['file']
_O = DEFAULT_SETTINGS['output']
_D = DEFAULT_SETTINGS['display']
_A = DEFAULT_SETTINGS['agent_limits']

#: pyvisa's two Prologix interface resource classes (``pyvisa/rname.py``):
#: ``PRLGX-ASRL[board]::serial device::INTFC`` and
#: ``PRLGX-TCPIP[board]::host address[::port]::INTFC``. The middle part is
#: left to pyvisa; this only catches a name that cannot be one at all.
#: pyvisa reads the prefix in any case but wants ``INTFC`` in capitals.
_PRLGX_INTFC = re.compile(r'^(?i:PRLGX-(?:ASRL|TCPIP))\d*::.+::INTFC$')


class SettingsModel(BaseModel):
    """Base for every settings group: unknown keys pass through."""

    model_config = ConfigDict(extra='allow', validate_assignment=True)


class InstrumentSettings(SettingsModel):
    """Knobs that apply to every mode, wherever the value comes from.

    ``gpib_address``, ``visa_library``, ``gpib_interface`` and
    ``allow_agents`` are machine-local — ``ConfigManager`` keeps them in this
    machine's own file and injects them into the profile on read, so they are
    never stored per user (``config.py``).
    """

    gpib_address: str = Field(default=_M['gpib_address'], min_length=1)
    #: '' = pyvisa's default, '@ivi' = vendor VISA, '@py' = pyvisa-py, or a
    #: path to a VISA library (``visa_backend.py``).
    visa_library: str = _M['visa_library']
    gpib_interface: str = Field(
        default=_M['gpib_interface'],
        description=(
            "Interface resource of a Prologix-style GPIB adapter (Prologix "
            "GPIB-USB / GPIB-ETHERNET, AR488), opened before the instrument "
            "so that GPIB<board>::<addr>::INSTR resolves; pyvisa-py only. "
            "Empty = none. Serial: PRLGX-ASRL[board]::<device>::INTFC, where "
            "<device> is the port path on macOS and Linux "
            "(PRLGX-ASRL::/dev/cu.usbserial-PX12345::INTFC, "
            "PRLGX-ASRL::/dev/ttyUSB0::INTFC) and the COM port number alone "
            "on Windows (PRLGX-ASRL::5::INTFC for COM5). Ethernet: "
            "PRLGX-TCPIP[board]::<host>[::port]::INTFC, port 1234 by default. "
            "[board] defaults to 0 and is the <board> of the instrument "
            "address."
        ),
    )
    #: Strict: a switch that lets an AI agent drive the instrument is true or
    #: false, never a string or a number that happens to read as one.
    allow_agents: StrictBool = Field(
        default=_M['allow_agents'],
        description=(
            "Let AI agents connect to a backend on this machine, through an "
            "MCP server, with a token of their own (role 'agent'). "
            "Machine-local; only the 'ui' role may change it."
        ),
    )
    nplc: float = Field(default=_M['nplc'], ge=0.01, le=10.0,
                        description="Integration time per reading, in power-line cycles.")
    sampling_rate: float = Field(
        default=_M['sampling_rate'], ge=0.1, le=100.0,
        description="Readings per second asked for; the timing settings may allow fewer.")
    settling_time: float = Field(default=_M['settling_time'], ge=0.0, le=10.0)
    auto_zero: Literal['on', 'once', 'off'] = Field(
        default=_M['auto_zero'],
        description="on: re-zero with every reading (slower, no drift); once: zero at the "
                    "start of the run; off: never.")
    filter_enabled: bool = _M['filter_enabled']
    filter_type: Literal['repeat', 'moving'] = Field(
        default=_M['filter_type'],
        description="repeat: each reading averages filter_count new conversions; moving: "
                    "a running average.")
    filter_count: int = Field(default=_M['filter_count'], ge=1, le=100)
    stop_on_compliance: bool = Field(default=_M['stop_on_compliance'],
                                     description="End the run when the output reaches "
                                                 "its compliance limit.")

    @field_validator('gpib_interface')
    @classmethod
    def _interface_is_a_prologix_intfc(cls, value: str) -> str:
        value = value.strip()
        if value and not _PRLGX_INTFC.match(value):
            raise ValueError(
                "expected PRLGX-ASRL[board]::<serial device>::INTFC or "
                "PRLGX-TCPIP[board]::<host>[::port]::INTFC")
        return value


class AuxSensorSettings(SettingsModel):
    """Auxiliary-sensor co-logging. Serialized inside ``measurement``.

    Co-logging is only wired into the continuous modes
    (``data_export.AUX_LOG_MODES``); the resolver reports an issue when a
    request asks for it on a sweep or vdP run.
    """

    aux_log_enabled: bool = Field(
        default=_M['aux_log_enabled'],
        description="Log an auxiliary sensor's channels beside each reading, as aux_<key> "
                    "columns and aux_fault; resistance, source_v, source_i and four_point "
                    "only.")
    aux_driver: str = Field(
        default=_M['aux_driver'], min_length=1,
        description="Sensor driver: arduino_thermocouple (a K-type thermocouple board "
                    "streaming DATA lines) or stream_sensor (a device that names its "
                    "channels in an HDR line). Read when aux_log_enabled.")
    aux_address: str = Field(
        default=_M['aux_address'], min_length=1,
        description="VISA resource of the sensor, e.g. ASRL6::INSTR (serial port COM6 on "
                    "Windows). Read when aux_log_enabled.")


class SafetySettings(SettingsModel):
    """Touch-safety warning threshold. Serialized inside ``measurement``.

    ``safety_voltage_warn_silenced`` is the sticky per-profile flag behind the
    warning dialog's "don't show again" checkbox.
    ``safety_voltage_warn_silenced_until`` is the timed form, a Unix time:
    the warning is silenced until then and back by itself afterwards. An
    expired value is ignored, never cleared (``safety.warning_silenced``).

    The threshold runs to 1100 V, the 2410's range and the Settings dialog's
    maximum: a threshold the dialog can save must not make the profile
    unrunnable here. 0 disables the warning. Every key here belongs to the
    profile; a strict run request may not send any (``resolve.SAFETY_KEYS``).
    """

    safety_voltage_warn_v: float = Field(default=_M['safety_voltage_warn_v'], ge=0.0, le=1100.0)
    safety_voltage_warn_silenced: bool = _M['safety_voltage_warn_silenced']
    safety_voltage_warn_silenced_until: Optional[float] = Field(
        default=_M['safety_voltage_warn_silenced_until'], ge=0.0, allow_inf_nan=False)


class AgentLimitSettings(SettingsModel):
    """What a run an AI agent starts may put on the device. Its own section.

    A section beside ``measurement`` rather than more keys inside it, because
    these are not settings of a run: the resolver never copies them into one,
    so they cannot reach a data file, and a run request only carries
    measurement keys. They bound the ``agent`` role only; a run a person
    starts is never held to them (``docs/design/mcp_layer.md`` M4).

    None is no cap beyond the instrument's own. A cap is a positive, finite
    number: 0 would refuse every run, which is what turning agents off is for.
    """

    max_voltage_v: Optional[float] = Field(
        default=_A['max_voltage_v'], gt=0.0, allow_inf_nan=False, strict=True,
        description="Largest |V| an agent's run may source or allow as compliance, in V. "
                    "None = the instrument's own limit.")
    max_current_a: Optional[float] = Field(
        default=_A['max_current_a'], gt=0.0, allow_inf_nan=False, strict=True,
        description="Largest |I| an agent's run may source or allow as compliance, in A. "
                    "None = the instrument's own limit.")
    max_power_w: Optional[float] = Field(
        default=_A['max_power_w'], gt=0.0, allow_inf_nan=False, strict=True,
        description="Largest |V| x |I| an agent's run could deliver, in W. "
                    "None = the instrument's own limit.")


class FileSettings(SettingsModel):
    """Where rows are written and how often they are flushed."""

    auto_save_interval: int = Field(default=_F['auto_save_interval'], ge=1, le=3600)
    data_directory: str = Field(default=_F['data_directory'], min_length=1)


class OutputSettings(SettingsModel):
    """Exporter selection; consumed by ``data_export.make_exporter``."""

    format: Literal['csv', 'hdf5', 'csv+legacy_json'] = _O['format']
    compression: Literal['never', 'always', 'auto'] = _O['compression']
    compression_threshold_mb: float = Field(default=_O['compression_threshold_mb'], ge=0.0)


class DisplaySettings(SettingsModel):
    """Plot appearance. Frontend-only, modelled so a profile round-trips."""

    enable_plot: bool = _D['enable_plot']
    plot_update_interval: int = Field(default=_D['plot_update_interval'], ge=1, le=10000)
    plot_color_r: str = _D['plot_color_r']
    plot_color_v: str = _D['plot_color_v']
    plot_color_i: str = _D['plot_color_i']
    plot_figsize: List[float] = Field(default_factory=lambda: list(_D['plot_figsize']))
    # 0 (or None, in older configs) means unlimited.
    buffer_size: Optional[int] = Field(default=_D['buffer_size'], ge=0)

"""Pluggable auxiliary-sensor layer for co-logging arbitrary measurements
alongside the electrical channel.

The point of this module is generality. A researcher should be able to wire
*any* secondary instrument — a thermocouple, a strain/stress gauge, a flow
meter, a hygrometer — and have its readings timestamped into the same record
as the Keithley measurement, with data columns and a live readout that appear
automatically. Nothing downstream is sensor-specific: the worker, the
exporter, and the UI all iterate over what a sensor *declares* about itself
via :meth:`AuxiliarySensor.channels`.

Concrete drivers either live here or in a researcher's own package; each
implements the :class:`AuxiliarySensor` Protocol (four methods). The first
shipped driver is an Arduino K-type thermocouple that streams CSV lines over
USB-serial (ASRL via pyvisa), but it carries no privileged status — it is
simply one implementation of the contract, and the smallest example of how to
write another.

This module is pure (no Qt). The one concrete serial driver subclasses
:class:`~resistamet_gui.instrument.VisaInstrument`, so it inherits the
connection lifecycle and works under ``--simulate`` with no extra harness.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Optional, Protocol, runtime_checkable

import pyvisa

from .instrument import VisaInstrument


class SensorError(Exception):
    """Base class for auxiliary-sensor failures."""


class SensorReadError(SensorError):
    """Raised when a sensor produces no valid reading."""


@dataclass(frozen=True)
class SensorChannel:
    """One scalar quantity a sensor reports.

    ``key`` is the stable identifier used for the data-dict / CSV column
    (prefixed ``aux_`` downstream). ``label`` and ``unit`` drive the live
    readout and any plot axes. A sensor's list of channels is its
    self-description — the thing that lets the rest of the app stay
    sensor-agnostic.
    """
    key: str
    label: str
    unit: str


@dataclass(frozen=True)
class SensorReading:
    """One synchronized sample across all of a sensor's channels.

    ``values`` is keyed by :attr:`SensorChannel.key`. ``flags`` carries any
    per-channel or device fault/status codes (0 == OK by convention). ``ok``
    is True only when every flag is zero. A faulted reading is still returned
    so a caller can record *and* mark it, rather than silently dropping data.
    """
    timestamp: float
    values: dict[str, float]
    flags: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(v == 0 for v in self.flags.values())


@runtime_checkable
class AuxiliarySensor(Protocol):
    """The contract every co-logged sensor implements.

    Four methods. :meth:`channels` is the self-description that makes the
    whole pipeline sensor-agnostic: declare what you measure, and the worker,
    exporter, and UI handle the rest. A driver need not be serial or VISA —
    anything that can produce a :class:`SensorReading` qualifies.
    """

    def open(self) -> "AuxiliarySensor": ...
    def channels(self) -> list[SensorChannel]: ...
    def read_latest(self) -> SensorReading: ...
    def close(self) -> None: ...


class SerialLineSensor(VisaInstrument):
    """Reusable base for sensors that *stream* delimited ASCII lines over a
    serial (ASRL) link — the most common lab-bench shape.

    A subclass declares :attr:`CHANNELS` and implements :meth:`parse_line`.
    This base provides:

    * connection — inherited from :class:`VisaInstrument`, so the
      ``--simulate`` monkeypatch seam works unchanged;
    * freshness — :meth:`read_latest` discards buffered (stale) input, then
      reads the newest line, so the value reflects the read instant;
    * resync — partial / non-conforming lines are skipped until one parses.

    The device only streams, so :meth:`read_latest` never writes to it.
    """

    CHANNELS: list[SensorChannel] = []
    MAX_READ_ATTEMPTS = 8

    def __init__(self, resource: str, timeout_ms: int = 3000,
                 clock: Callable[[], float] = time.time):
        super().__init__(resource, timeout_ms)
        self._clock = clock

    def open(self) -> "SerialLineSensor":
        self.connect()  # VisaInstrument.connect(): opens dev, sets '\n' terminations
        return self

    def channels(self) -> list[SensorChannel]:
        return list(self.CHANNELS)

    def parse_line(self, line: str) -> Optional[SensorReading]:
        """Parse one raw line into a reading, or return None if the line is
        not a valid record (partial line, wrong tag, bad field count).
        Subclasses implement this."""
        raise NotImplementedError

    def _drain(self) -> None:
        """Best-effort discard of buffered input so the next read is fresh."""
        try:
            self.dev.flush(pyvisa.constants.BufferOperation.discard_read_buffer)
        except Exception:
            pass

    def read_latest(self) -> SensorReading:
        self._drain()
        last_err: Optional[Exception] = None
        for _ in range(self.MAX_READ_ATTEMPTS):
            try:
                raw = self.dev.read()
            except Exception as exc:  # timeout / decode / closed port
                last_err = exc
                continue
            line = raw.strip() if isinstance(raw, str) else str(raw).strip()
            reading = self.parse_line(line)
            if reading is not None:
                return replace(reading, timestamp=self._clock())
        raise SensorReadError(
            f"No valid reading from {self.resource_str} after "
            f"{self.MAX_READ_ATTEMPTS} attempts"
            + (f": {last_err}" if last_err is not None else "")
        )


# --- First concrete driver: Arduino K-type thermocouple ---------------------

_TC_LINE_RE = re.compile(
    r"^DATA,(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+),(-?\d+)$"
)


def parse_thermocouple_line(line: str) -> Optional[SensorReading]:
    """Parse one Arduino thermocouple line.

    Format: ``DATA,<tip_C>,<coldjunction_C>,<fault>,<status>`` (CRLF on the
    wire; caller strips). Returns None for partial or malformed lines so the
    reader resyncs on the next ``DATA,`` record. The returned reading has
    ``timestamp=0.0``; :meth:`SerialLineSensor.read_latest` stamps the real
    time at the read instant.
    """
    if not line:
        return None
    m = _TC_LINE_RE.match(line)
    if not m:
        return None
    tip, cold, fault, status = m.groups()
    return SensorReading(
        timestamp=0.0,
        values={"t_sample": float(tip), "t_coldjunction": float(cold)},
        flags={"t_sample": int(fault), "status": int(status)},
    )


class ArduinoThermocouple(SerialLineSensor):
    """Arduino K-type thermocouple over USB-serial (e.g. a MAX31855 board).

    Streams ``DATA,<tip_C>,<coldjunction_C>,<fault>,<status>`` at a few Hz.
    Native-USB CDC, so the baud rate is ignored; addressed as an ASRL VISA
    resource (``ASRL6::INSTR`` on the lab rig). Field 1 is the sample
    temperature, field 2 the chip cold-junction (diagnostic), fields 3-4 are
    fault/status flags (0 == OK).
    """

    CHANNELS = [
        SensorChannel("t_sample", "Sample (K-type)", "°C"),
        SensorChannel("t_coldjunction", "Cold junction", "°C"),
    ]

    def parse_line(self, line: str) -> Optional[SensorReading]:
        return parse_thermocouple_line(line)


# --- Registry ---------------------------------------------------------------
#
# A plain name->class dict, mirroring instrument._MODELS. In-tree drivers are
# registered below; a third-party package registers its own driver at import
# time via register_sensor(). A setuptools entry-point group can replace this
# lookup later without changing the AuxiliarySensor contract.

_SENSORS: dict[str, type] = {
    "arduino_thermocouple": ArduinoThermocouple,
}


def register_sensor(name: str, cls: type) -> None:
    """Register an :class:`AuxiliarySensor` driver under a short name so it can
    be selected from settings."""
    _SENSORS[name] = cls


def available_sensors() -> tuple[str, ...]:
    """Return the registered driver names."""
    return tuple(_SENSORS)


def make_sensor(driver: str, address: str, **opts) -> AuxiliarySensor:
    """Construct (but do not open) a registered sensor driver."""
    try:
        cls = _SENSORS[driver]
    except KeyError:
        raise ValueError(
            f"Unknown sensor driver {driver!r}. Available: "
            f"{', '.join(sorted(_SENSORS)) or '(none)'}"
        )
    return cls(address, **opts)


# --- Generic glue used by the worker / exporter -----------------------------

def aux_column_names(sensor: AuxiliarySensor) -> list[str]:
    """Column names a sensor contributes, derived purely from ``channels()``.

    Lets the exporter build headers without knowing the sensor type.
    """
    return [f"aux_{ch.key}" for ch in sensor.channels()]


def reading_to_columns(reading: SensorReading) -> dict[str, float]:
    """Flatten a reading into ``aux_<key>`` columns (plus ``aux_<key>_flag``
    for any flagged channel) for merging into a measurement row dict."""
    cols: dict[str, float] = {f"aux_{k}": v for k, v in reading.values.items()}
    for key, flag in reading.flags.items():
        cols[f"aux_{key}_flag"] = flag
    return cols

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

Streaming serial drivers run a background reader thread that continuously
parses lines into a latest-value cache, so :meth:`read_latest` is a
non-blocking cache read — the acquisition loop and the GUI never wait on the
stream. Fault provenance is carried per row in a single ``aux_fault`` column
(see :func:`reading_to_columns`); channel values are preserved even when
flagged, so fault-time data is recorded *and* marked, never silently dropped.

This module is pure (no Qt). The one concrete serial driver subclasses
:class:`~resistamet_gui.instrument.VisaInstrument`, so it inherits the
connection lifecycle and works under ``--simulate`` with no extra harness.
"""
from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Optional, Protocol, runtime_checkable

from pyvisa.constants import Parity, StopBits

from .constants import AUX_STALE_AFTER_S
from .instrument import VisaInstrument

# Every sensor column carries this prefix; the provenance column closes the
# block. See aux_column_names / reading_to_columns.
AUX_COLUMN_PREFIX = "aux_"
AUX_FAULT_COLUMN = AUX_COLUMN_PREFIX + "fault"

# Flag a stream parser sets on a channel whose value arrived as nan or inf.
# It reads as ``<key>=1`` in the aux_fault column.
FLAG_NON_FINITE = 1


class SensorError(Exception):
    """Base class for auxiliary-sensor failures."""


class SensorReadError(SensorError):
    """Raised when a sensor produces no valid reading."""


class SensorHeaderError(SensorError):
    """Raised when a device's channel description cannot be used. The message
    names the offending field or key."""


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

    def age_s(self, now: float) -> float:
        """Seconds between this reading and ``now``.

        ``read_latest`` hands back the newest cached reading for as long as
        it is younger than ``AUX_STALE_AFTER_S``, so a row can carry a value
        several seconds older than the measurement beside it. This is how a
        caller finds out how old. ``now`` must come from the clock that
        stamped the reading: the driver's ``clock`` argument, ``time.time``
        unless one was injected. Never negative: a clock stepped backwards
        reads as zero.
        """
        return max(0.0, now - self.timestamp)


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


_STOP_BITS = {1: StopBits.one, 1.5: StopBits.one_and_a_half, 2: StopBits.two}


def serial_session_attributes(baud_rate: Optional[int] = None,
                              data_bits: Optional[int] = None,
                              parity: Optional[str] = None,
                              stop_bits: Optional[float] = None,
                              termination: Optional[str] = None) -> dict:
    """Turn serial-line settings into the VISA session attributes to set.

    Only the settings that are given appear in the result, so a link left
    unconfigured keeps whatever the VISA backend defaults to. ``parity`` is
    one of none / odd / even / mark / space; ``stop_bits`` is 1, 1.5 or 2;
    ``termination`` is the character(s) that end a line from the device.
    Raises ValueError naming the setting that is out of range.
    """
    attrs: dict = {}
    if baud_rate is not None:
        if isinstance(baud_rate, bool) or not isinstance(baud_rate, int) or baud_rate <= 0:
            raise ValueError(f"baud_rate must be a positive integer, got {baud_rate!r}")
        attrs["baud_rate"] = baud_rate
    if data_bits is not None:
        if data_bits not in (5, 6, 7, 8):
            raise ValueError(f"data_bits must be 5, 6, 7 or 8, got {data_bits!r}")
        attrs["data_bits"] = int(data_bits)
    if parity is not None:
        try:
            attrs["parity"] = (parity if isinstance(parity, Parity)
                               else Parity[str(parity).strip().lower()])
        except KeyError:
            raise ValueError(
                f"parity must be one of {', '.join(p.name for p in Parity)}, "
                f"got {parity!r}"
            ) from None
    if stop_bits is not None:
        if isinstance(stop_bits, StopBits):
            attrs["stop_bits"] = stop_bits
        elif stop_bits in _STOP_BITS:
            attrs["stop_bits"] = _STOP_BITS[stop_bits]
        else:
            raise ValueError(f"stop_bits must be 1, 1.5 or 2, got {stop_bits!r}")
    if termination is not None:
        if not isinstance(termination, str) or not termination:
            raise ValueError(
                f"termination must be a non-empty string, got {termination!r}"
            )
        attrs["read_termination"] = termination
    return attrs


class SerialLineSensor(VisaInstrument):
    """Reusable base for sensors that *stream* delimited ASCII lines over a
    serial (ASRL) link — the most common lab-bench shape.

    A subclass declares :attr:`CHANNELS` and implements :meth:`parse_line`.
    This base provides:

    * connection — inherited from :class:`VisaInstrument`, so the
      ``--simulate`` monkeypatch seam works unchanged;
    * a background reader thread — started by :meth:`open`, it consumes the
      stream continuously and caches the newest parsed reading, so
      :meth:`read_latest` is a NON-BLOCKING cache read (the caller never
      waits on the serial link) and stale data is detected by age;
    * resync — partial / non-conforming lines are skipped by the reader.

    The device only streams; nothing here writes to it.

    A native-USB board ignores the line settings. A device behind a real
    UART does not: pass ``baud_rate`` / ``data_bits`` / ``parity`` /
    ``stop_bits`` / ``termination`` (see :func:`serial_session_attributes`)
    and :meth:`open` sets them on the session before the reader starts. Any
    left out stay at the VISA backend's default.
    """

    CHANNELS: list[SensorChannel] = []

    def __init__(self, resource: str, timeout_ms: int = 3000,
                 clock: Callable[[], float] = time.time, *,
                 baud_rate: Optional[int] = None,
                 data_bits: Optional[int] = None,
                 parity: Optional[str] = None,
                 stop_bits: Optional[float] = None,
                 termination: Optional[str] = None):
        super().__init__(resource, timeout_ms)
        # Validated here so a bad setting fails before any port is opened.
        self._serial_attrs = serial_session_attributes(
            baud_rate, data_bits, parity, stop_bits, termination)
        self._clock = clock
        # Injectable for deterministic staleness tests.
        self._monotonic: Callable[[], float] = time.monotonic
        self._latest: Optional[tuple[SensorReading, float]] = None
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._reader: Optional[threading.Thread] = None
        # Why the device's channel description was refused, if it was;
        # wait_ready() reports it instead of a bare timeout.
        self._header_error: Optional[str] = None

    def open(self) -> "SerialLineSensor":
        """Connect and start the reader. The VISA connect (resource listing
        plus open) runs on the calling thread and takes as long as the
        backend takes; only the reads that follow are off-thread."""
        self.connect()  # VisaInstrument.connect(): opens dev, sets '\n' terminations
        try:
            for name, value in self._serial_attrs.items():
                setattr(self.dev, name, value)
        except Exception:
            self.close()  # do not leave the port held by a half-set session
            raise
        self._start_reader()
        return self

    def _start_reader(self) -> None:
        """Start the background reader thread (split from open() so tests can
        inject a fake ``dev`` and start the loop without a VISA connect)."""
        # A new event per reader, not clear(): a previous reader that outlived
        # close() still holds its own, set, and must not be woken by a reopen.
        self._stop_evt = threading.Event()
        self._reader = threading.Thread(
            target=self._read_loop, args=(self._stop_evt,),
            name=f"aux-reader:{self.resource_str}", daemon=True,
        )
        self._reader.start()

    def channels(self) -> list[SensorChannel]:
        return list(self.CHANNELS)

    def parse_line(self, line: str) -> Optional[SensorReading]:
        """Parse one raw line into a reading, or return None if the line is
        not a valid record (partial line, wrong tag, bad field count).
        Subclasses implement this."""
        raise NotImplementedError

    def _read_loop(self, stop: threading.Event) -> None:
        """Background thread: consume the stream, cache the newest reading.

        A blocking ``dev.read()`` here only ever stalls this thread, never
        a caller of :meth:`read_latest`. :meth:`close` closes the device
        underneath it; whether that ends a read already in progress is up to
        the VISA backend. Where it does not, the read runs out its own
        timeout and this thread exits then, discarding what it read.
        """
        while not stop.is_set():
            dev = self.dev
            if dev is None:
                return
            try:
                raw = dev.read()
            except Exception:
                if stop.is_set() or self.dev is None:
                    return
                # Timeout / decode hiccup: brief pause so a persistently
                # failing device can't spin this thread hot.
                time.sleep(0.005)
                continue
            if stop.is_set():
                return  # closed while reading: this line belongs to nobody
            line = raw.strip() if isinstance(raw, str) else str(raw).strip()
            reading = self.parse_line(line)
            if reading is not None:
                stamped = replace(reading, timestamp=self._clock())
                with self._lock:
                    if stop.is_set():
                        return  # close() cleared the cache; leave it clear
                    self._latest = (stamped, self._monotonic())

    def read_latest(self) -> SensorReading:
        """Return the newest cached reading. Non-blocking.

        Raises :class:`SensorReadError` when nothing has been cached yet or
        the cache is older than ``constants.AUX_STALE_AFTER_S`` (stream died,
        cable bumped, device rebooting).
        """
        with self._lock:
            latest = self._latest
        if latest is None:
            raise SensorReadError(
                f"No reading from {self.resource_str} yet"
            )
        reading, cached_at = latest
        age = self._monotonic() - cached_at
        if age > AUX_STALE_AFTER_S:
            raise SensorReadError(
                f"Reading from {self.resource_str} is stale ({age:.1f}s old)"
            )
        return reading

    def wait_for_reading(self, timeout_s: float) -> SensorReading:
        """Block (politely) until a fresh reading is cached, or raise.

        For callers on threads where blocking is acceptable (the worker
        thread, tests) — never call from the GUI thread.
        """
        deadline = self._monotonic() + timeout_s
        while True:
            try:
                return self.read_latest()
            except SensorReadError:
                if self._monotonic() >= deadline:
                    raise SensorReadError(
                        f"No valid reading from {self.resource_str} "
                        f"within {timeout_s:.1f}s"
                    )
                time.sleep(0.02)

    def wait_ready(self, timeout_s: float) -> None:
        """Block until the sensor is fully usable: channels are known AND a
        first reading is cached. For self-describing drivers (StreamSensor)
        this also covers header discovery."""
        deadline = self._monotonic() + timeout_s
        while not self.channels():
            if self._monotonic() >= deadline:
                if self._header_error:
                    raise SensorError(
                        f"{self.resource_str}: unusable channel description "
                        f"— {self._header_error}"
                    )
                raise SensorError(
                    f"{self.resource_str}: no channel description within "
                    f"{timeout_s:.1f}s — wrong device or driver?"
                )
            time.sleep(0.02)
        remaining = max(0.05, deadline - self._monotonic())
        self.wait_for_reading(remaining)

    def close(self) -> None:
        """Close the device and stop the reader. This call can block.

        It closes the VISA session, which is as quick as the backend makes
        it, then waits up to 1 s for the reader thread. A backend that does
        not abort a pending ``read()`` when its session closes leaves the
        reader in that read, so the full second is spent and the thread
        (a daemon) is still alive on return; it exits when the read times
        out. Callers on a GUI thread should expect that delay.
        """
        self._stop_evt.set()
        try:
            super().close()  # ends a pending reader read() if the backend allows
        finally:
            reader = self._reader
            if reader is not None and reader.is_alive():
                reader.join(timeout=1.0)
            self._reader = None
            with self._lock:
                self._latest = None


# --- First concrete driver: Arduino K-type thermocouple ---------------------

_TC_LINE_RE = re.compile(
    r"^DATA,(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+),(-?\d+)$"
)


def parse_thermocouple_line(line: str) -> Optional[SensorReading]:
    """Parse one Arduino thermocouple line.

    Format: ``DATA,<tip_C>,<coldjunction_C>,<fault>,<status>`` (CRLF on the
    wire; caller strips). Returns None for partial or malformed lines so the
    reader resyncs on the next ``DATA,`` record. The returned reading has
    ``timestamp=0.0``; the reader thread stamps the real time at the cache
    instant.
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
    """Arduino K-type thermocouple over USB-serial (e.g. a MAX31856 board).

    Streams ``DATA,<tip_C>,<coldjunction_C>,<fault>,<status>`` at a few Hz.
    Native-USB CDC, so the baud rate is ignored; addressed as an ASRL VISA
    resource (``ASRL6::INSTR`` on the lab rig). Field 1 is the sample
    temperature, field 2 the chip cold-junction (diagnostic), fields 3-4 are
    fault/status flags (0 == OK).

    On open the board emits a one-line banner then ``READY`` before the data
    stream (e.g. "maxwelld foam-TC v1 MAX31856 K-type CS7"); the reader
    thread resyncs past those non-DATA lines automatically.
    """

    CHANNELS = [
        SensorChannel("t_sample", "Sample (K-type)", "°C"),
        SensorChannel("t_coldjunction", "Cold junction", "°C"),
    ]

    def parse_line(self, line: str) -> Optional[SensorReading]:
        return parse_thermocouple_line(line)


# --- General multi-channel driver: self-describing stream -------------------

# A channel key becomes the column ``aux_<key>`` in the CSV header and a field
# name in the HDF5 compound dtype, so it is held to identifier characters:
# no ``/`` (an HDF5 path separator), no ``#`` (the CSV comment marker), no
# spaces, no leading digit.
_CHANNEL_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def reserved_channel_keys() -> frozenset:
    """Keys a sensor may not declare, because ``aux_<key>`` would repeat a
    column the record already has: the ``aux_fault`` provenance column, or
    any built-in column of a co-logging mode that carries the aux prefix.

    Computed from the exporter's own column tables rather than listed here,
    so a built-in column added later is reserved without touching this file.
    """
    from .data_export import AUX_LOG_MODES, get_column_config

    taken = {AUX_FAULT_COLUMN}
    for mode in AUX_LOG_MODES:
        for settings in (None, {"fpp_delta_mode": True}):
            taken.update(get_column_config(mode, settings)[0])
    return frozenset(
        col[len(AUX_COLUMN_PREFIX):] for col in taken
        if col.startswith(AUX_COLUMN_PREFIX)
    )


def validate_channel_key(key: str, reserved: Optional[frozenset] = None) -> None:
    """Raise :class:`SensorHeaderError` naming ``key`` if it cannot be a
    channel key: wrong characters, or reserved (see
    :func:`reserved_channel_keys`)."""
    if not _CHANNEL_KEY_RE.match(key):
        raise SensorHeaderError(
            f"channel key {key!r} is not a letter followed by letters, "
            f"digits or underscores"
        )
    if reserved is None:
        reserved = reserved_channel_keys()
    if key in reserved:
        raise SensorHeaderError(
            f"channel key {key!r} is reserved: its column "
            f"{AUX_COLUMN_PREFIX}{key} already exists in the record"
        )


def parse_stream_header(line: str) -> Optional[list[SensorChannel]]:
    """Parse a ``HDR,<key>:<unit>,<key>:<unit>,...`` line into channels.

    Returns None for a line that is not a header at all (no ``HDR,`` tag), so
    the reader can skip it. A line that IS a header but cannot be used raises
    :class:`SensorHeaderError` naming the offending field or key: a field
    that is not ``<key>:<unit>`` (an empty header included), a key that
    fails :func:`validate_channel_key`, or a DUPLICATE key.
    Duplicate or colliding column names would silently collapse a channel in
    the CSV and abort the HDF5 exporter mid-run, so they are refused at the
    source. Units are ASCII wire tokens (e.g. ``degC``, ``uS/cm``) surfaced
    verbatim; the label is derived from the key.
    """
    if not line.startswith("HDR,"):
        return None
    fields = line.split(",")[1:]
    reserved = reserved_channel_keys()
    chans: list[SensorChannel] = []
    seen: set[str] = set()
    for f in fields:
        if ":" not in f:
            raise SensorHeaderError(
                f"header field {f.strip()!r} is not <key>:<unit>"
            )
        key, _, unit = f.partition(":")
        key = key.strip()
        unit = unit.strip()
        validate_channel_key(key, reserved)
        if key in seen:
            raise SensorHeaderError(f"channel key {key!r} is declared twice")
        seen.add(key)
        chans.append(SensorChannel(key, key.replace("_", " ").title(), unit))
    return chans


def parse_stream_data(line: str,
                      channels: list[SensorChannel]) -> Optional[SensorReading]:
    """Parse a ``DATA,<v1>,<v2>,...`` row positionally against ``channels``.

    Returns None for partial lines, wrong field count, or non-numeric values
    so the reader resyncs on the next valid row.

    ``nan`` and ``inf`` are what firmware prints for an open or failed
    transducer, so they are not clean data. Such a value is kept as received
    and its channel is flagged :data:`FLAG_NON_FINITE`, which makes the
    reading not-ok and puts ``<key>=1`` in ``aux_fault``. The flag is per
    channel, like every other fault in that column: the other channels of the
    same row are good data and stay usable, which marking the whole reading
    (or dropping it, as ``read_error`` does) would throw away.
    """
    if not line.startswith("DATA,") or not channels:
        return None
    fields = line.split(",")[1:]
    if len(fields) != len(channels):
        return None
    try:
        vals = {ch.key: float(f) for ch, f in zip(channels, fields)}
    except ValueError:
        return None
    flags = {k: FLAG_NON_FINITE for k, v in vals.items() if not math.isfinite(v)}
    return SensorReading(timestamp=0.0, values=vals, flags=flags)


class StreamSensor(SerialLineSensor):
    """Generic multi-channel streaming sensor with self-describing channels.

    The device announces its channels in a header line at connect, then streams
    positional data rows::

        HDR,pressure:psi,t_sample:degC,force:N
        DATA,32.5,24.1,0.98
        ...

    This is the general case behind the Characterization Bench: one serial link,
    many channels, declared by the device — not hardcoded here. The reader
    thread captures the header (so :meth:`open` does not wait for it) and
    :meth:`channels` returns the dynamically discovered channels; callers use
    :meth:`wait_ready` to block until discovery completes. The worker /
    exporter / UI build columns from whatever the device reports.
    """

    def __init__(self, resource: str, timeout_ms: int = 3000,
                 clock: Callable[[], float] = time.time, **serial):
        super().__init__(resource, timeout_ms, clock, **serial)
        self._channels: list[SensorChannel] = []

    def channels(self) -> list[SensorChannel]:
        return list(self._channels)

    def parse_line(self, line: str) -> Optional[SensorReading]:
        # Header capture happens in the reader thread: until the device has
        # described itself, every line is tried as a header; afterwards,
        # rows parse positionally against the discovered channels.
        if not self._channels:
            try:
                chans = parse_stream_header(line)
            except SensorHeaderError as e:
                # Keep reading: a later, usable header still wins.
                self._header_error = str(e)
                return None
            if chans:
                self._header_error = None
                self._channels = chans
            return None
        return parse_stream_data(line, self._channels)


# --- Registry ---------------------------------------------------------------
#
# A plain name->class dict, mirroring instrument._MODELS. In-tree drivers are
# registered below; a third-party package registers its own driver at import
# time via register_sensor(). A setuptools entry-point group can replace this
# lookup later without changing the AuxiliarySensor contract.

_SENSORS: dict[str, type] = {
    "arduino_thermocouple": ArduinoThermocouple,
    "stream_sensor": StreamSensor,
}


def register_sensor(name: str, cls: type) -> None:
    """Register an :class:`AuxiliarySensor` driver under a short name so it can
    be selected from settings."""
    _SENSORS[name] = cls


def available_sensors() -> tuple[str, ...]:
    """Return the registered driver names."""
    return tuple(_SENSORS)


def make_sensor(driver: str, address: str, **opts) -> AuxiliarySensor:
    """Construct (but do not open) a registered sensor driver.

    ``opts`` go to the driver's constructor unchanged. The serial drivers
    here take ``timeout_ms`` and the line settings of
    :class:`SerialLineSensor`; a driver that is not serial need not accept
    them, so pass only what was actually configured.
    """
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
    """Column names a sensor contributes: one ``aux_<key>`` per channel plus
    the trailing ``aux_fault`` provenance column.

    Derived purely from ``channels()`` so the exporter builds headers without
    knowing the sensor type.
    """
    return ([f"{AUX_COLUMN_PREFIX}{ch.key}" for ch in sensor.channels()]
            + [AUX_FAULT_COLUMN])


def format_fault(flags: dict[str, int]) -> str:
    """Render a reading's flags as the ``aux_fault`` column value.

    ``"0"`` when every flag is zero; otherwise the nonzero flags joined as
    ``key=value`` pairs (e.g. ``"t_sample=1;status=2"``). The worker writes
    ``"read_error"`` when no reading was available at all.
    """
    nonzero = {k: v for k, v in flags.items() if v != 0}
    if not nonzero:
        return "0"
    return ";".join(f"{k}={v}" for k, v in nonzero.items())


def reading_to_columns(reading: SensorReading) -> dict[str, object]:
    """Flatten a reading into ``aux_<key>`` value columns plus the single
    ``aux_fault`` provenance column, for merging into a measurement row dict.

    Channel VALUES ARE PRESERVED even when flagged — per the
    :class:`SensorReading` contract a faulted reading is recorded *and*
    marked, never silently dropped; downstream analysis decides what to do
    with fault-time data.
    """
    cols: dict[str, object] = {
        f"{AUX_COLUMN_PREFIX}{k}": v for k, v in reading.values.items()
    }
    cols[AUX_FAULT_COLUMN] = format_fault(reading.flags)
    return cols

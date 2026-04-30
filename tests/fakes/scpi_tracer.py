"""Recording wrapper around a pyvisa resource.

Wraps any object that exposes ``write(cmd)`` and ``query(cmd)`` and records
each call into a :class:`Trace`. Used by the capture scripts in
``tests/hardware/`` to harvest golden SCPI traces from real hardware.
"""
from __future__ import annotations

from typing import Protocol

from .trace_format import Trace, TraceEvent


class _ScpiDevice(Protocol):
    def write(self, cmd: str) -> object: ...
    def query(self, cmd: str) -> str: ...


class ScpiTracer:
    """Tee every write/query to an in-memory :class:`Trace`."""

    def __init__(self, device: _ScpiDevice, trace: Trace):
        self._dev = device
        self.trace = trace

    def write(self, cmd: str) -> object:
        self.trace.events.append(TraceEvent(op="write", cmd=cmd))
        return self._dev.write(cmd)

    def query(self, cmd: str) -> str:
        response = self._dev.query(cmd).strip()
        self.trace.events.append(TraceEvent(op="query", cmd=cmd, response=response))
        return response

    # Pass-through for attribute access (timeout, read_termination, etc.)
    def __getattr__(self, name: str):
        return getattr(self._dev, name)

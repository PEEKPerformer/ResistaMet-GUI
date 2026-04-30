"""SCPI trace I/O.

A trace is an ordered list of (op, command, response) events captured from a
real instrument. Traces are committed to ``tests/fixtures/scpi_traces/`` as
JSON and serve two purposes:

    1. Hardware-tier tests recapture each trace and diff against the golden
       file to detect firmware drift.
    2. CI-tier tests replay each trace through the FakeKeithley and assert
       byte-identical responses, validating the simulator.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional


@dataclass
class TraceEvent:
    op: str                      # "write" or "query"
    cmd: str
    response: Optional[str] = None     # only set when op == "query"


@dataclass
class Trace:
    name: str
    description: str
    instrument_idn: str
    captured_at: str             # ISO 8601 date or datetime
    dut_resistance_ohms: float
    events: list[TraceEvent] = field(default_factory=list)

    def to_json(self) -> str:
        payload = asdict(self)
        return json.dumps(payload, indent=2)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n", encoding="utf-8")

    @classmethod
    def from_json(cls, raw: str) -> "Trace":
        data = json.loads(raw)
        events = [TraceEvent(**e) for e in data.pop("events", [])]
        return cls(events=events, **data)

    @classmethod
    def read(cls, path: Path) -> "Trace":
        return cls.from_json(path.read_text(encoding="utf-8"))


def trace_dir() -> Path:
    """Project-wide canonical location for golden traces."""
    return Path(__file__).resolve().parent.parent / "fixtures" / "scpi_traces"


def iter_trace_files() -> Iterable[Path]:
    """Yield every committed *.json trace, excluding the diagnosis report."""
    for p in sorted(trace_dir().glob("*.json")):
        if p.name.startswith("diagnosis_"):
            continue
        yield p

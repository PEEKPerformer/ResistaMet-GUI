"""Auto-discover and replay community-submitted SCPI traces.

When a contributor runs ``scripts/community_capture.py`` on their Keithley
and the maintainers merge the resulting zip into
``tests/fixtures/scpi_traces_community/<model>_<serial>/``, this test picks
up every JSON trace under that directory and replays it through the
FakeKeithley.

Each merged submission becomes one row of cross-model validation. If a
submission would fail (e.g., a 2450 with divergent STAT bits), the
maintainers can either update the simulator and ship the fix, or keep the
trace as a known-divergence fixture (with a marker that opts it out of
strict comparison) and document the gap in ``docs/sim_fidelity.md``.

The test is parametrized over the discovered file set, so it does nothing
when no community traces have been merged — empty is a valid state.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes.fake_keithley import FakeKeithley
from tests.fakes.trace_format import Trace
from tests.test_fake_matches_hardware import (
    _compare_read,
    _compare_value_query,
    _form_elem_count,
    _is_read_query,
)


def _community_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "scpi_traces_community"


def _community_traces():
    """Discover every JSON trace under ``scpi_traces_community/<submission>/``."""
    base = _community_dir()
    if not base.is_dir():
        return []
    files = sorted(base.rglob("*.json"))
    return [
        pytest.param(p, id=str(p.relative_to(base)).replace("/", "__"))
        for p in files
    ]


@pytest.mark.parametrize("trace_path", _community_traces())
def test_community_trace_replays_through_fake(trace_path):
    trace = Trace.read(trace_path)
    # Use the trace's recorded model when it's part of the schema, otherwise
    # default to the bench reference.
    raw = trace_path.read_text(encoding="utf-8")
    fake_kwargs = {"dut_resistance_ohms": trace.dut_resistance_ohms}
    if '"model": "' in raw:
        # Community trace format includes a model field — parse it out
        import json as _json
        data = _json.loads(raw)
        if data.get("model"):
            fake_kwargs["model"] = data["model"]
    fake = FakeKeithley(**fake_kwargs)

    for i, ev in enumerate(trace.events):
        if ev.op == "write":
            fake.write(ev.cmd)
        elif ev.op == "query":
            response = fake.query(ev.cmd)
            assert ev.response is not None, (
                f"event {i}: trace has no captured response for {ev.cmd!r}"
            )
            if _is_read_query(ev.cmd):
                _compare_read(ev.response, response,
                              elem_count_hint=_form_elem_count(fake.state))
            else:
                _compare_value_query(ev.response, response, ev.cmd)
        else:
            pytest.fail(f"event {i}: unknown op {ev.op!r}")


def test_community_dir_exists():
    """Sanity: the directory should always be present, even if empty."""
    assert _community_dir().is_dir(), (
        f"missing community traces directory: {_community_dir()}"
    )

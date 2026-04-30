"""Validate the FakeKeithley simulator against captured hardware traces.

For each trace under ``tests/fixtures/scpi_traces/``, replay the recorded
write/query sequence through a fresh :class:`FakeKeithley` and assert the
fake's responses match real hardware.

Match policy:
    - Configuration queries (IDN, FORM:ELEM?, error queue, etc.):
      byte-identical match.
    - Settings round-trip queries (NPLC?, VOLT:RANG?, …):
      byte-identical match.
    - ``:READ?`` responses: parse the elements and compare numerically with
      generous tolerance for V/I (the real DUT is 99.567Ω, the fake assumes
      exactly 100Ω). The compliance bit (bit 3) of the STAT element MUST
      match exactly — that's the fidelity claim that matters.

Tests that fail here mean either (a) firmware drift (re-run the hardware
capture in tests/hardware/) or (b) a bug in the simulator.
"""
from __future__ import annotations

import math
import re

import pytest

from tests.fakes.fake_keithley import FakeKeithley, _STAT_BIT_COMPLIANCE
from tests.fakes.trace_format import Trace, iter_trace_files


# Tolerance for measured V/I — accounts for real DUT being ~99.6Ω, not 100Ω,
# plus measurement noise from the 2420's ADC at NPLC=1.
_RELATIVE_TOLERANCE = 0.05      # 5% on V/I
_ABSOLUTE_TOLERANCE = 1e-6      # for near-zero readings


def _all_traces():
    files = list(iter_trace_files())
    return [pytest.param(p, id=p.stem) for p in files]


def _parse_read_elements(response: str) -> list[float]:
    """Parse a comma-separated :READ? response into a list of floats."""
    return [float(p.strip()) for p in response.split(",") if p.strip()]


def _is_read_query(cmd: str) -> bool:
    return cmd.strip().upper() == ":READ?"


def _looks_like_read_response(text: str) -> bool:
    """Heuristic: a colon-free comma list of numeric tokens."""
    if not text or text.startswith('"'):
        return False
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 2:
        return False
    try:
        for p in parts:
            float(p)
    except ValueError:
        return False
    return True


def _compare_read(real: str, fake: str, elem_count_hint: int | None = None) -> None:
    """Assert two :READ? responses are equivalent within tolerance.

    The compliance bit of every STAT element must match EXACTLY.
    """
    real_vals = _parse_read_elements(real)
    fake_vals = _parse_read_elements(fake)
    assert len(real_vals) == len(fake_vals), (
        f"element count mismatch: real={len(real_vals)} fake={len(fake_vals)}\n"
        f"real='{real}'\nfake='{fake}'"
    )
    # Group elements into points if there's a hint (sweep responses pack many
    # points into one response). Otherwise assume one point.
    n = len(real_vals)
    if elem_count_hint and n > elem_count_hint:
        per_point = elem_count_hint
    else:
        per_point = n
    n_points = n // per_point
    for pt in range(n_points):
        for k in range(per_point):
            idx = pt * per_point + k
            r_val = real_vals[idx]
            f_val = fake_vals[idx]
            is_last_in_point = (k == per_point - 1)
            if is_last_in_point:
                # STAT element: compliance bit must match exactly
                r_stat = int(r_val)
                f_stat = int(f_val)
                r_comp = bool(r_stat & _STAT_BIT_COMPLIANCE)
                f_comp = bool(f_stat & _STAT_BIT_COMPLIANCE)
                assert r_comp == f_comp, (
                    f"compliance bit (point {pt}) mismatch: "
                    f"real_stat={r_stat} fake_stat={f_stat}"
                )
            else:
                # V or I or R element: compare with tolerance
                if math.isclose(r_val, f_val, rel_tol=_RELATIVE_TOLERANCE,
                                 abs_tol=_ABSOLUTE_TOLERANCE):
                    continue
                pytest.fail(
                    f"value (point {pt}, elem {k}) outside tolerance: "
                    f"real={r_val:+.4e}  fake={f_val:+.4e}  "
                    f"rel_diff={abs(r_val - f_val) / max(abs(r_val), abs(f_val), 1e-12):.3%}"
                )


def _compare_value_query(real: str, fake: str, cmd: str) -> None:
    """Compare a non-READ? query response.

    Tries a numeric compare first (handles 1.00 vs 1.000000E+00), falls back
    to literal string match.
    """
    if real == fake:
        return
    # Numeric compare with parsing both sides
    try:
        rv = float(real)
        fv = float(fake)
        if math.isclose(rv, fv, rel_tol=1e-3, abs_tol=1e-6):
            return
    except ValueError:
        pass
    pytest.fail(f"response mismatch for {cmd!r}: real={real!r} fake={fake!r}")


def _form_elem_count(state: dict) -> int:
    return len(state.get("form_elem", []))


@pytest.mark.parametrize("trace_path", _all_traces())
def test_replay_trace_through_fake(trace_path):
    trace = Trace.read(trace_path)
    fake = FakeKeithley(dut_resistance_ohms=trace.dut_resistance_ohms)

    for i, ev in enumerate(trace.events):
        if ev.op == "write":
            try:
                fake.write(ev.cmd)
            except Exception as e:
                pytest.fail(f"event {i} ({trace.name}): write {ev.cmd!r} raised {e}")
        elif ev.op == "query":
            try:
                response = fake.query(ev.cmd)
            except Exception as e:
                pytest.fail(f"event {i} ({trace.name}): query {ev.cmd!r} raised {e}")
            assert ev.response is not None, (
                f"event {i}: trace has no captured response for {ev.cmd!r}"
            )
            if _is_read_query(ev.cmd):
                _compare_read(ev.response, response,
                              elem_count_hint=_form_elem_count(fake.state))
            else:
                _compare_value_query(ev.response, response, ev.cmd)
        else:
            pytest.fail(f"event {i} ({trace.name}): unknown op {ev.op!r}")


def test_we_have_traces():
    """Sanity: ensure trace fixtures are committed and discoverable."""
    files = list(iter_trace_files())
    assert files, "no SCPI traces found under tests/fixtures/scpi_traces/"
    assert len(files) >= 10, f"expected >=10 traces, found {len(files)}"

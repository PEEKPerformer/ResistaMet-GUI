"""Hardware-tier: re-execute each captured trace's write/query sequence
against the real instrument and diff the responses against the golden file.

This is the regression net for firmware drift. It does NOT compare
floating-point measurements byte-for-byte (real readings have noise) —
only configuration round-trips and STAT compliance bits are pinned.

Skipped unless RESISTAMET_HARDWARE_ADDR is set.
"""
from __future__ import annotations

import math
import time

import pytest

from tests.fakes.fake_keithley import _STAT_BIT_COMPLIANCE
from tests.fakes.trace_format import Trace, iter_trace_files


_RELATIVE_TOLERANCE = 0.10   # 10% on V/I — DUT R may have shifted slightly
_ABSOLUTE_TOLERANCE = 1e-5


def _all_traces():
    return [pytest.param(p, id=p.stem) for p in iter_trace_files()]


def _drain(dev):
    while True:
        resp = dev.query(":SYST:ERR?").strip()
        if resp.startswith(("0,", "+0,")):
            return


def _compare_read_response(real_now: str, real_then: str) -> None:
    parts_now = [float(p.strip()) for p in real_now.split(",") if p.strip()]
    parts_then = [float(p.strip()) for p in real_then.split(",") if p.strip()]
    assert len(parts_now) == len(parts_then), (
        f"element count drift: now={len(parts_now)} captured={len(parts_then)}"
    )
    # Last element is STAT — compliance bit must match
    stat_now = int(parts_now[-1])
    stat_then = int(parts_then[-1])
    comp_now = bool(stat_now & _STAT_BIT_COMPLIANCE)
    comp_then = bool(stat_then & _STAT_BIT_COMPLIANCE)
    assert comp_now == comp_then, (
        f"compliance bit drift: now={stat_now} captured={stat_then}"
    )
    # Numeric elements: tolerate 10% drift
    for k, (a, b) in enumerate(zip(parts_now[:-1], parts_then[:-1])):
        if math.isclose(a, b, rel_tol=_RELATIVE_TOLERANCE, abs_tol=_ABSOLUTE_TOLERANCE):
            continue
        pytest.fail(
            f"element {k} drift: now={a:+.4e} captured={b:+.4e} "
            f"(rel diff {abs(a-b)/max(abs(a),abs(b),1e-12):.2%})"
        )


@pytest.mark.parametrize("trace_path", _all_traces())
def test_recapture_matches_golden(real_instrument, trace_path):
    trace = Trace.read(trace_path)
    dev = real_instrument

    # Drain any accumulated errors before starting
    try:
        _drain(dev)
    except Exception:
        pass

    for i, ev in enumerate(trace.events):
        if ev.op == "write":
            dev.write(ev.cmd)
            # Many traces do an extra :SYST:ERR? read between writes; mirror
            # that pattern via the natural test flow rather than reading here.
        elif ev.op == "query":
            response = dev.query(ev.cmd).strip()
            if ev.cmd.upper() == ":READ?":
                _compare_read_response(response, ev.response or "")
            else:
                # Configuration query — must match the captured response.
                # For numerics, allow tolerant compare (firmware can change
                # default precision).
                assert ev.response is not None
                expected = ev.response.strip()
                if response == expected:
                    continue
                # Try numeric compare
                try:
                    rv, ev_v = float(response), float(expected)
                    if math.isclose(rv, ev_v, rel_tol=1e-3, abs_tol=1e-6):
                        continue
                except ValueError:
                    pass
                pytest.fail(
                    f"event {i}: query {ev.cmd!r} drift: now={response!r} "
                    f"captured={expected!r}"
                )

    # Best-effort cleanup between scenarios
    try:
        dev.write(":OUTP OFF")
    except Exception:
        pass
    time.sleep(0.2)

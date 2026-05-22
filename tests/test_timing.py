"""Regression test for the Keithley 2400 timing model.

Validates the analytic estimator against measured GPIB :READ? rates
collected on the lab Keithley 2400 (firmware C30, 60 Hz line) wired in
2-point probe configuration. The full bench is stored at
``docs/keithley_2400_timing_bench.json`` — re-capture with the standalone
script in this repo's history when the model is touched.

The estimator is allowed to under-predict (be conservative) by up to 25%
in any single point; over-prediction must stay within 10%. The former
floor exists because the low-NPLC / high-filter corner is the worst-fit
region and the manual doesn't give per-conversion overhead numbers.
"""
import json
import math
from pathlib import Path

import pytest

from resistamet_gui.timing import estimate_max_sample_rate_hz


BENCH_PATH = Path(__file__).resolve().parent.parent / "docs" / "keithley_2400_timing_bench.json"


def _load_bench():
    if not BENCH_PATH.exists():
        pytest.skip(f"bench data missing: {BENCH_PATH}")
    with open(BENCH_PATH) as f:
        return json.load(f)


def test_estimator_matches_measured_bench_within_tolerance():
    rows = _load_bench()
    assert len(rows) >= 20, "bench appears truncated"
    worst_under = 0.0  # estimator < measured (conservative)
    worst_over = 0.0   # estimator > measured (over-promises)
    for r in rows:
        predicted = estimate_max_sample_rate_hz(
            nplc=r["nplc"],
            auto_zero=r["auto_zero"],
            filter_enabled=True,
            filter_type="repeat",
            filter_count=r["filter_count"],
            line_frequency_hz=60.0,
        )
        measured = r["rate_hz"]
        err = (predicted - measured) / measured  # negative = conservative
        if err < worst_under:
            worst_under = err
        if err > worst_over:
            worst_over = err
    # Conservative side: allow up to 25% under-prediction (low-NPLC corner)
    assert worst_under > -0.25, f"estimator under-predicts by {worst_under*100:.1f}% somewhere"
    # Over-promise side: never more than 10%
    assert worst_over < 0.10, f"estimator over-promises by {worst_over*100:.1f}% somewhere"


@pytest.mark.parametrize("nplc,az,fc,expected_hz", [
    # Headline configurations from the lab bench, accurate to ~5%.
    (1.0, "on",   10, 1.79),
    (1.0, "off",  10, 4.95),
    (1.0, "once",  5, 9.24),
    (0.5, "off",   5, 16.49),
    (0.1, "off",   1, 91.12),
])
def test_estimator_known_points(nplc, az, fc, expected_hz):
    got = estimate_max_sample_rate_hz(
        nplc=nplc, auto_zero=az, filter_enabled=True,
        filter_type="repeat", filter_count=fc,
    )
    # ±15% — generous to allow per-conversion-overhead variation across
    # firmware revisions while still catching gross drift.
    assert abs(got - expected_hz) / expected_hz < 0.15, (
        f"estimator drifted: nplc={nplc} az={az} fc={fc}: "
        f"got {got:.2f} Hz, measured {expected_hz:.2f} Hz"
    )


def test_auto_zero_once_equals_off():
    # The bench confirmed AZ=ONCE behaves identically to AZ=OFF for rate
    # purposes (the cached zero/ref is reused for every reading after the
    # initial calibration). The estimator should treat them the same.
    a = estimate_max_sample_rate_hz(nplc=1.0, auto_zero="once", filter_count=10)
    b = estimate_max_sample_rate_hz(nplc=1.0, auto_zero="off",  filter_count=10)
    assert a == b


def test_auto_zero_on_is_about_3x_slower():
    # Manual §3-10 says AZ=ON performs zero/reference/signal integrations
    # per reading vs just signal for ONCE/OFF — the rate should drop by
    # roughly 3× (slightly less because the per-conversion overhead doesn't
    # scale with auto-zero).
    fast = estimate_max_sample_rate_hz(nplc=1.0, auto_zero="off", filter_count=10)
    slow = estimate_max_sample_rate_hz(nplc=1.0, auto_zero="on",  filter_count=10)
    ratio = fast / slow
    assert 2.5 < ratio < 3.2, f"AZ=ON should be ~3× slower; got {ratio:.2f}×"


def test_disabling_filter_is_equivalent_to_count_one():
    a = estimate_max_sample_rate_hz(filter_enabled=False, filter_count=10)
    b = estimate_max_sample_rate_hz(filter_enabled=True,  filter_count=1)
    assert a == b


def test_offset_comp_halves_max_rate():
    # Offset-compensated ohms doubles per-reading time (paired I-on / I-off
    # sample). The rate cap must shrink accordingly so the UI doesn't
    # silently let users pick a sampling rate the instrument can't deliver
    # when Enhanced accuracy is active.
    base = estimate_max_sample_rate_hz(nplc=1.0, auto_zero='on', filter_count=10)
    enhanced = estimate_max_sample_rate_hz(
        nplc=1.0, auto_zero='on', filter_count=10, offset_comp=True,
    )
    ratio = base / enhanced
    assert math.isclose(ratio, 2.0, rel_tol=1e-9), (
        f"offset_comp should halve the rate; got {ratio:.3f}× speedup"
    )


def test_timing_settings_from_dict_pulls_offset_comp():
    # TimingSettings.from_dict reads res_offset_comp out of the
    # measurement settings — the resistance-mode wiring depends on this.
    from resistamet_gui.timing import TimingSettings
    s_on = TimingSettings.from_dict({'res_offset_comp': True})
    s_off = TimingSettings.from_dict({'res_offset_comp': False})
    assert s_on.offset_comp is True
    assert s_off.offset_comp is False
    assert s_on.max_rate_hz() < s_off.max_rate_hz()

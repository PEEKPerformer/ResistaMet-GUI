"""Keithley 2400-family timing model.

The per-reading time on a 2400-series sourcemeter is dominated by a few
documented behaviours (User's Manual §3-10, §7-7, §7-9). The model below
was validated against a Keithley 2400 (firmware C30) wired in 2-point
probe configuration: across 27 (auto_zero, NPLC, filter_count) combos
the formula matches measured rates within ~5% in the production range,
worst case ~14% **conservative** in the low-NPLC / high-filter corner
(so the helper under-promises, never over-promises). Raw bench data is
checked in at ``docs/keithley_2400_timing_bench.json``.

Why this exists:
  Stored ``sampling_rate`` defaults were "aspirational" — the timer
  would target 10 Hz while the instrument physically delivered 1.8 Hz
  at the configured NPLC/filter/auto-zero. UI now caps the spinbox to
  the achievable rate and surfaces a one-line suggestion when the user
  asks for more than the current configuration allows.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional


# --- timing model ----------------------------------------------------------

# Per the manual, 1 NPLC = 1/line_freq seconds per raw A/D conversion. With
# auto_zero=ON the instrument performs three integrations per returned
# conversion (zero / reference / signal); with ONCE or OFF only the signal
# integration runs and the cached zero/ref are reused.
def _per_conversion_overhead_s(auto_zero: str) -> float:
    """ADC-pipeline / settling overhead per raw conversion, in seconds.

    Empirically ~3 ms for AZ=OFF/ONCE and ~6 ms for AZ=ON (the extra
    integrations have their own settling). Fitted from bench sweep on
    a 2400 over GPIB."""
    return 0.006 if auto_zero.lower() == 'on' else 0.003


# Fixed per-call overhead (pyvisa GPIB round-trip + Python loop). ~6 ms
# observed on the lab rig.
_PER_READING_OVERHEAD_S = 0.006


def estimate_max_sample_rate_hz(
    nplc: float = 1.0,
    auto_zero: str = 'on',
    filter_enabled: bool = True,
    filter_type: str = 'repeat',
    filter_count: int = 10,
    line_frequency_hz: float = 60.0,
) -> float:
    """Predict the maximum sustainable :READ? rate at the given settings.

    Conservative — measured rates are typically 0–14% higher than the
    estimate, never lower, so a UI cap based on this value will not
    over-promise.
    """
    plc_s = 1.0 / line_frequency_hz
    az = auto_zero.lower()
    az_factor = 3.0 if az == 'on' else 1.0
    per_conv = _per_conversion_overhead_s(az)
    if filter_enabled and filter_type.lower() == 'repeat':
        fc = max(1, int(filter_count))
    else:
        fc = 1
    per_reading_s = (az_factor * nplc * plc_s + per_conv) * fc + _PER_READING_OVERHEAD_S
    return 1.0 / per_reading_s


# --- "smart options" suggestions ------------------------------------------

@dataclass(frozen=True)
class TimingSettings:
    """The subset of measurement settings that affect per-reading time."""
    nplc: float
    auto_zero: str
    filter_enabled: bool
    filter_type: str
    filter_count: int

    @classmethod
    def from_dict(cls, m: dict) -> 'TimingSettings':
        return cls(
            nplc=float(m.get('nplc', 1.0)),
            auto_zero=str(m.get('auto_zero', 'on')),
            filter_enabled=bool(m.get('filter_enabled', True)),
            filter_type=str(m.get('filter_type', 'repeat')),
            filter_count=int(m.get('filter_count', 10)),
        )

    def max_rate_hz(self, line_frequency_hz: float = 60.0) -> float:
        return estimate_max_sample_rate_hz(
            nplc=self.nplc, auto_zero=self.auto_zero,
            filter_enabled=self.filter_enabled, filter_type=self.filter_type,
            filter_count=self.filter_count, line_frequency_hz=line_frequency_hz,
        )


def suggest_change_for_rate(
    target_hz: float,
    current: TimingSettings,
    line_frequency_hz: float = 60.0,
) -> Optional[str]:
    """Return a one-line human-readable suggestion that would let the user
    reach ``target_hz`` from ``current``, or None if no single change does.

    Tried in order of decreasing accuracy cost: auto_zero ON→ONCE (cheap,
    minor drift), then filter_count down (linear speedup, noisier reading),
    then NPLC down (less line-noise rejection per conversion).
    """
    def reaches(s: TimingSettings) -> bool:
        return s.max_rate_hz(line_frequency_hz) >= target_hz

    if current.auto_zero.lower() == 'on':
        cand = replace(current, auto_zero='once')
        if reaches(cand):
            return "set auto_zero to 'once' (re-zeros are cached during the run)"

    if current.filter_count > 1:
        for fc in (5, 2, 1):
            if fc < current.filter_count:
                cand = replace(current, filter_count=fc)
                if reaches(cand):
                    if fc == 1:
                        cand2 = replace(current, filter_enabled=False)
                        return "disable the hardware filter (each reading becomes a single conversion)"
                    return f"lower filter_count to {fc} (more jitter per live point, run-level stats unchanged)"

    if current.nplc > 0.1:
        for nplc in (0.5, 0.1):
            if nplc < current.nplc:
                cand = replace(current, nplc=nplc)
                if reaches(cand):
                    return f"lower NPLC to {nplc} (less line-noise rejection per reading)"

    return None

"""Compatibility shim — the fake now lives in ``resistamet_gui._simulator``.

It moved into the main package so it ships with ``pip install`` and powers
the ``--simulate`` runtime flag. Tests can keep importing from this path.
"""
from __future__ import annotations

from resistamet_gui._simulator import *  # noqa: F401,F403
from resistamet_gui._simulator import (  # noqa: F401
    DEFAULT_IDN,
    FakeKeithley,
    FakeResourceManager,
    _STAT_BASELINE,
    _STAT_BIT_COMPLIANCE,
    _idn_for_model,
)

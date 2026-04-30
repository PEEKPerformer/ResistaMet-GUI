"""Hardware-tier test fixtures.

Tests in this directory require a real Keithley 2400-series sourcemeter
and are skipped unless the ``RESISTAMET_HARDWARE_ADDR`` environment
variable is set to a VISA resource address (e.g. ``GPIB0::24::INSTR``).

The default DUT is a 100Ω resistor in 4-wire Kelvin connection. Override
with ``RESISTAMET_DUT_OHMS=<value>`` if a different reference is wired.
"""
from __future__ import annotations

import os

import pytest


_SKIP_REASON = "set RESISTAMET_HARDWARE_ADDR to enable hardware tests"


@pytest.fixture(autouse=True)
def _require_hardware_env_var():
    """Skip hardware-tier tests that don't take ``real_instrument``.

    Belt for the suspenders below: any future hardware test that doesn't
    request the module-scoped ``real_instrument`` fixture still gets
    skipped without needing OS-aware path matching.
    """
    if not os.environ.get("RESISTAMET_HARDWARE_ADDR"):
        pytest.skip(_SKIP_REASON)


@pytest.fixture(scope="module")
def real_instrument():
    """Open a real PyVISA connection. Cleans up on teardown.

    Skips at module scope when the bench env var is unset. The
    ``_require_hardware_env_var`` autouse fixture above does the same
    at function scope, but module-scoped fixtures resolve first, so
    the check has to live here too — otherwise on systems without the
    env var (e.g. CI) we'd hit ``KeyError`` during setup before the
    function-scoped skip can fire.
    """
    import pyvisa

    addr = os.environ.get("RESISTAMET_HARDWARE_ADDR")
    if not addr:
        pytest.skip(_SKIP_REASON)
    rm = pyvisa.ResourceManager()
    dev = rm.open_resource(addr)
    dev.timeout = 10000
    try:
        dev.read_termination = "\n"
        dev.write_termination = "\n"
    except Exception:
        pass
    try:
        yield dev
    finally:
        try:
            dev.write(":OUTP OFF")
        except Exception:
            pass
        dev.close()
        rm.close()


@pytest.fixture(scope="module")
def dut_ohms() -> float:
    return float(os.environ.get("RESISTAMET_DUT_OHMS", "100.0"))

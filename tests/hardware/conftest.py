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


def pytest_collection_modifyitems(config, items):
    """Skip the entire hardware tier when the env var isn't set."""
    if os.environ.get("RESISTAMET_HARDWARE_ADDR"):
        return
    skip_marker = pytest.mark.skip(
        reason="set RESISTAMET_HARDWARE_ADDR to enable hardware tests"
    )
    for item in items:
        # Only skip items in this directory
        if "tests/hardware" in str(item.fspath):
            item.add_marker(skip_marker)


@pytest.fixture(scope="module")
def real_instrument():
    """Open a real PyVISA connection. Cleans up on teardown."""
    import pyvisa

    addr = os.environ["RESISTAMET_HARDWARE_ADDR"]
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

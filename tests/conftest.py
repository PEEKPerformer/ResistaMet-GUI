"""Shared fixtures.

The ``fake_instrument`` fixture monkey-patches ``pyvisa.ResourceManager``
so that any code under test that opens ``GPIB0::24::INSTR`` (or whatever
address it has been configured with) gets a stateful FakeKeithley instead
of a real instrument. Tests can mutate ``fake_instrument.dut_resistance``
or call ``fake_instrument.fail_next_query()`` to exercise edge cases.
"""
from __future__ import annotations

from typing import Iterator

import pyvisa
import pytest

from .fakes.fake_keithley import FakeKeithley, FakeResourceManager


@pytest.fixture
def fake_rm(monkeypatch) -> FakeResourceManager:
    """Replace ``pyvisa.ResourceManager`` with a FakeResourceManager.

    Any code path under test that does ``pyvisa.ResourceManager()`` will
    transparently get the fake. The fake exposes ``opened`` so tests can
    reach into the FakeKeithley instances that were opened.
    """
    rm = FakeResourceManager()

    def _factory(*args, **kwargs):
        return rm

    monkeypatch.setattr(pyvisa, "ResourceManager", _factory)
    return rm


@pytest.fixture
def fake_instrument(fake_rm) -> Iterator[FakeKeithley]:
    """A pre-opened FakeKeithley for tests that drive it directly.

    For tests that want to exercise the real ``Keithley2400.connect()`` path
    while still hitting a fake, depend on ``fake_rm`` instead and let the
    code under test open its own resource.
    """
    dev = fake_rm.open_resource("GPIB0::24::INSTR")
    try:
        yield dev
    finally:
        dev.close()

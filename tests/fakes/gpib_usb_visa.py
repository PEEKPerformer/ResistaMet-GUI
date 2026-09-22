"""pyvisa-py stand-ins and fixtures shared by the GPIB-USB session tests.

Imports pyvisa-py: import this only after the module-level skip for a
pyvisa-py older than 0.8, as ``test_gpib_usb_visa`` and
``test_gpib_usb_visa_intfc`` do.
"""
from typing import List

import pytest
from pyvisa.constants import StatusCode
from pyvisa_py.sessions import OpenError, Session

from resistamet_gui.gpib_usb import boards, transport
from resistamet_gui.gpib_usb.visa_intfc import NiUsbGpibIntfcDispatch
from resistamet_gui.gpib_usb.visa_session import NiUsbGpibDispatch
from tests.fakes.gpib_usb import fake_adapter_info


class Sentinel(Session):
    """Stands in for whatever pyvisa-py had registered for (gpib, INSTR)."""

    calls: List[str] = []

    def __init__(self, resource_manager_session, resource_name, parsed=None, open_timeout=None):
        Sentinel.calls.append(resource_name)
        raise OpenError(StatusCode.error_resource_not_found)

    @staticmethod
    def list_resources() -> List[str]:
        return ['GPIB9::1::INSTR']

    def _get_attribute(self, attribute):
        raise NotImplementedError

    def _set_attribute(self, attribute, state):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


@pytest.fixture
def session_registry():
    """Restore pyvisa-py's session table and both dispatchers' memory after each test.

    ``install()`` puts the INSTR and the INTFC dispatcher in place together,
    so both ``previous`` slots are saved here.
    """
    saved = dict(Session._session_classes)
    saved_previous = NiUsbGpibDispatch.previous
    saved_intfc_previous = NiUsbGpibIntfcDispatch.previous
    Sentinel.calls = []
    yield Session._session_classes
    Session._session_classes.clear()
    Session._session_classes.update(saved)
    NiUsbGpibDispatch.previous = saved_previous
    NiUsbGpibIntfcDispatch.previous = saved_intfc_previous


@pytest.fixture
def enumeration(monkeypatch):
    """A replaceable find_adapters that counts its calls."""
    state = {'adapters': [fake_adapter_info()], 'calls': 0}

    def find_adapters():
        state['calls'] += 1
        return list(state['adapters'])

    monkeypatch.setattr(transport, 'find_adapters', find_adapters)
    return state


@pytest.fixture(autouse=True)
def switch_unset(monkeypatch):
    """The developer's shell must not decide which instructions these tests see."""
    for name in boards.INSTRUCTIONS_ENVS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def ni_instructions(monkeypatch, switch_unset):
    """Switch NI's instructions (0x0b, 0x0e, 0x10) on for boards opened in this test; they are off by default."""
    monkeypatch.setenv(boards.INSTRUCTIONS_ENV, '1')

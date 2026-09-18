"""NI GPIB-USB adapters (GPIB-USB-HS family) from user space, for pyvisa-py.

Written from ``docs/design/ni_usb_gpib_protocol.md`` only; see
``docs/design/ni_usb_gpib_clean_room.md``.

NI stopped shipping a GPIB driver for macOS; the adapter is simple enough to
drive over libusb. ``install()`` registers a pyvisa-py session so
``GPIB0::24::INSTR`` opens through this package when an NI adapter is
plugged in, and through whatever pyvisa-py had before otherwise. Nothing
here imports Qt.

Layers, bottom up: ``protocol`` (bytes, pure), ``transport`` (pyusb),
``controller`` (sequencing over one adapter), ``visa_session`` (pyvisa-py).
"""
import logging

from . import transport as _transport
from .protocol import AdapterNotReady, GpibError, GpibTimeout, NoListener, NoReply, ProtocolError
from .transport import AdapterInfo, TransportError, TransportTimeout, find_adapters

__all__ = [
    'AdapterInfo',
    'AdapterNotReady',
    'GpibError',
    'GpibTimeout',
    'NoListener',
    'NoReply',
    'ProtocolError',
    'TransportError',
    'TransportTimeout',
    'available',
    'find_adapters',
    'install',
]

logger = logging.getLogger(__name__)


def available() -> bool:
    """True when pyusb imports and a libusb backend loads. Never raises."""
    try:
        return _transport.libusb_backend() is not None
    except Exception as exc:  # noqa: BLE001 - a broken USB stack must read as "not available"
        logger.debug('NI GPIB-USB unavailable: %s', exc)
        return False


def install() -> None:
    """Register the pyvisa-py session. Idempotent; a no-op without pyusb/libusb or pyvisa-py."""
    if not available():
        logger.debug('NI GPIB-USB: pyusb or libusb missing; pyvisa-py session not installed')
        return
    try:
        from . import visa_session
    except ImportError as exc:
        logger.debug('NI GPIB-USB: pyvisa-py not importable (%s); session not installed', exc)
        return
    visa_session.install()

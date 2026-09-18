"""The USB layer under the NI GPIB-USB driver, and nothing else.

Everything above this module speaks in whole protocol messages; this module
moves those messages over pyusb and knows which adapters exist. It has no
opinion about what the bytes mean, so it can be replaced by a scripted fake
in tests and the protocol logic above it never notices.

pyusb is imported lazily: the package must import on machines without it,
and ``available()`` must be able to say "no" rather than crash. libusb itself
is looked for beside the frozen executable first (the packaged app ships it)
and through pyusb's own discovery second.

Handles: pyusb opens a device the first time anything talks to it (including
a string-descriptor read) and never closes it on its own; only
``usb.util.dispose_resources`` does. On macOS an open handle is exclusive, so
every device this module touches during enumeration is disposed before the
function returns, and ``dispose_adapter`` exists for the registry to call
when it drops an ``AdapterInfo``.
"""
import glob
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Protocol, TypeVar

from . import tables as t
from .protocol import AdapterNotReady

logger = logging.getLogger(__name__)

#: §2.1: the protocol lives on interface 0 on every model (the HS+ has a second
#: interface for its bus analyser, which is left alone).
INTERFACE_NUMBER = 0
#: Used when the endpoint descriptor cannot be read; every model here is a
#: high-speed device, so 512 is the bulk maximum.
DEFAULT_MAX_PACKET_SIZE = 512

T = TypeVar('T')


class TransportError(Exception):
    """The USB layer failed: device gone, access denied, pipe error."""


class TransportTimeout(TransportError):
    """No reply within the host wait; the device may still owe one (§5.11)."""


class Transport(Protocol):
    """What the controller needs from a USB connection to one adapter.

    The specification has no host-to-device control requests, so there is no
    control-out operation.
    """

    #: wMaxPacketSize of the bulk IN endpoint, for sizing read buffers (§8.6).
    max_packet_size: int

    def control_in(self, request: int, value: int, index: int, length: int,
                   timeout_ms: int,
                   request_type: int = t.REQUEST_TYPE_VENDOR_DEVICE) -> bytes: ...

    def bulk_out(self, data: bytes, timeout_ms: int) -> None: ...

    def bulk_in(self, length: int, timeout_ms: int) -> bytes: ...

    def close(self) -> None: ...


@dataclass
class AdapterInfo:
    """One enumerated adapter: identity, endpoints, and how to open it."""

    model: str
    vendor_id: int
    product_id: int
    bus: int
    address: int
    serial: Optional[str]
    endpoint_out: int
    endpoint_in: int
    endpoint_interrupt: int
    needs_firmware: bool
    #: The pyusb device; None for fakes, which supply their own transport.
    device: Any = None

    @property
    def label(self) -> str:
        serial = ' serial %s' % self.serial if self.serial else ''
        return '%s (bus %d address %d%s)' % (self.model, self.bus, self.address, serial)


def _import_usb() -> Any:
    """The ``usb`` package, or None when pyusb is not installed."""
    try:
        import usb.backend.libusb1  # noqa: F401
        import usb.core  # noqa: F401
        import usb.util  # noqa: F401
    except ImportError:
        return None
    return usb


def _bundled_libusb() -> Optional[str]:
    """A libusb-1.0 shared library shipped with the frozen app, if any.

    PyInstaller 6 unpacks bundled libraries into ``sys._MEIPASS``; older
    layouts put them beside the executable. Both are checked.
    """
    directories: List[str] = []
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        directories.append(getattr(sys, '_MEIPASS'))
    directories.append(os.path.dirname(os.path.abspath(sys.executable)))
    for directory in directories:
        for pattern in ('libusb-1.0*.dylib', 'libusb-1.0*.so*', 'libusb-1.0.dll'):
            hits = sorted(glob.glob(os.path.join(directory, pattern)))
            if hits:
                return hits[0]
    return None


def libusb_library_path() -> Optional[str]:
    """The libusb the frozen app ships, if that is the one in use.

    For diagnostics: ``None`` means pyusb's own discovery is being used (a
    system libusb), which is the normal source checkout case.
    """
    return _bundled_libusb()


def libusb_backend() -> Any:
    """A pyusb libusb-1.0 backend, or None when no libusb can be loaded."""
    usb = _import_usb()
    if usb is None:
        return None
    from usb.backend import libusb1
    bundled = _bundled_libusb()
    if bundled:
        backend = libusb1.get_backend(find_library=lambda name: bundled)
        if backend is not None:
            return backend
        logger.debug('bundled libusb at %s did not load; trying the system one', bundled)
    return libusb1.get_backend()


def find_adapters() -> List[AdapterInfo]:
    """Supported NI adapters on the USB buses, in (bus, address) order.

    Empty when pyusb or libusb is unavailable. The USB-B before its firmware
    upload is listed with ``needs_firmware`` set so the UI can name it, but
    ``open_transport`` refuses it. Every device touched here is disposed
    again before returning; the returned handles are closed until used.
    """
    usb = _import_usb()
    backend = libusb_backend()
    if usb is None or backend is None:
        return []
    found: List[AdapterInfo] = []
    try:
        devices = usb.core.find(find_all=True, backend=backend, idVendor=t.VENDOR_ID)
        for device in devices:
            model = t.MODELS.get(device.idProduct)
            if model is None:
                continue
            found.append(AdapterInfo(
                model=model.name,
                vendor_id=t.VENDOR_ID,
                product_id=model.product_id,
                bus=device.bus or 0,
                address=device.address or 0,
                serial=_serial_of(usb, device),
                endpoint_out=model.endpoint_out,
                endpoint_in=model.endpoint_in,
                endpoint_interrupt=model.endpoint_interrupt,
                needs_firmware=model.needs_firmware,
                device=device,
            ))
    except usb.core.USBError as exc:
        logger.debug('USB enumeration failed: %s', exc)
    found.sort(key=lambda info: (info.bus, info.address))
    return found


def _serial_of(usb: Any, device: Any) -> Optional[str]:
    """The USB serial string, or None. Closes the handle the read opened."""
    # A string-descriptor read needs the device opened; without permission it
    # fails in several ways (USBError, ValueError for a missing langid). The
    # serial is a convenience, so any failure just means "unknown".
    try:
        return device.serial_number
    except Exception as exc:  # noqa: BLE001 - see comment above
        logger.debug('serial number unavailable: %s', exc)
        return None
    finally:
        _dispose(usb, device)


def dispose_adapter(info: AdapterInfo) -> None:
    """Release whatever libusb handle pyusb holds on ``info.device``."""
    usb = _import_usb()
    if usb is not None and info.device is not None:
        _dispose(usb, info.device)


def _dispose(usb: Any, device: Any) -> None:
    try:
        usb.util.dispose_resources(device)
    except usb.core.USBError as exc:
        logger.debug('dispose_resources: %s', exc)


def open_transport(info: AdapterInfo) -> Transport:
    """A claimed USB connection to ``info``."""
    if info.needs_firmware:
        raise AdapterNotReady('%s enumerates as 0x%04x and needs its firmware uploaded '
                              'before it can be driven' % (info.label, info.product_id))
    if info.device is None:
        raise TransportError('%s has no USB device handle' % info.label)
    return PyUsbTransport(info.device, info.endpoint_out, info.endpoint_in)


class PyUsbTransport:
    """``Transport`` over a pyusb device (§2.1)."""

    def __init__(self, device: Any, endpoint_out: int, endpoint_in: int,
                 interface: int = INTERFACE_NUMBER) -> None:
        usb = _import_usb()
        if usb is None:
            raise TransportError('pyusb is not installed')
        self._usb = usb
        self._device = device
        self._out = endpoint_out
        self._in = endpoint_in
        self._interface = interface
        self.max_packet_size = DEFAULT_MAX_PACKET_SIZE
        try:
            self._configure()
            self._detach_kernel_driver()
            usb.util.claim_interface(device, interface)
        except usb.core.USBError as exc:
            _dispose(usb, device)
            raise TransportError('cannot claim interface %d: %s' % (interface, exc)) from exc
        self.max_packet_size = self._in_packet_size()

    def _configure(self) -> None:
        # §2.1 step 2: normally already configured; a failure here is harmless.
        try:
            self._device.get_active_configuration()
        except self._usb.core.USBError:
            try:
                self._device.set_configuration()
            except self._usb.core.USBError as exc:
                logger.debug('set_configuration failed (harmless): %s', exc)

    def _detach_kernel_driver(self) -> None:
        # Only Linux has a kernel driver to detach; other backends raise
        # NotImplementedError, which is the expected answer there.
        try:
            if self._device.is_kernel_driver_active(self._interface):
                self._device.detach_kernel_driver(self._interface)
        except (NotImplementedError, self._usb.core.USBError) as exc:
            logger.debug('kernel driver detach skipped: %s', exc)

    def _in_packet_size(self) -> int:
        try:
            interface = self._device.get_active_configuration()[(self._interface, 0)]
            endpoint = self._usb.util.find_descriptor(interface, bEndpointAddress=self._in)
        except (self._usb.core.USBError, KeyError, IndexError):
            return DEFAULT_MAX_PACKET_SIZE
        if endpoint is None:
            return DEFAULT_MAX_PACKET_SIZE
        return int(endpoint.wMaxPacketSize) or DEFAULT_MAX_PACKET_SIZE

    def _run(self, what: str, call: Callable[[], T]) -> T:
        try:
            return call()
        except self._usb.core.USBTimeoutError as exc:
            raise TransportTimeout('%s timed out' % what) from exc
        except self._usb.core.USBError as exc:
            raise TransportError('%s failed: %s' % (what, exc)) from exc

    def control_in(self, request: int, value: int, index: int, length: int,
                   timeout_ms: int,
                   request_type: int = t.REQUEST_TYPE_VENDOR_DEVICE) -> bytes:
        reply = self._run('control request 0x%02x' % request, lambda: self._device.ctrl_transfer(
            request_type, request, value, index, length, timeout_ms))
        return bytes(reply)

    def bulk_out(self, data: bytes, timeout_ms: int) -> None:
        written = self._run('bulk write', lambda: self._device.write(self._out, data, timeout_ms))
        if written != len(data):
            raise TransportError('bulk write sent %d of %d bytes' % (written, len(data)))

    def bulk_in(self, length: int, timeout_ms: int) -> bytes:
        reply = self._run('bulk read', lambda: self._device.read(self._in, length, timeout_ms))
        return bytes(reply)

    def close(self) -> None:
        try:
            self._usb.util.release_interface(self._device, self._interface)
        except self._usb.core.USBError as exc:
            logger.debug('release_interface: %s', exc)
        _dispose(self._usb, self._device)

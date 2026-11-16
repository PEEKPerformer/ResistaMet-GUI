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
import errno
import glob
import logging
import os
import sys
import time
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
    """The USB layer failed: device gone, access denied, pipe error.

    ``errno`` and ``backend_code`` are those of the pyusb ``USBError``
    underneath, None when there was none; they are what tells one failure
    from another in a log (``backend_code`` is libusb's, -9 for a pipe error).
    """

    errno: Optional[int] = None
    backend_code: Optional[int] = None


class TransportTimeout(TransportError):
    """No reply within the host wait; the device may still owe one (§5.11).

    ``partial`` holds whatever a raw bulk IN had received before its wait
    expired (see ``Transport.bulk_in_raw``); empty for every other transfer.
    """

    def __init__(self, message: str, partial: bytes = b'') -> None:
        super().__init__(message)
        self.partial = partial


class TransportStall(TransportError):
    """The endpoint answered with a STALL handshake and is now halted.

    On the alternate bulk OUT this is how the adapter refuses the data of a
    0x0e it cannot start (§10.6.5); the reply still arrives on the primary
    bulk IN, and the endpoint stays halted until ``Transport.clear_halt``.
    """


class TransportGone(TransportError):
    """The adapter has left the USB bus: unplugged, or its power lost.

    Nothing sent on the handle can arrive any more, so there is nothing to
    recover: every pipe reset, stop request and drain fails the same way
    (spec §11.2, "Hot-unplug mid-run"). A replugged adapter comes back as a
    new USB device, reached through a fresh enumeration, not through this
    handle. Raised for libusb's "no such device"; on macOS, where an open
    handle never reports that (§10.11), the controller raises it when
    ``device_present`` no longer finds the adapter after another error.
    """


#: libusb's code for a pipe error, which is how it reports a STALL. The
#: installed pyusb libusb-1.0 backend raises every negative libusb return but
#: a timeout as ``USBError(strerror, ret, _libusb_errno[ret])`` (its
#: ``_check``), so a pipe error arrives with ``backend_error_code`` -9 and
#: ``errno`` EPIPE, both filled.
LIBUSB_ERROR_PIPE = -9
#: libusb's code for a device that is no longer there. The same ``_check``
#: raises it as ``USBError('No such device (it may have been disconnected)',
#: -4, ENODEV)``; pyusb's libusb-0.1 backend passes the negative errno itself
#: as the backend code (-ENODEV) and no errno.
LIBUSB_ERROR_NO_DEVICE = -4


class TransportAccessDenied(TransportError):
    """The operating system refused this process access to the adapter's USB device.

    On Linux that is the permission on its device node, which a udev rule
    grants (package README, "Linux"; §10.11: root:root 0664 without the
    rule, and with it the unprivileged user opened the adapter). The
    message names the node and the rule.
    """


#: libusb's code for access denied. The libusb-1.0 backend raises it as
#: ``USBError('Access denied (insufficient permissions)', -3, EACCES)``; the
#: libusb-0.1 backend passes -EACCES as the backend code and no errno.
LIBUSB_ERROR_ACCESS = -3


def _is_stall(exc: Exception) -> bool:
    return (getattr(exc, 'backend_error_code', None) == LIBUSB_ERROR_PIPE
            or getattr(exc, 'errno', None) == errno.EPIPE)


def _is_gone(exc: Exception) -> bool:
    return (getattr(exc, 'errno', None) == errno.ENODEV
            or getattr(exc, 'backend_error_code', None) in (LIBUSB_ERROR_NO_DEVICE, -errno.ENODEV))


def _is_access_denied(exc: Exception) -> bool:
    return (getattr(exc, 'errno', None) == errno.EACCES
            or getattr(exc, 'backend_error_code', None) in (LIBUSB_ERROR_ACCESS, -errno.EACCES))


def _access_denied_message(device: Any, interface: int, exc: Exception) -> str:
    """What to tell the user when the claim is refused: the cause, and on Linux the remedy."""
    bus, address = getattr(device, 'bus', None), getattr(device, 'address', None)
    if sys.platform.startswith('linux'):
        node = ('/dev/bus/usb/%03d/%03d' % (bus, address)) if bus is not None and address is not None \
            else 'under /dev/bus/usb'
        return ('cannot open the adapter: no permission on its USB device node %s (%s). Install the '
                'udev rule in resistamet_gui/gpib_usb/README.md ("Linux"), reload the rules, replug '
                'the adapter, and make sure your user is in the group the rule names' % (node, exc))
    return ('cannot claim interface %d: the operating system denied access to the adapter (%s); '
            'another program may have it open' % (interface, exc))


def _carrying_codes(error: TransportError, exc: Exception) -> TransportError:
    """``error`` with the errno and backend code of the pyusb exception it stands for."""
    error.errno = getattr(exc, 'errno', None)
    error.backend_code = getattr(exc, 'backend_error_code', None)
    return error


class Transport(Protocol):
    """What the controller needs from a USB connection to one adapter.

    ``bulk_out`` / ``bulk_in`` are the primary pair that carries messages and
    replies (§3.1). ``bulk_out_raw`` / ``bulk_in_raw`` are the alternate pair
    that carries the bytes of 0x0e writes and 0x0b reads unframed (§10.1.3,
    §10.5.2); a transport for a model without the pair raises
    ``TransportError`` from them. ``interrupt_in`` receives the SRQ push
    (§10.4.2) and ``control_out`` sends the one host-to-device request that
    follows it (§2.2).

    Short raw transfers: pyusb reports a bulk transfer whose wait expired
    after some bytes moved as the partial count, not as a timeout. On the
    raw pair that matters, because the device paces both transfers by the
    instrument's handshake. ``bulk_out_raw`` therefore returns the count
    accepted (short = the wait expired) and ``bulk_in_raw`` raises
    ``TransportTimeout`` carrying the partial bytes when a short transfer
    took the whole wait; a short transfer that came back sooner is the
    device's short packet, i.e. the end of the data. The primary pair
    carries fixed-shape messages, where short means a fault.

    A STALL on any endpoint raises ``TransportStall``. ``clear_halt`` takes
    the endpoint address (``tables.Model`` has them) and resets that pipe,
    which is what NI's driver does after a refused 0x0e (§10.6.5). A device
    that is no longer on the bus raises ``TransportGone`` from any call
    where libusb says so, which on macOS it does not (§10.11): there the
    open handle of an unplugged adapter fails with an I/O error and then
    "Other error". ``device_present() -> Optional[bool]`` answers from an
    enumeration whether the adapter is still there, None when it cannot
    tell; it is optional (``PyUsbTransport`` has it), and a transport
    without it counts as one that cannot tell.
    """

    #: wMaxPacketSize of the primary bulk IN endpoint, for sizing read buffers (§8.6).
    max_packet_size: int
    #: wMaxPacketSize of the alternate bulk IN endpoint, for the 0x0b data buffer.
    max_packet_size_raw: int

    def control_in(self, request: int, value: int, index: int, length: int,
                   timeout_ms: int,
                   request_type: int = t.REQUEST_TYPE_VENDOR_DEVICE) -> bytes: ...

    def control_out(self, request: int, value: int, index: int, data: bytes,
                    timeout_ms: int,
                    request_type: int = t.REQUEST_TYPE_VENDOR_DEVICE_OUT) -> None: ...

    def bulk_out(self, data: bytes, timeout_ms: int) -> None: ...

    def bulk_in(self, length: int, timeout_ms: int) -> bytes: ...

    def bulk_out_raw(self, data: bytes, timeout_ms: int) -> int: ...

    def bulk_in_raw(self, length: int, timeout_ms: int) -> bytes: ...

    def interrupt_in(self, length: int, timeout_ms: int) -> bytes: ...

    def clear_halt(self, endpoint: int) -> None: ...

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
    #: The alternate bulk pair for raw data (§1.2, §10); None on models without one.
    endpoint_out_raw: Optional[int] = None
    endpoint_in_raw: Optional[int] = None

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
                endpoint_out_raw=model.endpoint_out_raw,
                endpoint_in_raw=model.endpoint_in_raw,
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
    return PyUsbTransport(info.device, info.endpoint_out, info.endpoint_in,
                          endpoint_out_raw=info.endpoint_out_raw, endpoint_in_raw=info.endpoint_in_raw,
                          endpoint_interrupt=info.endpoint_interrupt)


class PyUsbTransport:
    """``Transport`` over a pyusb device (§2.1)."""

    def __init__(self, device: Any, endpoint_out: int, endpoint_in: int,
                 interface: int = INTERFACE_NUMBER, *,
                 endpoint_out_raw: Optional[int] = None, endpoint_in_raw: Optional[int] = None,
                 endpoint_interrupt: Optional[int] = None) -> None:
        usb = _import_usb()
        if usb is None:
            raise TransportError('pyusb is not installed')
        self._usb = usb
        self._device = device
        self._out = endpoint_out
        self._in = endpoint_in
        self._out_raw = endpoint_out_raw
        self._in_raw = endpoint_in_raw
        self._interrupt = endpoint_interrupt
        self._interface = interface
        self.max_packet_size = DEFAULT_MAX_PACKET_SIZE
        self.max_packet_size_raw = DEFAULT_MAX_PACKET_SIZE
        try:
            self._configure()
            self._detach_kernel_driver()
            usb.util.claim_interface(device, interface)
        except usb.core.USBError as exc:
            _dispose(usb, device)
            if _is_access_denied(exc):
                error: TransportError = TransportAccessDenied(_access_denied_message(device, interface, exc))
            else:
                kind = TransportGone if _is_gone(exc) else TransportError
                error = kind('cannot claim interface %d: %s' % (interface, exc))
            raise _carrying_codes(error, exc) from exc
        self.max_packet_size = self._in_packet_size(self._in)
        if self._in_raw is not None:
            self.max_packet_size_raw = self._in_packet_size(self._in_raw)

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

    def _in_packet_size(self, address: int) -> int:
        try:
            interface = self._device.get_active_configuration()[(self._interface, 0)]
            endpoint = self._usb.util.find_descriptor(interface, bEndpointAddress=address)
        except (self._usb.core.USBError, KeyError, IndexError):
            return DEFAULT_MAX_PACKET_SIZE
        if endpoint is None:
            return DEFAULT_MAX_PACKET_SIZE
        return int(endpoint.wMaxPacketSize) or DEFAULT_MAX_PACKET_SIZE

    def _run(self, what: str, call: Callable[[], T]) -> T:
        try:
            return call()
        except self._usb.core.USBTimeoutError as exc:
            raise _carrying_codes(TransportTimeout('%s timed out' % what), exc) from exc
        except self._usb.core.USBError as exc:
            if _is_gone(exc):
                raise _carrying_codes(TransportGone('%s failed: the device is no longer on the USB bus (%s)'
                                                    % (what, exc)), exc) from exc
            if _is_stall(exc):
                raise _carrying_codes(TransportStall('%s was refused with a STALL' % what), exc) from exc
            raise _carrying_codes(TransportError('%s failed: %s' % (what, exc)), exc) from exc

    def control_in(self, request: int, value: int, index: int, length: int,
                   timeout_ms: int,
                   request_type: int = t.REQUEST_TYPE_VENDOR_DEVICE) -> bytes:
        reply = self._run('control request 0x%02x' % request, lambda: self._device.ctrl_transfer(
            request_type, request, value, index, length, timeout_ms))
        return bytes(reply)

    def control_out(self, request: int, value: int, index: int, data: bytes,
                    timeout_ms: int,
                    request_type: int = t.REQUEST_TYPE_VENDOR_DEVICE_OUT) -> None:
        self._run('control request 0x%02x' % request, lambda: self._device.ctrl_transfer(
            request_type, request, value, index, data, timeout_ms))

    def bulk_out(self, data: bytes, timeout_ms: int) -> None:
        self._write(self._out, 'bulk write', data, timeout_ms)

    def bulk_in(self, length: int, timeout_ms: int) -> bytes:
        reply = self._run('bulk read', lambda: self._device.read(self._in, length, timeout_ms))
        return bytes(reply)

    def bulk_out_raw(self, data: bytes, timeout_ms: int) -> int:
        """Bytes the device accepted on the alternate OUT; fewer than sent means the wait expired.

        pyusb reports a timeout after some bytes moved as the partial count,
        not an error (its ``__write``: LIBUSB_ERROR_TIMEOUT with a nonzero
        count is returned as the count). The device paces this transfer by
        the instrument's handshake, so a short count is the timeout case
        and the caller treats it as one.
        """
        if self._out_raw is None:
            raise TransportError('this adapter has no alternate bulk OUT endpoint')
        endpoint = self._out_raw
        return int(self._run('raw bulk write', lambda: self._device.write(endpoint, data, timeout_ms)))

    def bulk_in_raw(self, length: int, timeout_ms: int) -> bytes:
        """The next transfer on the alternate IN; a short one that used the whole wait is a timeout.

        pyusb returns partial data as success when the wait expires after
        some bytes arrived (its ``__read``), which is indistinguishable by
        length from the device's terminating short packet. Elapsed time
        tells them apart: a short packet completes the read the moment it
        arrives, a timeout only at the deadline.
        """
        if self._in_raw is None:
            raise TransportError('this adapter has no alternate bulk IN endpoint')
        endpoint = self._in_raw
        started = time.monotonic()
        reply = bytes(self._run('raw bulk read', lambda: self._device.read(endpoint, length, timeout_ms)))
        if len(reply) < length and (time.monotonic() - started) * 1000 >= timeout_ms:
            raise TransportTimeout('raw bulk read timed out after %d of %d bytes' % (len(reply), length), reply)
        return reply

    def interrupt_in(self, length: int, timeout_ms: int) -> bytes:
        if self._interrupt is None:
            raise TransportError('this adapter has no interrupt endpoint')
        endpoint = self._interrupt
        reply = self._run('interrupt read', lambda: self._device.read(endpoint, length, timeout_ms))
        return bytes(reply)

    def clear_halt(self, endpoint: int) -> None:
        """Reset a halted pipe: CLEAR_FEATURE(ENDPOINT_HALT) and the host's data toggle."""
        self._run('clear halt on endpoint 0x%02x' % endpoint, lambda: self._device.clear_halt(endpoint))

    def device_present(self) -> Optional[bool]:
        """Whether this adapter is still on the USB bus, from an enumeration; None if it cannot tell.

        On macOS the open handle of an unplugged adapter never says "no
        such device": the transfer in flight fails with errno 5 and every
        later request with libusb's "Other error", and errno 19 comes only
        when the device is opened again (§10.11). The controller asks here
        instead, after an error that does not say, and more than once: on
        the bench libusb still listed the unplugged adapter for about 10 ms
        after the first error (``link.AdapterLink.gone_instead``).

        Present means a device with this one's vendor and product id at its
        bus and address. pyusb's ``find`` reads each device's descriptor,
        bus and address without opening it; the serial number is not
        compared, because reading it opens the device, and a replugged
        adapter comes back as a new device at a new address, which this
        handle does not reach anyway. Enumeration that fails, or a device
        whose location is unknown, gives None.
        """
        vendor = getattr(self._device, 'idVendor', None)
        product = getattr(self._device, 'idProduct', None)
        bus = getattr(self._device, 'bus', None)
        address = getattr(self._device, 'address', None)
        if None in (vendor, product, bus, address):
            return None
        try:
            backend = libusb_backend()
            if backend is None:
                return None
            found = self._usb.core.find(find_all=True, backend=backend, idVendor=vendor, idProduct=product,
                                        bus=bus, address=address)
            return next(iter(found), None) is not None
        except Exception as exc:  # noqa: BLE001 - an enumeration that fails cannot tell
            logger.debug('USB enumeration for the presence check failed: %s', exc)
            return None

    def _write(self, endpoint: int, what: str, data: bytes, timeout_ms: int) -> None:
        """All of ``data`` or ``TransportTimeout``: pyusb returns a short count only when the wait
        ran out after some packets went (its ``__write``), which is how a hung adapter takes the
        first packets of a message and NAKs the rest (§8.17)."""
        written = self._run(what, lambda: self._device.write(endpoint, data, timeout_ms))
        if written != len(data):
            raise TransportTimeout('%s timed out after %d of %d bytes' % (what, written, len(data)))

    def close(self) -> None:
        try:
            self._usb.util.release_interface(self._device, self._interface)
        except self._usb.core.USBError as exc:
            logger.debug('release_interface: %s', exc)
        _dispose(self._usb, self._device)

"""The pyusb layer against a fake ``usb`` package injected into ``sys.modules``.

pyusb opens a device the first time anything talks to it and only
``dispose_resources`` closes it again; on macOS an open handle locks other
processes out of the adapter. These tests pin down that every device touched
during enumeration is disposed, that the interface is claimed and released,
and that USB errors become the transport's own exceptions.
"""
import array
import errno
import sys
import types
from typing import Any, Dict, List, Optional

import pytest

import resistamet_gui.gpib_usb as gpib_usb
from resistamet_gui.gpib_usb import tables as t
from resistamet_gui.gpib_usb import transport
from resistamet_gui.gpib_usb.transport import (AdapterInfo, PyUsbTransport, TransportError, TransportGone,
                                                TransportStall, TransportTimeout)


class FakeEndpoint:
    def __init__(self, address: int, packet: int) -> None:
        self.bEndpointAddress = address
        self.wMaxPacketSize = packet


class FakeInterface:
    def __init__(self, endpoints: List[FakeEndpoint]) -> None:
        self.endpoints = endpoints


class FakeConfiguration:
    def __init__(self, interface: FakeInterface) -> None:
        self._interface = interface

    def __getitem__(self, key):
        if key != (0, 0):
            raise KeyError(key)
        return self._interface


class FakeDevice:
    def __init__(self, idVendor: int, idProduct: int, bus: int = 1, address: int = 1,
                 serial: Optional[str] = 'SN', serial_error: Optional[Exception] = None,
                 configured: bool = True, claim_error: Optional[Exception] = None) -> None:
        self.idVendor = idVendor
        self.idProduct = idProduct
        self.bus = bus
        self.address = address
        self._serial = serial
        self._serial_error = serial_error
        self.configured = configured
        self.claim_error = claim_error
        self.serial_reads = 0
        self.set_configuration_calls = 0
        self.ctrl_calls: List[tuple] = []
        self.writes: List[tuple] = []
        self.reads: List[tuple] = []
        self.next_read: Any = b''
        self.ctrl_reply: bytes = b'\x40' + bytes(15)
        self.write_returns: Optional[int] = None
        self.write_error: Optional[Exception] = None
        self.halts_cleared: List[int] = []
        self.clear_halt_error: Optional[Exception] = None
        self.raw_packet = 512

    @property
    def serial_number(self):
        self.serial_reads += 1
        if self._serial_error is not None:
            raise self._serial_error
        return self._serial

    def get_active_configuration(self):
        if not self.configured:
            raise FAKE['core'].USBError('Configuration not set')
        return FakeConfiguration(FakeInterface([FakeEndpoint(0x02, 512), FakeEndpoint(0x84, 512),
                                                FakeEndpoint(0x06, 512), FakeEndpoint(0x88, self.raw_packet),
                                                FakeEndpoint(0x81, 64)]))

    def set_configuration(self):
        self.set_configuration_calls += 1
        self.configured = True

    def is_kernel_driver_active(self, interface):
        raise NotImplementedError('not on this platform')

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex, data_or_wLength, timeout):
        self.ctrl_calls.append((bmRequestType, bRequest, wValue, wIndex, data_or_wLength, timeout))
        if bmRequestType & 0x80 == 0:
            return len(data_or_wLength)  # host-to-device: pyusb returns the bytes written
        return array.array('B', self.ctrl_reply[:data_or_wLength])

    def write(self, endpoint, data, timeout):
        self.writes.append((endpoint, bytes(data), timeout))
        if self.write_error is not None:
            raise self.write_error
        return len(data) if self.write_returns is None else self.write_returns

    def clear_halt(self, endpoint):
        self.halts_cleared.append(endpoint)
        if self.clear_halt_error is not None:
            raise self.clear_halt_error

    def read(self, endpoint, length, timeout):
        self.reads.append((endpoint, length, timeout))
        if isinstance(self.next_read, Exception):
            raise self.next_read
        return array.array('B', self.next_read)


FAKE: Dict[str, Any] = {}


def install_fake_usb(monkeypatch, devices: List[FakeDevice], backend: Any = 'backend') -> Dict[str, Any]:
    """A minimal ``usb`` package: core.find, the error classes, util helpers, libusb1.get_backend."""
    usb = types.ModuleType('usb')
    core = types.ModuleType('usb.core')
    util = types.ModuleType('usb.util')
    backend_pkg = types.ModuleType('usb.backend')
    libusb1 = types.ModuleType('usb.backend.libusb1')

    class USBError(IOError):
        """pyusb's signature: the backend's code in ``backend_error_code``, ``errno`` from IOError."""

        def __init__(self, strerror, error_code=None, errno=None):
            IOError.__init__(self, errno, strerror)
            self.backend_error_code = error_code

    class USBTimeoutError(USBError):
        pass

    calls: Dict[str, List[Any]] = {'find': [], 'claim': [], 'release': [], 'dispose': [], 'get_backend': []}

    def find(find_all=False, backend=None, custom_match=None, **fields):
        calls['find'].append((backend, fields))
        matched = [d for d in devices if all(getattr(d, k) == v for k, v in fields.items())]
        return iter(matched) if find_all else (matched[0] if matched else None)

    def claim_interface(device, interface):
        if device.claim_error is not None:
            raise device.claim_error
        calls['claim'].append((device, interface))

    def find_descriptor(desc, find_all=False, custom_match=None, **fields):
        for endpoint in desc.endpoints:
            if all(getattr(endpoint, k) == v for k, v in fields.items()):
                return endpoint
        return None

    def get_backend(find_library=None):
        calls['get_backend'].append(find_library)
        return backend

    core.USBError = USBError
    core.USBTimeoutError = USBTimeoutError
    core.find = find
    util.claim_interface = claim_interface
    util.release_interface = lambda device, interface: calls['release'].append((device, interface))
    util.dispose_resources = lambda device: calls['dispose'].append(device)
    util.find_descriptor = find_descriptor
    libusb1.get_backend = get_backend
    usb.core = core
    usb.util = util
    usb.backend = backend_pkg
    backend_pkg.libusb1 = libusb1
    for name, module in (('usb', usb), ('usb.core', core), ('usb.util', util),
                         ('usb.backend', backend_pkg), ('usb.backend.libusb1', libusb1)):
        monkeypatch.setitem(sys.modules, name, module)
    FAKE.clear()
    FAKE.update({'core': core, 'util': util, 'calls': calls})
    return FAKE


HS = lambda **kw: FakeDevice(t.VENDOR_ID, t.PID_HS, **kw)  # noqa: E731


class TestFindAdapters:
    def test_filters_by_vendor_and_known_product(self, monkeypatch):
        hs = HS(bus=2, address=7, serial='HS1')
        usb_b = FakeDevice(t.VENDOR_ID, t.PID_USB_B_PRE_FIRMWARE, bus=1, address=3, serial='B1')
        unknown_ni = FakeDevice(t.VENDOR_ID, 0x1111, bus=1, address=4)
        other_vendor = FakeDevice(0x1234, t.PID_HS, bus=1, address=1)
        fake = install_fake_usb(monkeypatch, [hs, usb_b, unknown_ni, other_vendor])

        adapters = transport.find_adapters()

        assert [(a.model, a.bus, a.address, a.serial) for a in adapters] == [
            ('GPIB-USB-B (no firmware)', 1, 3, 'B1'), ('GPIB-USB-HS', 2, 7, 'HS1')]
        assert adapters[0].needs_firmware and not adapters[1].needs_firmware
        assert adapters[1].endpoint_out == 0x02 and adapters[1].endpoint_in == 0x84
        assert adapters[1].endpoint_out_raw == 0x06 and adapters[1].endpoint_in_raw == 0x88
        assert adapters[1].endpoint_interrupt == 0x81
        assert adapters[0].endpoint_out_raw is None and adapters[0].endpoint_in_raw is None
        assert adapters[1].device is hs
        assert fake['calls']['find'] == [('backend', {'idVendor': t.VENDOR_ID})]

    def test_every_device_whose_serial_was_read_is_disposed(self, monkeypatch):
        hs = HS(serial='HS1')
        usb_b = FakeDevice(t.VENDOR_ID, t.PID_USB_B_PRE_FIRMWARE, address=3)
        unknown_ni = FakeDevice(t.VENDOR_ID, 0x1111, address=4)
        fake = install_fake_usb(monkeypatch, [hs, usb_b, unknown_ni])

        transport.find_adapters()

        assert hs.serial_reads == 1 and usb_b.serial_reads == 1 and unknown_ni.serial_reads == 0
        assert fake['calls']['dispose'] == [hs, usb_b]

    def test_serial_failure_is_unknown_and_still_disposed(self, monkeypatch):
        hs = HS(serial_error=ValueError('no langid'))
        fake = install_fake_usb(monkeypatch, [hs])
        adapters = transport.find_adapters()
        assert adapters[0].serial is None
        assert fake['calls']['dispose'] == [hs]

    def test_enumeration_error_yields_nothing(self, monkeypatch):
        fake = install_fake_usb(monkeypatch, [])

        def find(**kwargs):
            raise fake['core'].USBError('access denied')
        fake['core'].find = find
        assert transport.find_adapters() == []

    def test_empty_without_a_backend(self, monkeypatch):
        install_fake_usb(monkeypatch, [HS()], backend=None)
        assert transport.find_adapters() == []
        assert transport.libusb_backend() is None
        assert gpib_usb.available() is False

    def test_available_with_a_backend(self, monkeypatch):
        install_fake_usb(monkeypatch, [])
        assert gpib_usb.available() is True

    def test_dispose_adapter(self, monkeypatch):
        hs = HS()
        fake = install_fake_usb(monkeypatch, [hs])
        info = transport.find_adapters()[0]
        transport.dispose_adapter(info)
        assert fake['calls']['dispose'] == [hs, hs]
        transport.dispose_adapter(AdapterInfo('x', 0, 0, 0, 0, None, 0, 0, 0, False, device=None))
        assert len(fake['calls']['dispose']) == 2


class TestLibusbLookup:
    def test_bundled_library_next_to_the_executable_is_preferred(self, monkeypatch, tmp_path):
        fake = install_fake_usb(monkeypatch, [])
        bundled = tmp_path / 'libusb-1.0.0.dylib'
        bundled.write_bytes(b'')
        monkeypatch.setattr(sys, 'executable', str(tmp_path / 'python'))
        monkeypatch.delattr(sys, 'frozen', raising=False)
        assert transport.libusb_backend() == 'backend'
        finder = fake['calls']['get_backend'][0]
        assert callable(finder) and finder('usb-1.0') == str(bundled)

    def test_frozen_app_looks_in_meipass_first(self, monkeypatch, tmp_path):
        fake = install_fake_usb(monkeypatch, [])
        internal = tmp_path / '_internal'
        internal.mkdir()
        (internal / 'libusb-1.0.so.0').write_bytes(b'')
        monkeypatch.setattr(sys, 'executable', str(tmp_path / 'app'))
        monkeypatch.setattr(sys, 'frozen', True, raising=False)
        monkeypatch.setattr(sys, '_MEIPASS', str(internal), raising=False)
        transport.libusb_backend()
        assert fake['calls']['get_backend'][0]('usb-1.0') == str(internal / 'libusb-1.0.so.0')

    def test_falls_back_to_pyusb_discovery(self, monkeypatch, tmp_path):
        fake = install_fake_usb(monkeypatch, [])
        monkeypatch.setattr(sys, 'executable', str(tmp_path / 'python'))
        monkeypatch.delattr(sys, 'frozen', raising=False)
        assert transport.libusb_backend() == 'backend'
        assert fake['calls']['get_backend'] == [None]


class TestPyUsbTransport:
    def test_claims_interface_0_and_reads_the_packet_size(self, monkeypatch):
        device = HS()
        fake = install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84)
        assert fake['calls']['claim'] == [(device, 0)]
        assert usb_transport.max_packet_size == 512
        assert device.set_configuration_calls == 0

    def test_configures_an_unconfigured_device(self, monkeypatch):
        device = HS(configured=False)
        install_fake_usb(monkeypatch, [device])
        PyUsbTransport(device, 0x02, 0x84)
        assert device.set_configuration_calls == 1

    def test_claim_failure_is_a_transport_error_and_disposes(self, monkeypatch):
        device = HS()
        fake = install_fake_usb(monkeypatch, [device])
        device.claim_error = fake['core'].USBError('busy')
        with pytest.raises(TransportError):
            PyUsbTransport(device, 0x02, 0x84)
        assert fake['calls']['dispose'] == [device]

    def test_control_in_uses_the_vendor_request_types(self, monkeypatch):
        device = HS()
        install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84)
        device.ctrl_reply = bytes.fromhex('41785634120000000000000000000000')
        assert usb_transport.control_in(0x41, 0, 0, 16, 1000) == device.ctrl_reply
        assert device.ctrl_calls[-1] == (0xC0, 0x41, 0, 0, 16, 1000)
        usb_transport.control_in(0xF8, 0, 1, 9, 1000, request_type=0xC1)
        assert device.ctrl_calls[-1][:4] == (0xC1, 0xF8, 0, 1)

    def test_bulk_out_and_in(self, monkeypatch):
        device = HS()
        install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84)
        usb_transport.bulk_out(b'\x06\x00\x00\x00\x04\x00\x00\x00', 2000)
        assert device.writes == [(0x02, b'\x06\x00\x00\x00\x04\x00\x00\x00', 2000)]
        device.next_read = b'\x06\x01\x30\x00\x00\x00\x00\x00\x04\x00\x00\x00'
        assert usb_transport.bulk_in(12, 5000) == device.next_read
        assert device.reads == [(0x84, 12, 5000)]

    def test_raw_in_packet_size_is_read_from_its_own_descriptor(self, monkeypatch):
        device = HS()
        device.raw_packet = 64
        install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84, endpoint_out_raw=0x06, endpoint_in_raw=0x88)
        assert usb_transport.max_packet_size == 512 and usb_transport.max_packet_size_raw == 64
        without = PyUsbTransport(device, 0x02, 0x84)
        assert without.max_packet_size_raw == transport.DEFAULT_MAX_PACKET_SIZE

    def test_raw_endpoints_and_interrupt(self, monkeypatch):
        device = HS()
        install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84, endpoint_out_raw=0x06, endpoint_in_raw=0x88,
                                       endpoint_interrupt=0x81)
        assert usb_transport.max_packet_size_raw == 512
        usb_transport.bulk_out_raw(b'*CLS;' * 410, 5000)
        assert device.writes == [(0x06, b'*CLS;' * 410, 5000)]
        device.next_read = b'KEITHLEY'
        assert usb_transport.bulk_in_raw(20992, 5000) == b'KEITHLEY'
        assert device.reads[-1] == (0x88, 20992, 5000)
        device.next_read = b''
        assert usb_transport.bulk_in_raw(4608, 100) == b''   # a zero-length transfer (§10.1.3)
        device.next_read = bytes.fromhex('30 18 00 60 31 a1 01 00')
        assert usb_transport.interrupt_in(64, 1000) == device.next_read
        assert device.reads[-1] == (0x81, 64, 1000)

    def test_short_raw_read_that_used_the_whole_wait_is_a_timeout_with_the_partial_bytes(self, monkeypatch):
        device = HS()
        install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84, endpoint_out_raw=0x06, endpoint_in_raw=0x88)
        clock = iter([0.0, 0.05, 0.0, 5.0])  # first read: 50 ms of a 5 s wait; second: the full 5 s
        monkeypatch.setattr(transport.time, 'monotonic', lambda: next(clock))
        device.next_read = b'short packet'
        assert usb_transport.bulk_in_raw(4608, 5000) == b'short packet'      # the device's short packet
        device.next_read = b'PART'
        with pytest.raises(TransportTimeout) as info:                          # pyusb's partial count
            usb_transport.bulk_in_raw(4608, 5000)
        assert info.value.partial == b'PART'

    def test_zero_length_raw_read_before_the_deadline_is_the_adapter_ending_the_transfer(self, monkeypatch):
        # §10.6.6: at its own timeout (4.195 s under the 3 s code) the adapter completes the
        # pending 0x88 transfer with a zero-length packet. Inside a 25 s host wait that is data
        # of length zero, not a host timeout.
        device = HS()
        install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84, endpoint_out_raw=0x06, endpoint_in_raw=0x88)
        monkeypatch.setattr(transport.time, 'monotonic', iter([0.0, 4.195]).__next__)
        device.next_read = b''
        assert usb_transport.bulk_in_raw(20992, 25480) == b''

    def test_full_raw_read_at_the_deadline_is_not_a_timeout(self, monkeypatch):
        device = HS()
        install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84, endpoint_out_raw=0x06, endpoint_in_raw=0x88)
        monkeypatch.setattr(transport.time, 'monotonic', iter([0.0, 9.0]).__next__)
        device.next_read = bytes(512)
        assert usb_transport.bulk_in_raw(512, 5000) == bytes(512)

    def test_control_out_uses_the_host_to_device_vendor_type(self, monkeypatch):
        device = HS()
        install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84)
        usb_transport.control_out(0x3B, 0, 0, b'', 1000)
        assert device.ctrl_calls[-1] == (0x40, 0x3B, 0, 0, b'', 1000)

    def test_models_without_the_alternate_pair_refuse_raw_transfers(self, monkeypatch):
        device = FakeDevice(t.VENDOR_ID, t.PID_USB_B)
        install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x82)
        with pytest.raises(TransportError):
            usb_transport.bulk_out_raw(b'x', 1000)
        with pytest.raises(TransportError):
            usb_transport.bulk_in_raw(512, 1000)
        with pytest.raises(TransportError):
            usb_transport.interrupt_in(64, 1000)
        assert device.writes == [] and device.reads == []

    def test_short_raw_write_returns_the_count_accepted(self, monkeypatch):
        # pyusb hands back the partial count when the wait expires after some bytes moved.
        device = HS()
        install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84, endpoint_out_raw=0x06, endpoint_in_raw=0x88)
        assert usb_transport.bulk_out_raw(bytes(2050), 5000) == 2050
        device.write_returns = 100
        assert usb_transport.bulk_out_raw(bytes(2050), 5000) == 100

    def test_short_write_is_a_timeout(self, monkeypatch):
        # pyusb returns a short count only when the wait ran out after some packets went: a hung
        # adapter taking the first packets of a message (§8.17) must read as a timeout, which
        # the controller reports as the hung adapter, not as another USB failure.
        device = HS()
        install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84)
        device.write_returns = 3
        with pytest.raises(TransportTimeout):
            usb_transport.bulk_out(b'\x06\x00\x00\x00\x04\x00\x00\x00', 2000)

    def test_usb_timeout_becomes_transport_timeout(self, monkeypatch):
        device = HS()
        fake = install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84)
        device.next_read = fake['core'].USBTimeoutError('timed out')
        with pytest.raises(TransportTimeout):
            usb_transport.bulk_in(12, 100)
        device.next_read = fake['core'].USBError('pipe error')
        with pytest.raises(TransportError) as info:
            usb_transport.bulk_in(12, 100)
        assert not isinstance(info.value, TransportTimeout)

    def test_a_stall_is_its_own_error_whichever_way_pyusb_marks_it(self, monkeypatch):
        # pyusb's libusb1 backend: USBError('Pipe error', -9, EPIPE) for LIBUSB_ERROR_PIPE.
        device = HS()
        fake = install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84, endpoint_out_raw=0x06, endpoint_in_raw=0x88)
        for error in (fake['core'].USBError('Pipe error', -9, errno.EPIPE),
                      fake['core'].USBError('Pipe error', -9, None),
                      fake['core'].USBError('Broken pipe', None, errno.EPIPE)):
            device.write_error = error
            with pytest.raises(TransportStall):
                usb_transport.bulk_out_raw(bytes(2502), 5000)
        assert issubclass(TransportStall, TransportError) and not issubclass(TransportStall, TransportTimeout)

    def test_other_usb_errors_are_not_stalls(self, monkeypatch):
        device = HS()
        fake = install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84, endpoint_out_raw=0x06, endpoint_in_raw=0x88)
        device.write_error = fake['core'].USBError('Input/output error', -1, errno.EIO)
        with pytest.raises(TransportError) as info:
            usb_transport.bulk_out_raw(bytes(2502), 5000)
        assert not isinstance(info.value, (TransportStall, TransportGone))
        # What the controller logs when the raw OUT of a 0x0e fails.
        assert (info.value.errno, info.value.backend_code) == (errno.EIO, -1)

    def test_no_such_device_is_its_own_error_whichever_way_pyusb_marks_it(self, monkeypatch):
        # pyusb's libusb1 backend: USBError('No such device (it may have been disconnected)',
        # -4, ENODEV) for LIBUSB_ERROR_NO_DEVICE; its libusb0 backend passes -ENODEV as the
        # backend code and no errno. The bench saw errno 19 on every call after an unplug (§11.2).
        device = HS()
        fake = install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84, endpoint_out_raw=0x06, endpoint_in_raw=0x88)
        for error in (fake['core'].USBError('No such device (it may have been disconnected)', -4, errno.ENODEV),
                      fake['core'].USBError('No such device', -4, None),
                      fake['core'].USBError('No such device', -errno.ENODEV, None),
                      fake['core'].USBError('No such device', None, errno.ENODEV)):
            device.next_read = error
            with pytest.raises(TransportGone) as info:
                usb_transport.bulk_in(12, 100)
            assert 'no longer on the USB bus' in str(info.value)
            device.clear_halt_error = error
            with pytest.raises(TransportGone):
                usb_transport.clear_halt(0x88)
        assert issubclass(TransportGone, TransportError) and not issubclass(TransportGone, TransportTimeout)

    def test_a_device_gone_before_the_claim_is_reported_as_gone(self, monkeypatch):
        device = HS()
        fake = install_fake_usb(monkeypatch, [device])
        device.claim_error = fake['core'].USBError('No such device', -4, errno.ENODEV)
        with pytest.raises(TransportGone):
            PyUsbTransport(device, 0x02, 0x84)
        assert fake['calls']['dispose'] == [device]

    def test_a_stall_carries_the_errno_and_backend_code_it_came_with(self, monkeypatch):
        device = HS()
        fake = install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84, endpoint_out_raw=0x06, endpoint_in_raw=0x88)
        device.write_error = fake['core'].USBError('Pipe error', -9, errno.EPIPE)
        with pytest.raises(TransportStall) as info:
            usb_transport.bulk_out_raw(bytes(2502), 5000)
        assert (info.value.errno, info.value.backend_code) == (errno.EPIPE, -9)
        assert (TransportError('made here').errno, TransportError('made here').backend_code) == (None, None)

    def test_clear_halt_names_the_endpoint_and_wraps_a_failure(self, monkeypatch):
        device = HS()
        fake = install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84, endpoint_out_raw=0x06, endpoint_in_raw=0x88)
        usb_transport.clear_halt(0x06)
        usb_transport.clear_halt(0x02)
        assert device.halts_cleared == [0x06, 0x02]
        device.clear_halt_error = fake['core'].USBError('No such device', -4, errno.ENODEV)
        with pytest.raises(TransportError):
            usb_transport.clear_halt(0x06)

    def test_device_present_finds_this_device_by_bus_and_address_and_opens_nothing(self, monkeypatch):
        # §10.11: on macOS the open handle of an unplugged adapter never says "no such device".
        device = HS(bus=2, address=7, serial='01CEE482')
        devices = [device]
        fake = install_fake_usb(monkeypatch, devices)
        usb_transport = PyUsbTransport(device, 0x02, 0x84)
        assert usb_transport.device_present() is True
        assert fake['calls']['find'][-1] == ('backend', {'idVendor': t.VENDOR_ID, 'idProduct': t.PID_HS,
                                                         'bus': 2, 'address': 7})
        assert device.serial_reads == 0 and device.ctrl_calls == [] and fake['calls']['dispose'] == []
        devices.clear()   # unplugged
        assert usb_transport.device_present() is False
        devices.append(HS(bus=2, address=8, serial='01CEE482'))   # back, as a new device
        assert usb_transport.device_present() is False

    def test_device_present_cannot_tell_without_an_enumeration(self, monkeypatch):
        device = HS()
        fake = install_fake_usb(monkeypatch, [device])
        usb_transport = PyUsbTransport(device, 0x02, 0x84)

        def find(**kwargs):
            raise fake['core'].USBError('Other error', -99, None)
        fake['core'].find = find
        assert usb_transport.device_present() is None
        install_fake_usb(monkeypatch, [device], backend=None)
        assert usb_transport.device_present() is None
        device.address = None
        assert usb_transport.device_present() is None

    def test_close_releases_and_disposes(self, monkeypatch):
        device = HS()
        fake = install_fake_usb(monkeypatch, [device])
        PyUsbTransport(device, 0x02, 0x84).close()
        assert fake['calls']['release'] == [(device, 0)]
        assert fake['calls']['dispose'] == [device]

    def test_open_transport_needs_a_device_handle(self, monkeypatch):
        install_fake_usb(monkeypatch, [])
        info = AdapterInfo('GPIB-USB-HS', t.VENDOR_ID, t.PID_HS, 0, 0, None, 0x02, 0x84, 0x81, False, device=None)
        with pytest.raises(TransportError):
            transport.open_transport(info)

    def test_open_transport_end_to_end(self, monkeypatch):
        device = HS()
        fake = install_fake_usb(monkeypatch, [device])
        info = transport.find_adapters()[0]
        usb_transport = transport.open_transport(info)
        assert isinstance(usb_transport, PyUsbTransport)
        assert fake['calls']['claim'] == [(device, 0)]
        # The raw pair and the interrupt endpoint come from the enumerated model.
        usb_transport.bulk_out_raw(b'x', 100)
        device.next_read = b''
        usb_transport.bulk_in_raw(512, 100)
        usb_transport.interrupt_in(64, 100)
        assert [w[0] for w in device.writes] == [0x06] and [r[0] for r in device.reads] == [0x88, 0x81]

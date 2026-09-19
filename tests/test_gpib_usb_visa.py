"""The pyvisa-py session end to end: install, list, open, query, dispatch.

A behavioural fake adapter answers each protocol message the way the real one
would (readiness, register writes, addressing, data in and out, the NDAC
presence probe), holding one fake instrument at address 24 that answers
``*IDN?``. That lets pyvisa drive the whole stack: ``ResourceManager("@py")``,
``list_resources``, ``open_resource``, ``query``.
"""
import sys
from typing import Dict, List, Optional, Tuple

import pytest

pyvisa_py = pytest.importorskip('pyvisa_py')
# pyvisa-py 0.8 requires Python 3.10, so a 3.9 run resolves to a release whose
# Session API predates what the session is written against. The driver itself
# degrades (install() catches the ImportError and registers nothing); there is
# nothing here to test on such a run.
if not hasattr(pytest.importorskip('pyvisa_py.sessions'), 'OpenError'):
    pytest.skip('pyvisa-py predates the Session API this needs (0.8+)',
                allow_module_level=True)

import pyvisa  # noqa: E402
from pyvisa import constants  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402
from pyvisa_py.sessions import OpenError, Session  # noqa: E402

import resistamet_gui.gpib_usb as gpib_usb  # noqa: E402
from resistamet_gui.gpib_usb import protocol as p  # noqa: E402
from resistamet_gui.gpib_usb import tables as t  # noqa: E402
from resistamet_gui.gpib_usb import controller as controller_module  # noqa: E402
from resistamet_gui.gpib_usb import transport, visa_session  # noqa: E402
from resistamet_gui.gpib_usb.boards import BoardRegistry  # noqa: E402
from resistamet_gui.gpib_usb.transport import AdapterInfo, TransportError  # noqa: E402
from resistamet_gui.gpib_usb.visa_intfc import NiUsbGpibIntfcDispatch  # noqa: E402
from resistamet_gui.gpib_usb.visa_session import GPIB_INSTR, NiUsbGpibDispatch  # noqa: E402


def h(text: str) -> bytes:
    return bytes.fromhex(text.replace(' ', ''))


# ---------------------------------------------------------------------------
# a behavioural fake adapter
# ---------------------------------------------------------------------------

class FakeInstrument:
    def __init__(self, idn: str) -> None:
        self.idn = idn
        self.received: List[bytes] = []
        self.pending = b''
        self.cleared = 0
        #: What a serial poll returns.
        self.status_byte = 0

    def accept(self, data: bytes, eoi: bool) -> None:
        self.received.append(data)
        if data.strip() == b'*IDN?':
            self.pending += (self.idn + '\n').encode()


class SimulatedAdapter:
    """Answers protocol messages like an attached HS with instruments on the bus."""

    max_packet_size = 512

    def __init__(self, instruments: Dict[int, FakeInstrument], serial_reply: bytes = h('41 78 56 34 12')) -> None:
        self.instruments = instruments
        self.serial_reply = serial_reply
        self.listening: List[int] = []
        self.talker: Optional[int] = None
        self.atn = True
        self.ren = False
        #: The adapter's own addressed state (its address is 0).
        self.own_talker = False
        self.own_listener = False
        #: Set by a test to hold the SRQ line asserted.
        self.srq = False
        self.reply = b''
        #: What the next bulk_in_raw returns (the data of a 0x0b), None when none is owed.
        self.raw_reply: Optional[bytes] = None
        #: (length, EOI) of the 0x0e whose bytes the next bulk_out_raw must bring.
        self.pending_raw_write: Optional[Tuple[int, bool]] = None
        self.raw_writes: List[bytes] = []
        self.messages: List[bytes] = []
        self.control_requests: List[int] = []
        self.bulk_in_timeouts: List[int] = []
        self.raw_in_timeouts: List[int] = []
        self.closed = False
        #: Raised by the next bulk_out, once.
        self.fail_next: Optional[Exception] = None
        #: Raised by the next control_in, once.
        self.fail_next_control: Optional[Exception] = None

    def control_in(self, request, value, index, length, timeout_ms, request_type=0xC0) -> bytes:
        if self.fail_next_control is not None:
            failure, self.fail_next_control = self.fail_next_control, None
            raise failure
        self.control_requests.append(request)
        if request == 0x41:
            return self.serial_reply
        if request == 0x40:
            return h('40 01 00 01 30 01 02 03 00 03 96 00 00 00 00 00')
        if request == 0x21:
            return self._status(request, ibsta=self.ibsta())
        return bytes((request,)) + h('01 30 00 00 00 00 00')

    def ibsta(self) -> int:
        """CMPL and CIC always; ATN, TACS, LACS and SRQI from the bus state."""
        return (0x0120 | (0x0010 if self.atn else 0) | (0x0008 if self.own_talker else 0)
                | (0x0004 if self.own_listener else 0) | (0x1000 if self.srq else 0))

    def bus_lines(self) -> int:
        """The BSR of §5.13 for the fake's state: a listener holds NDAC while ATN is false."""
        ndac = 0x20 if (self.listening and not self.atn) else 0x00
        return (ndac | (0x01 if self.ren else 0) | (0x80 if self.atn else 0)
                | (0x04 if self.srq else 0))

    def _status(self, opcode: int, error: int = 0, count: int = 0, ibsta: int = 0x0130) -> bytes:
        return (bytes((opcode,)) + ibsta.to_bytes(2, 'big') + bytes((error,))
                + (count & 0xFFFF).to_bytes(2, 'little') + b'\x00\x00')

    def bulk_out(self, data: bytes, timeout_ms: int) -> None:
        if self.fail_next is not None:
            failure, self.fail_next = self.fail_next, None
            raise failure
        self.messages.append(data)
        opcode = data[0]
        if opcode in (p.OP_TAKE_CONTROL, p.OP_INTERFACE_CLEAR):
            self.atn = True
            self.reply = self._status(opcode) + h('04 00 00 00')
        elif opcode == p.OP_GO_TO_STANDBY:
            self.atn = False
            self.reply = self._status(opcode) + h('04 00 00 00')
        elif opcode == p.OP_REGISTER_WRITE:
            for start in range(3, 3 + 3 * data[1], 3):
                if data[start:start + 3] == bytes(t.REN_ON_WRITE):
                    self.ren = True
                elif data[start:start + 3] == bytes(t.REN_OFF_WRITE):
                    self.ren = False
            self.reply = self._status(opcode) + bytes((data[1], 0, 0, 0)) + h('04 00 00 00')
        elif opcode == p.OP_REGISTER_READ:
            self.reply = bytes((0x34, self.bus_lines(), 0, 0, 0x35, 1, 0, 0)) + h('04 00 00 00')
        elif opcode == p.OP_COMMAND:
            self.reply = self._command(data)
        elif opcode == p.OP_WRITE:
            self.reply = self._write(data)
        elif opcode == p.OP_READ:
            self.reply = self._read(data)
        elif opcode == p.OP_READ_RAW:
            self.reply = self._read_raw(data)
        elif opcode == p.OP_WRITE_RAW:
            # §10.5.2: the header now, the bytes on the alternate OUT next; the reply after those.
            self.pending_raw_write = (-int.from_bytes(data[8:12], 'little', signed=True), bool(data[6] & 0x08))
        elif opcode == p.OP_SERIAL_POLL:
            self.reply = self._serial_poll(data)
        else:
            raise AssertionError('unexpected opcode 0x%02x' % opcode)

    def _command(self, data: bytes) -> bytes:
        count = 0x100 - data[1]
        command_bytes = data[4:4 + count]
        if not self.instruments:
            return self._status(p.OP_COMMAND, error=5, count=-count) + h('04 00 00 00')
        self.atn = True
        for byte in command_bytes:
            if byte == t.CMD_UNL:
                self.listening = []
                self.own_listener = False
            elif 0x20 <= byte <= 0x3E:
                if byte - 0x20 in self.instruments:
                    self.listening.append(byte - 0x20)
                self.own_listener = self.own_listener or byte == 0x20
            elif 0x40 <= byte <= 0x5E:
                self.talker = byte - 0x40 if byte - 0x40 in self.instruments else None
                self.own_talker = byte == 0x40
            elif byte == t.CMD_UNT:
                self.talker = None
                self.own_talker = False
            elif byte == t.CMD_SDC:
                for pad in self.listening:
                    self.instruments[pad].cleared += 1
                    self.instruments[pad].pending = b''
            elif byte == t.CMD_DCL:
                for instrument in self.instruments.values():
                    instrument.cleared += 1
                    instrument.pending = b''
        return self._status(p.OP_COMMAND) + h('04 00 00 00')

    def _write(self, data: bytes) -> bytes:
        length = 0x10000 - int.from_bytes(data[1:3], 'little')
        payload = data[8:8 + length]
        if not self.listening:
            return self._status(p.OP_WRITE, error=8, count=-length) + h('04 00 00 00')
        for pad in self.listening:
            self.instruments[pad].accept(payload, bool(data[6] & 0x08))
        return self._status(p.OP_WRITE) + h('04 00 00 00')

    def _serial_poll(self, data: bytes) -> bytes:
        """0x10 (§10.5.4): ``3a P S sb`` then a 0x39 status block; error 0x0a for an absent device."""
        pad, sad_byte = data[4], data[5]
        instrument = self.instruments.get(pad)
        self.atn = True  # the adapter addresses the bus itself
        if instrument is None:
            return bytes((0x3A, pad, sad_byte, 0x00)) + self._status(0x39, error=0x0A, count=-1) + h('04 00 00 00')
        return bytes((0x3A, pad, sad_byte, instrument.status_byte)) + self._status(0x39, ibsta=0x0074) + h('04 00 00 00')

    def _write_raw(self, payload: bytes, eoi: bool) -> bytes:
        """The 0x0e reply once the bytes have arrived: an 8-byte status with a 32-bit count."""
        if not self.listening:
            count = (-len(payload)).to_bytes(4, 'little', signed=True)
            return bytes((p.OP_WRITE_RAW, 0x00, 0x28, 0x08)) + count + h('04 00 00 00')
        for pad in self.listening:
            self.instruments[pad].accept(payload, eoi)
        return bytes((p.OP_WRITE_RAW, 0x00, 0x28, 0x00)) + h('00 00 00 00') + h('04 00 00 00')

    def _talker_output(self, requested: int, eos_mode: int, eos_char: int) -> Optional[Tuple[bytes, bool]]:
        """What the addressed talker gives up for one read: (bytes, END), or None when nothing is pending."""
        instrument = self.instruments.get(self.talker) if self.talker is not None else None
        if instrument is None or not instrument.pending:
            return None
        source = instrument.pending
        if eos_mode & 0x04 and bytes((eos_char,)) in source:
            cut = source.index(bytes((eos_char,))) + 1
        else:
            cut = len(source)
        cut = min(cut, requested)
        out, instrument.pending = source[:cut], source[cut:]
        end = not instrument.pending or bool(eos_mode & 0x04 and out.endswith(bytes((eos_char,))))
        return out, end

    def _read_raw(self, data: bytes) -> bytes:
        """0x0b (§10.1.3): the bytes go to the alternate IN, a 12-byte 0x0b block and the
        clear-END write's status come back on the primary."""
        requested = -int.from_bytes(data[4:8], 'little', signed=True)
        result = None if self.atn else self._talker_output(requested, data[1], data[2])
        if result is None:
            out, end, error = b'', False, (2 if self.atn else 0x0A)
        else:
            (out, end), error = result, 0
        self.raw_reply = out
        count = (len(out) - requested).to_bytes(4, 'little', signed=True)
        status = bytes((p.OP_READ_RAW,)) + (0x2064 if end else 0x0064).to_bytes(2, 'big') + bytes((error,))
        return (status + count + bytes((0xE0 if end else 0x60, 0, 0, 0))
                + h('09 00 64 00') + count + h('01 00 00 00') + h('04 00 00 00'))

    def _read(self, data: bytes) -> bytes:
        requested = 0x10000 - int.from_bytes(data[4:6], 'little')
        eos_mode, eos_char = data[1], data[2]
        # The 16-byte trailer as the real adapter sends it.
        trailer_tail = h('04 00 00 00')
        if self.atn:
            return self._status(0x38, error=2, count=-requested) + h('60 00 00 00') + trailer_tail
        result = self._talker_output(requested, eos_mode, eos_char)
        if result is None:
            return (self._status(0x38, error=0x0A, count=-requested, ibsta=0x0020)
                    + h('e0 5e 00 00') + trailer_tail)
        out, end = result
        blocks = b''
        for start in range(0, len(out), 15):
            chunk = out[start:start + 15]
            blocks += bytes((0x36,)) + chunk + b'\xee' * (15 - len(chunk))
        last_count = len(out) - ((len(out) - 1) // 15) * 15 if out else 0
        status = self._status(0x38, count=len(out) - requested,
                              ibsta=0x2100 if end else 0x0100)
        return blocks + status + bytes((0xE0 if end else 0x60, last_count, 0, 0)) + trailer_tail

    def bulk_in(self, length: int, timeout_ms: int) -> bytes:
        self.bulk_in_timeouts.append(timeout_ms)
        assert len(self.reply) <= length, 'reply of %d bytes would overflow %d' % (len(self.reply), length)
        reply, self.reply = self.reply, b''
        return reply

    # The alternate pair and the interrupt endpoint; behaviour is added with the
    # instructions that use them.
    def bulk_out_raw(self, data: bytes, timeout_ms: int) -> None:
        assert self.pending_raw_write is not None, 'raw bulk OUT with no 0x0e outstanding'
        length, eoi = self.pending_raw_write
        assert len(data) == length, 'the 0x0e announced %d bytes, %d arrived' % (length, len(data))
        self.pending_raw_write = None
        self.raw_writes.append(data)
        self.reply = self._write_raw(data, eoi)

    def bulk_in_raw(self, length: int, timeout_ms: int) -> bytes:
        self.raw_in_timeouts.append(timeout_ms)
        assert self.raw_reply is not None, 'raw bulk IN with no 0x0b outstanding'
        assert len(self.raw_reply) < length, 'raw data of %d bytes needs a buffer larger than %d' % (len(self.raw_reply), length)
        reply, self.raw_reply = self.raw_reply, None
        return reply

    def interrupt_in(self, length: int, timeout_ms: int) -> bytes:
        raise AssertionError('unexpected interrupt read')

    def control_out(self, request, value, index, data, timeout_ms, request_type=0x40) -> None:
        raise AssertionError('unexpected control OUT 0x%02x' % request)

    def close(self) -> None:
        self.closed = True

    def instructions(self, opcode: int) -> List[bytes]:
        return [m for m in self.messages if m[0] == opcode]


def fake_adapter_info(serial: Optional[str] = '01234567', bus: int = 20, address: int = 5) -> AdapterInfo:
    return AdapterInfo(model='GPIB-USB-HS', vendor_id=t.VENDOR_ID, product_id=t.PID_HS, bus=bus,
                       address=address, serial=serial, endpoint_out=0x02, endpoint_in=0x84,
                       endpoint_interrupt=0x81, needs_firmware=False, device=None)


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


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

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


@pytest.fixture
def adapter(monkeypatch, session_registry, enumeration):
    """One fake HS at GPIB0 with a Keithley-like instrument at address 24, installed."""
    sim = SimulatedAdapter({24: FakeInstrument('KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30')})
    monkeypatch.setattr(gpib_usb, 'available', lambda: True)
    monkeypatch.setattr(controller_module, 'IFC_SETTLE_S', 0.0)  # the fake needs no settle
    monkeypatch.setattr(visa_session, '_REGISTRY',
                        BoardRegistry(open_transport=lambda i: sim, first_board=0))
    session_registry[GPIB_INSTR] = Sentinel
    gpib_usb.install()
    return sim


@pytest.fixture
def rm(adapter):
    # The class itself, not the ``pyvisa.ResourceManager`` name: the --simulate
    # machinery rebinds that name process-wide and other test modules leave it so.
    manager = pyvisa.highlevel.ResourceManager('@py')
    yield manager
    manager.close()


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

class TestInstall:
    def test_dispatcher_wraps_the_previous_class(self, adapter):
        assert Session._session_classes[GPIB_INSTR] is NiUsbGpibDispatch
        assert NiUsbGpibDispatch.previous is Sentinel

    def test_install_is_idempotent(self, adapter):
        gpib_usb.install()
        gpib_usb.install()
        assert Session._session_classes[GPIB_INSTR] is NiUsbGpibDispatch
        assert NiUsbGpibDispatch.previous is Sentinel

    def test_install_is_a_no_op_without_libusb(self, monkeypatch, session_registry):
        monkeypatch.setattr(gpib_usb, 'available', lambda: False)
        session_registry[GPIB_INSTR] = Sentinel
        gpib_usb.install()
        assert session_registry[GPIB_INSTR] is Sentinel


class TestListResources:
    def test_lists_the_instrument_found_by_the_probe(self, rm, adapter):
        resources = rm.list_resources()
        assert 'GPIB0::24::INSTR' in resources
        assert not [r for r in resources if r.startswith('GPIB0::') and r != 'GPIB0::24::INSTR']

    def test_merges_the_previous_class_listing(self, rm):
        assert 'GPIB9::1::INSTR' in rm.list_resources()

    def test_probing_attaches_then_releases_the_board(self, rm, adapter):
        rm.list_resources()
        assert adapter.control_requests[:2] == [0x41, 0x40]
        assert adapter.messages[0] == p.register_write_message(t.register_init_writes())
        assert adapter.messages[-1] == p.register_write_message(t.SHUTDOWN_WRITES)
        assert adapter.closed

    def test_previous_listing_failure_does_not_hide_our_boards(self, rm, monkeypatch):
        def boom():
            raise RuntimeError('linux-gpib exploded')
        monkeypatch.setattr(Sentinel, 'list_resources', staticmethod(boom))
        assert 'GPIB0::24::INSTR' in rm.list_resources()

    def test_listing_while_a_session_is_open_keeps_the_board_attached(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        assert 'GPIB0::24::INSTR' in rm.list_resources()
        assert adapter.control_requests.count(0x41) == 1
        assert not adapter.closed
        inst.query('*IDN?')
        inst.close()
        assert adapter.closed


class TestInstrumentSession:
    def test_query_round_trips(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 5000
        assert inst.query('*IDN?') == 'KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30\n'
        write = adapter.instructions(p.OP_WRITE)[-1]
        assert write == p.write_message(b'*IDN?\r\n', 0xFD, send_eoi=True)
        # pyvisa reads in 20480-byte chunks, so the read is a 0x0b with the data on the
        # alternate endpoint, as it is under NI's driver (§10.1.1).
        assert adapter.instructions(p.OP_READ) == []
        read = adapter.instructions(p.OP_READ_RAW)[-1]
        # Compare disabled (m 00) with the default termination character in e, 10 s code, -20480:
        # the bytes NI sends for the same read (§10.1.2, idn.pcap 0.5160 with code 0xfe).
        assert read[:8] == h('0b 00 0a fd 00 b0 ff ff')
        assert adapter.raw_in_timeouts[-1] == 15000 + 20480  # host wait + 20480 B at 1000 B/s
        # Addressing: controller talks / instrument listens, then instrument talks.
        commands = adapter.instructions(p.OP_COMMAND)[-2:]
        assert commands[0][4:7] == bytes((0x3F, 0x40, 0x38))
        assert commands[1][4:7] == bytes((0x3F, 0x20, 0x58))
        inst.close()

    def test_read_termination_selects_eos_and_is_stripped(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR', read_termination='\n')
        assert inst.query('*IDN?') == 'KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30'
        read = adapter.instructions(p.OP_READ_RAW)[-1]
        assert read[1:3] == h('14 0a')
        inst.close()

    def test_a_long_write_goes_raw_with_the_termination_character_in_the_header(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 20000
        inst.write('*CLS;' * 409 + '*CL')  # 2048 + '\r\n' = 2050 bytes, as longwrite.pcap
        assert adapter.instructions(p.OP_WRITE) == []
        header = adapter.instructions(p.OP_WRITE_RAW)[-1]
        assert header == h('0e 00 00 fe 00 0a 08 00 fe f7 ff ff 04 00 00 00')
        assert adapter.raw_writes[-1] == b'*CLS;' * 409 + b'*CL\r\n'
        assert adapter.instruments[24].received[-1] == adapter.raw_writes[-1]
        inst.close()

    def test_a_long_write_to_an_empty_address_reports_no_listeners(self, rm, adapter):
        inst = rm.open_resource('GPIB0::5::INSTR')
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            inst.write('x' * 3000)
        assert info.value.error_code == StatusCode.error_no_listeners
        inst.close()

    def test_changing_the_termination_character_changes_e_on_both_read_forms(self, rm, adapter):
        # eosmodes.pcap: TERMCHAR 0x2c enabled -> 14 2c; disabled the character still rides in e.
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.set_visa_attribute(constants.VI_ATTR_TERMCHAR, 0x2C)
        inst.write('*IDN?')
        assert inst.read() == 'KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30\n'
        assert adapter.instructions(p.OP_READ_RAW)[-1][1:3] == h('00 2c')
        inst.set_visa_attribute(constants.VI_ATTR_TERMCHAR_EN, True)
        inst.write('*IDN?')
        assert inst.read() == 'KEITHLEY INSTRUMENTS INC.,'
        assert adapter.instructions(p.OP_READ_RAW)[-1][1:3] == h('14 2c')
        inst.close()

    def test_a_small_chunk_size_reads_through_the_framed_instruction(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.chunk_size = 256
        assert inst.query('*IDN?') == 'KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30\n'
        assert adapter.instructions(p.OP_READ_RAW) == []
        read = adapter.instructions(p.OP_READ)[-1]
        assert read[1:6] == h('00 0a fc 00 ff')  # m 00 e 0a (§10.1.6), 3 s default timeout, -256
        inst.close()

    def test_timeout_attribute_reaches_the_instruction(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 300
        inst.write('*IDN?')
        assert adapter.instructions(p.OP_WRITE)[-1][3] == 0xFA
        inst.timeout = 20000
        inst.write('*IDN?')
        assert adapter.instructions(p.OP_WRITE)[-1][3] == 0xFE
        inst.close()

    def test_timeout_is_rounded_to_the_device_table(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 5000
        assert inst.timeout == 10000
        inst.timeout = 2000
        assert inst.timeout == 3000
        inst.timeout = 2_000_000
        assert inst.timeout == 1_000_000
        inst.write('*IDN?')
        assert adapter.instructions(p.OP_WRITE)[-1][3] == 0x02
        # The host waited for the 1000 s row plus 50 %.
        assert adapter.bulk_in_timeouts[-1] == 1_500_000
        inst.close()

    def test_host_wait_follows_the_effective_device_timeout(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 5000
        inst.write('*IDN?')
        assert adapter.bulk_in_timeouts[-1] == 15000  # 10 s row + max(2, 5)
        inst.close()

    def test_no_response_is_a_visa_timeout(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 100
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            inst.read()
        assert info.value.error_code == StatusCode.error_timeout
        inst.close()

    def test_read_stb_of_an_absent_device_is_a_timeout(self, rm, adapter):
        inst = rm.open_resource('GPIB0::5::INSTR')
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            inst.read_stb()
        assert info.value.error_code == StatusCode.error_timeout
        inst.close()

    def test_write_to_an_empty_address_reports_no_listeners(self, rm, adapter):
        inst = rm.open_resource('GPIB0::5::INSTR')
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            inst.write('*IDN?')
        assert info.value.error_code == StatusCode.error_no_listeners
        inst.close()

    def test_usb_fault_is_error_io_and_the_adapter_recovers(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        adapter.fail_next = TransportError('pipe stalled')
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            inst.write('*IDN?')
        assert info.value.error_code == StatusCode.error_io
        assert inst.query('*IDN?').startswith('KEITHLEY')
        assert adapter.control_requests.count(0x41) == 2  # attach ran again first
        inst.close()

    def test_clear_sends_selected_device_clear_with_the_session_timeout(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 300
        inst.clear()
        last = adapter.instructions(p.OP_COMMAND)[-1]
        assert last[4:7] == bytes((0x3F, 0x38, 0x04))
        assert last[3] == 0xFA
        assert adapter.instruments[24].cleared == 1
        inst.close()

    def test_gpib_attributes(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        assert inst.primary_address == 24
        assert inst.secondary_address == constants.VI_NO_SEC_ADDR
        assert inst.get_visa_attribute(constants.VI_ATTR_INTF_NUM) == 0
        assert inst.get_visa_attribute(constants.VI_ATTR_INTF_TYPE) == constants.InterfaceType.gpib
        assert inst.get_visa_attribute(constants.VI_ATTR_RSRC_CLASS) == 'INSTR'
        assert inst.get_visa_attribute(constants.VI_ATTR_RSRC_NAME) == 'GPIB0::24::INSTR'
        assert inst.get_visa_attribute(constants.VI_ATTR_GPIB_READDR_EN) is True
        inst.set_visa_attribute(constants.VI_ATTR_GPIB_READDR_EN, False)
        assert inst.get_visa_attribute(constants.VI_ATTR_GPIB_READDR_EN) is False
        assert inst.send_end is True
        inst.send_end = False
        inst.write('*IDN?')
        assert adapter.instructions(p.OP_WRITE)[-1][6] == 0x00
        inst.close()

    def test_address_attributes_can_be_changed(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.set_visa_attribute(constants.VI_ATTR_GPIB_PRIMARY_ADDR, 5)
        inst.set_visa_attribute(constants.VI_ATTR_GPIB_SECONDARY_ADDR, 3)
        assert inst.primary_address == 5 and inst.secondary_address == 3
        with pytest.raises(pyvisa.errors.VisaIOError):
            inst.write('*IDN?')  # nobody at 5
        assert adapter.instructions(p.OP_COMMAND)[-1][4:8] == bytes((0x3F, 0x40, 0x25, 0x63))
        inst.set_visa_attribute(constants.VI_ATTR_GPIB_SECONDARY_ADDR, constants.VI_NO_SEC_ADDR)
        assert inst.secondary_address == constants.VI_NO_SEC_ADDR
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            inst.set_visa_attribute(constants.VI_ATTR_GPIB_PRIMARY_ADDR, 31)
        assert info.value.error_code == StatusCode.error_nonsupported_attribute_state
        with pytest.raises(pyvisa.errors.VisaIOError):
            inst.set_visa_attribute(constants.VI_ATTR_GPIB_SECONDARY_ADDR, 40)
        inst.close()

    def test_suppress_end_true_is_rejected(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        assert inst.get_visa_attribute(constants.VI_ATTR_SUPPRESS_END_EN) is False
        inst.set_visa_attribute(constants.VI_ATTR_SUPPRESS_END_EN, False)
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            inst.set_visa_attribute(constants.VI_ATTR_SUPPRESS_END_EN, True)
        assert info.value.error_code == StatusCode.error_nonsupported_attribute_state
        inst.close()

    def test_secondary_address_in_the_resource_name(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::3::INSTR')
        assert inst.secondary_address == 3
        inst.write('*IDN?')
        assert adapter.instructions(p.OP_COMMAND)[-1][4:8] == bytes((0x3F, 0x40, 0x38, 0x63))
        inst.close()

    def test_board_is_shared_and_closed_with_the_last_session(self, rm, adapter):
        first = rm.open_resource('GPIB0::24::INSTR')
        second = rm.open_resource('GPIB0::24::INSTR')
        assert adapter.control_requests.count(0x41) == 1  # attached once
        first.close()
        assert not adapter.closed
        second.close()
        assert adapter.closed
        assert adapter.messages[-1] == p.register_write_message(t.SHUTDOWN_WRITES)

    def test_read_stb_and_trigger_carry_the_session_timeout(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 1000
        adapter.instruments[24].status_byte = 0x40
        commands_before = len(adapter.instructions(p.OP_COMMAND))
        assert inst.read_stb() == 0x40
        # One 0x10 instruction (§10.5.4), no SPE / SPD command bytes and no read.
        assert adapter.instructions(p.OP_SERIAL_POLL)[-1] == h('10 01 00 00 18 00 fb 00 04 00 00 00')
        assert len(adapter.instructions(p.OP_COMMAND)) == commands_before
        assert adapter.instructions(p.OP_READ) == [] and adapter.instructions(p.OP_READ_RAW) == []
        inst.assert_trigger()
        assert adapter.instructions(p.OP_COMMAND)[-1][4:7] == bytes((0x3F, 0x38, 0x08))
        assert adapter.instructions(p.OP_COMMAND)[-1][3] == 0xFB
        inst.close()

    def test_ifc_ren_and_raw_command(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 300
        # pyvisa puts send_ifc on GPIBInterface only; the INSTR session still answers it.
        assert inst.visalib.gpib_send_ifc(inst.session) == StatusCode.success
        assert adapter.messages[-1] == p.interface_clear_message()
        inst.control_ren(constants.RENLineOperation.deassert)
        assert adapter.messages[-1] == p.register_write_message([t.REN_OFF_WRITE])
        inst.control_ren(constants.RENLineOperation.address_gtl)
        assert adapter.instructions(p.OP_COMMAND)[-1][4:7] == bytes((0x3F, 0x38, 0x01))
        assert adapter.instructions(p.OP_COMMAND)[-1][3] == 0xFA
        inst.visalib.gpib_command(inst.session, b'\x14')
        assert adapter.instructions(p.OP_COMMAND)[-1][4:5] == b'\x14'
        assert adapter.instructions(p.OP_COMMAND)[-1][3] == 0xFA
        inst.close()

    def test_unknown_board_is_not_found(self, rm):
        with pytest.raises(pyvisa.errors.VisaIOError):
            rm.open_resource('GPIB3::24::INSTR')
        assert Sentinel.calls == ['GPIB3::24::INSTR']


class TestDispatch:
    def test_other_boards_reach_the_previous_class(self, rm):
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            rm.open_resource('GPIB9::1::INSTR')
        assert info.value.error_code == StatusCode.error_resource_not_found
        assert Sentinel.calls == ['GPIB9::1::INSTR']

    def test_opening_our_board_does_not_re_enumerate_but_a_miss_looks_once(self, rm, enumeration):
        rm.open_resource('GPIB0::24::INSTR').close()
        rm.open_resource('GPIB0::24::INSTR').close()
        assert enumeration['calls'] == 1
        with pytest.raises(pyvisa.errors.VisaIOError):
            rm.open_resource('GPIB9::1::INSTR')
        assert enumeration['calls'] == 2
        assert Sentinel.calls == ['GPIB9::1::INSTR']

    def test_adapter_plugged_in_later_opens_without_a_listing(self, rm, adapter, enumeration):
        enumeration['adapters'].append(fake_adapter_info(serial='LATER', address=9))
        inst = rm.open_resource('GPIB1::24::INSTR')
        assert inst.get_visa_attribute(constants.VI_ATTR_INTF_NUM) == 1
        inst.close()
        assert Sentinel.calls == []

    def test_attach_failure_is_a_visa_error_not_a_crash(self, rm, monkeypatch, adapter):
        def broken(info):
            raise TransportError('no permission')
        monkeypatch.setattr(visa_session, '_REGISTRY', BoardRegistry(open_transport=broken, first_board=0))
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            rm.open_resource('GPIB0::24::INSTR')
        assert info.value.error_code == StatusCode.error_system_error

    def test_attach_failure_closes_the_transport(self, rm, monkeypatch, adapter):
        broken_sim = SimulatedAdapter({}, serial_reply=h('00 00 00 00 00'))
        monkeypatch.setattr(visa_session, '_REGISTRY',
                            BoardRegistry(open_transport=lambda i: broken_sim, first_board=0))
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            rm.open_resource('GPIB0::24::INSTR')
        assert info.value.error_code == StatusCode.error_system_error
        assert broken_sim.closed

    def test_unexpected_exception_while_opening_is_a_visa_error(self, rm, monkeypatch, adapter):
        def broken(info):
            raise RuntimeError('libusb exploded')
        monkeypatch.setattr(visa_session, '_REGISTRY', BoardRegistry(open_transport=broken, first_board=0))
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            rm.open_resource('GPIB0::24::INSTR')
        assert info.value.error_code == StatusCode.error_system_error


class TestBoardRegistry:
    def test_adapters_keep_their_numbers_across_refreshes(self, enumeration, monkeypatch):
        disposed: List[AdapterInfo] = []
        monkeypatch.setattr(transport, 'dispose_adapter', disposed.append)
        first = fake_adapter_info(serial='AAA', bus=1, address=9)
        registry = BoardRegistry(open_transport=lambda i: SimulatedAdapter({}), first_board=0)
        enumeration['adapters'] = [first]
        assert registry.owns('0')
        # A miss looks at the bus again, which hands board 0 a fresh handle.
        assert not registry.owns('1')
        # A second adapter appears at a lower USB address: it must not take GPIB0.
        first_again = fake_adapter_info(serial='AAA', bus=1, address=9)
        second = fake_adapter_info(serial='BBB', bus=1, address=2)
        enumeration['adapters'] = [second, first_again]
        registry.refresh()
        assert registry.board_names() == ['0', '1']
        assert registry.owns('1')
        # Every superseded handle of the re-enumerated adapter, disposed once:
        # one for the miss above, one for this refresh.
        assert [info.serial for info in disposed] == ['AAA', 'AAA']

    def test_a_board_in_use_keeps_its_controller_and_drops_the_duplicate(self, enumeration, monkeypatch):
        disposed: List[AdapterInfo] = []
        monkeypatch.setattr(transport, 'dispose_adapter', disposed.append)
        info = fake_adapter_info(serial='AAA')
        enumeration['adapters'] = [info]
        sims: List[SimulatedAdapter] = []

        def opener(i):
            sims.append(SimulatedAdapter({}))
            return sims[-1]

        registry = BoardRegistry(open_transport=opener, first_board=0)
        controller = registry.acquire('0')
        duplicate = fake_adapter_info(serial='AAA')
        enumeration['adapters'] = [duplicate]
        registry.refresh()
        assert registry.acquire('0') is controller
        assert disposed == [duplicate]
        assert len(sims) == 1
        registry.release('0')
        registry.release('0')
        assert sims[0].closed

    def test_vanished_adapters_are_disposed_and_unplugged_in_use_boards_survive(self, enumeration, monkeypatch):
        disposed: List[AdapterInfo] = []
        monkeypatch.setattr(transport, 'dispose_adapter', disposed.append)
        gone = fake_adapter_info(serial='GONE')
        kept = fake_adapter_info(serial='KEPT', address=6)
        enumeration['adapters'] = [gone, kept]
        registry = BoardRegistry(open_transport=lambda i: SimulatedAdapter({}), first_board=0)
        registry.acquire('1')
        enumeration['adapters'] = []
        registry.refresh()
        assert registry.board_names() == ['1']
        assert disposed == [gone]

    def test_location_identity_when_there_is_no_serial(self, enumeration):
        enumeration['adapters'] = [fake_adapter_info(serial=None, bus=3, address=4)]
        registry = BoardRegistry(open_transport=lambda i: SimulatedAdapter({}), first_board=2)
        assert registry.board_names() == ['2']
        enumeration['adapters'] = [fake_adapter_info(serial=None, bus=3, address=4)]
        registry.refresh()
        assert registry.board_names() == ['2']

    def test_owns_finds_an_adapter_that_appeared_after_the_first_enumeration(self, enumeration):
        enumeration['adapters'] = []
        registry = BoardRegistry(open_transport=lambda i: SimulatedAdapter({}), first_board=0)
        assert registry.owns('0') is False
        assert enumeration['calls'] == 1
        enumeration['adapters'] = [fake_adapter_info(serial='NEW')]
        assert registry.owns('0') is True
        assert enumeration['calls'] == 2
        # A hit does not enumerate; a miss for a board that does not exist does, once, and stays False.
        assert registry.owns('0') is True
        assert enumeration['calls'] == 2
        assert registry.owns('7') is False
        assert enumeration['calls'] == 3

    def test_failed_acquire_re_enumerates_next_time(self, enumeration):
        def broken(info):
            raise TransportError('busy')
        registry = BoardRegistry(open_transport=broken, first_board=0)
        with pytest.raises(TransportError):
            registry.acquire('0')
        calls = enumeration['calls']
        registry.owns('0')
        assert enumeration['calls'] == calls + 1


class TestAvailability:
    def test_unavailable_without_pyusb(self, monkeypatch):
        monkeypatch.setitem(sys.modules, 'usb', None)
        assert gpib_usb.available() is False
        assert gpib_usb.find_adapters() == []
        assert transport.libusb_backend() is None

    def test_open_transport_refuses_a_firmwareless_usb_b(self):
        info = fake_adapter_info()
        info.product_id = t.PID_USB_B_PRE_FIRMWARE
        info.needs_firmware = True
        with pytest.raises(gpib_usb.AdapterNotReady):
            transport.open_transport(info)

    def test_adapter_label(self):
        assert fake_adapter_info().label == 'GPIB-USB-HS (bus 20 address 5 serial 01234567)'

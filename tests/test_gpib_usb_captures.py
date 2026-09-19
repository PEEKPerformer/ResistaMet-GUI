"""The codec against USB captures of NI's own driver (protocol §10).

``docs/design/captures/ni_usb_gpib_2026-09-19/`` holds USBPcap recordings of
NI-488.2 driving a GPIB-USB-HS. They are facts about the adapter, not code,
so the implementer may read them; these tests read them mechanically.

Three checks. Encoding: every instruction block NI sent (0x03, 0x09, 0x0a,
0x0b, 0x0c, 0x0d, 0x0e, 0x10) is rebuilt by our builders from the
parameters decoded out of it and must match byte for byte. Choice: for
every read count and write length NI was captured at, a ``Controller``
picks the instruction NI picked (framed 0x0a / 0x0d or raw 0x0b / 0x0e).
Error paths: the refused 0x0e, the 0x0b the adapter ended itself and the
0x10 reply without its result block parse to the errors VISA reported, and
the USB-level facts the driver relies on (the STALL, the two pipe resets,
no stop request anywhere) are in the recordings. Decoding: every
reply on bulk IN 0x84 splits into known blocks that account for every byte,
every 0x0b reply parses together with the transfer that arrived on 0x88 and
gives the counts and END flags the scenario descriptions promise, every
0x0a reply reassembles, every 0x10 reply yields its status byte, and every
interrupt push yields the instrument's status byte.

The pcap layout (link type 249, USBPcap) is decoded with the few struct
lines of the captures directory's ``usbpcap_dump.py``, copied here so the
test does not import from ``docs``.
"""
import struct
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import pytest

from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t
from resistamet_gui.gpib_usb.controller import RAW_READ_MIN_BYTES, RAW_WRITE_MIN_BYTES, Controller

CAPTURES = Path(__file__).resolve().parents[1] / 'docs' / 'design' / 'captures' / 'ni_usb_gpib_2026-09-19'
ADAPTER_DEVICE_ADDRESS = 2
EP_OUT, EP_IN, EP_OUT_RAW, EP_IN_RAW, EP_INTR = 0x02, 0x84, 0x06, 0x88, 0x81

pytestmark = pytest.mark.skipif(not CAPTURES.is_dir(), reason='NI capture directory not present')


# ---------------------------------------------------------------------------
# pcap decoding (USBPcap, link type 249)
# ---------------------------------------------------------------------------

class Transfer:
    def __init__(self, ts: float, endpoint: int, completion: bool, payload: bytes,
                 usbd_status: int = 0, function: int = 0) -> None:
        self.ts = ts
        self.endpoint = endpoint
        self.completion = completion
        self.payload = payload
        #: The Windows USBD status of a completion; 0 is success.
        self.usbd_status = usbd_status
        #: The URB function: 0x09 a bulk or interrupt transfer, 0x1e a pipe reset.
        self.function = function


def _packets(path: Path) -> Iterator[Tuple[float, bytes]]:
    b = path.read_bytes()
    magic, = struct.unpack_from('<I', b, 0)
    assert magic in (0xa1b2c3d4, 0xa1b23c4d), hex(magic)
    nano = magic == 0xa1b23c4d
    linktype, = struct.unpack_from('<I', b, 20)
    assert linktype == 249, linktype
    off = 24
    while off + 16 <= len(b):
        ts_s, ts_u, incl, _orig = struct.unpack_from('<IIII', b, off)
        off += 16
        yield ts_s + ts_u / (1e9 if nano else 1e6), b[off:off + incl]
        off += incl


def transfers(name: str) -> List[Transfer]:
    """Every transfer of the adapter in ``name.pcap``, in capture order.

    NI hands a message longer than 512 bytes to USB as two OUT transfers,
    512 bytes and the remainder, which the adapter sees as one run of
    packets (§10.5.2). They are joined here into the first of the two, so
    everything downstream sees whole messages.
    """
    out: List[Transfer] = []
    head: Optional[Transfer] = None  # an OUT on 0x02 that is not yet a whole message
    for ts, pkt in _packets(CAPTURES / (name + '.pcap')):
        hdr_len, _irp, status, function, info, _bus, device, endpoint, _transfer, data_len = (
            struct.unpack_from('<HQIHBHHBBI', pkt, 0))
        if device != ADAPTER_DEVICE_ADDRESS:
            continue
        # info bit 0: 1 = completion (device -> host for IN, done for OUT), 0 = request.
        transfer = Transfer(ts, endpoint, bool(info & 1), pkt[hdr_len:hdr_len + data_len], status, function)
        if endpoint == EP_OUT and not transfer.completion and transfer.payload:
            if head is not None:
                head.payload += transfer.payload
                head = None if _host_blocks(head.payload) is not None else head
                continue
            if _host_blocks(transfer.payload) is None:
                head = transfer
        out.append(transfer)
    assert head is None, '%s: an OUT message never reached its termination block' % name
    return out


def control_requests(name: str) -> List[Tuple[int, int]]:
    """(bmRequestType, bRequest) of every control request to the adapter in ``name.pcap``."""
    out: List[Tuple[int, int]] = []
    for _ts, pkt in _packets(CAPTURES / (name + '.pcap')):
        hdr_len, _irp, _status, _function, info, _bus, device, _endpoint, transfer, data_len = (
            struct.unpack_from('<HQIHBHHBBI', pkt, 0))
        # Transfer type 2 is control; its header carries the stage in byte 27, 0 = SETUP.
        if device == ADAPTER_DEVICE_ADDRESS and transfer == 2 and not info & 1 and pkt[27] == 0 and data_len >= 8:
            out.append((pkt[hdr_len], pkt[hdr_len + 1]))
    return out


# ---------------------------------------------------------------------------
# host -> device message splitting (§3.1, §3.2 header lengths)
# ---------------------------------------------------------------------------

def _pad4(n: int) -> int:
    return -(-n // 4) * 4


def split_host_blocks(message: bytes) -> List[bytes]:
    """The instruction blocks of a host message, each with its padding, up to the termination."""
    blocks = _host_blocks(message)
    assert blocks is not None, 'no termination block in %s' % message.hex(' ')
    return blocks


def _host_blocks(message: bytes) -> Optional[List[bytes]]:
    """As ``split_host_blocks``; None when ``message`` ends before its termination block."""
    blocks: List[bytes] = []
    off = 0
    while True:
        if off + 4 > len(message):
            return None
        opcode = message[off]
        if opcode == p.OP_TERMINATION:
            assert message[off:] == p.TERMINATION_BLOCK, message.hex(' ')
            return blocks
        if opcode in (p.OP_STATUS_SNAPSHOT, p.OP_TAKE_CONTROL, p.OP_GO_TO_STANDBY,
                      p.OP_INTERFACE_CLEAR, p.OP_PRESENCE_PROBE):
            length = 4
        elif opcode == p.OP_COMMAND:
            length = _pad4(4 + (0x100 - message[off + 1]))
        elif opcode == p.OP_WRITE:
            length = _pad4(8 + (0x10000 - int.from_bytes(message[off + 1:off + 3], 'little')))
        elif opcode in (p.OP_READ, p.OP_READ_RAW, p.OP_SERIAL_POLL):
            length = 8
        elif opcode == p.OP_WRITE_RAW:
            length = 12
        elif opcode == p.OP_REGISTER_WRITE:
            length = _pad4(3 + 3 * message[off + 1])
        elif opcode == p.OP_REGISTER_READ:
            length = _pad4(2 + 2 * message[off + 1])
        else:
            raise AssertionError('unknown host opcode 0x%02x in %s' % (opcode, message.hex(' ')))
        blocks.append(message[off:off + length])
        off += length


def _eos_params(m: int, e: int) -> Dict[str, object]:
    """Our builder arguments for NI's ``m e`` bytes (§10.1.6)."""
    if m == 0:
        return {'eos': None, 'termchar': e}
    assert m & p.EOS_MODE_REOS, hex(m)
    return {'eos': e, 'eos_8bit': bool(m & p.EOS_MODE_BIN), 'termchar': e}


def rebuild(block: bytes) -> bytes:
    """Our encoder's bytes for the parameters decoded from NI's ``block``."""
    opcode = block[0]
    if opcode == p.OP_STATUS_SNAPSHOT:
        return p.status_snapshot_block()
    if opcode == p.OP_COMMAND:
        count = 0x100 - block[1]
        return p.command_block(block[4:4 + count], block[3]) + bytes(_pad4(4 + count) - 4 - count)
    if opcode == p.OP_WRITE:
        length = 0x10000 - int.from_bytes(block[1:3], 'little')
        assert block[4] == 0 and block[7] == 0, block.hex(' ')
        built = p.write_block(block[8:8 + length], block[3], bool(block[6] & p.WRITE_FLAG_EOI), eos_char=block[5])
        return built + bytes(_pad4(len(built)) - len(built))
    if opcode == p.OP_WRITE_RAW:
        length = -int.from_bytes(block[8:12], 'little', signed=True)
        assert block[1:3] == b'\x00\x00' and block[4] == 0 and block[7] == 0, block.hex(' ')
        return p.write_raw_block(length, block[3], bool(block[6] & p.WRITE_FLAG_EOI), eos_char=block[5])
    if opcode == p.OP_READ:
        count = 0x10000 - int.from_bytes(block[4:6], 'little')
        return p.read_message(count, block[3], **_eos_params(block[1], block[2]))[:8]  # type: ignore[arg-type]
    if opcode == p.OP_READ_RAW:
        count = -int.from_bytes(block[4:8], 'little', signed=True)
        return p.read_raw_block(count, block[3], **_eos_params(block[1], block[2]))  # type: ignore[arg-type]
    if opcode == p.OP_SERIAL_POLL:
        sad = None if block[5] == 0 else block[5] - 0x60
        return p.serial_poll_block(block[4], block[6], sad=sad, flag=block[3])
    if opcode == p.OP_REGISTER_WRITE:
        writes = [tuple(block[i:i + 3]) for i in range(3, 3 + 3 * block[1], 3)]
        built = p.register_write_block(writes)  # type: ignore[arg-type]
        return built + bytes(_pad4(len(built)) - len(built))
    if opcode == p.OP_REGISTER_READ:
        reads = [tuple(block[i:i + 2]) for i in range(2, 2 + 2 * block[1], 2)]
        built = p.register_read_message(reads)[:-4]  # type: ignore[arg-type]
        return built
    if opcode == p.OP_INTERFACE_CLEAR:
        return p.interface_clear_message()[:4]
    if opcode == p.OP_TAKE_CONTROL:
        return p.take_control_message(bool(block[1]))[:4]
    if opcode == p.OP_GO_TO_STANDBY:
        # NI's byte 3 is 0x0a (§10.7.1), meaning not established; ours is 0x00.
        return block
    if opcode == p.OP_PRESENCE_PROBE:
        return block  # not built by this driver
    raise AssertionError('no builder for 0x%02x' % opcode)


ALL_PCAPS = sorted(path.stem for path in CAPTURES.glob('*.pcap')) if CAPTURES.is_dir() else []
RAW_READ_PCAPS = ['trac', 'counts', 'longwrite', 'stb', 'srq']


# ---------------------------------------------------------------------------
# (a) encoding
# ---------------------------------------------------------------------------

class TestEncoderReproducesNi:
    @pytest.mark.parametrize('name', ALL_PCAPS)
    def test_every_host_block_is_rebuilt_byte_for_byte(self, name):
        seen: Dict[int, int] = {}
        for transfer in transfers(name):
            if transfer.endpoint != EP_OUT or transfer.completion or not transfer.payload:
                continue
            for block in split_host_blocks(transfer.payload):
                assert rebuild(block) == block, '%s: 0x%02x block %s' % (name, block[0], block.hex(' '))
                seen[block[0]] = seen.get(block[0], 0) + 1
        assert seen, name

    def test_the_new_instructions_were_exercised(self):
        seen: Dict[int, int] = {}
        for name in RAW_READ_PCAPS:
            for transfer in transfers(name):
                if transfer.endpoint == EP_OUT and not transfer.completion and transfer.payload:
                    for block in split_host_blocks(transfer.payload):
                        seen[block[0]] = seen.get(block[0], 0) + 1
        assert seen[p.OP_READ_RAW] >= 12 and seen[p.OP_WRITE_RAW] >= 1
        assert seen[p.OP_SERIAL_POLL] >= 3 and seen[p.OP_WRITE] >= 10 and seen[p.OP_READ] >= 20


# ---------------------------------------------------------------------------
# (b) decoding
# ---------------------------------------------------------------------------

class Exchange:
    """One host message with its 0x84 reply and whatever moved on the raw endpoints meanwhile."""

    def __init__(self, message: bytes) -> None:
        self.message = message
        self.blocks = split_host_blocks(message)
        self.reply: Optional[bytes] = None
        self.raw_in: Optional[bytes] = None
        self.raw_out: Optional[bytes] = None

    def block(self, opcode: int) -> Optional[bytes]:
        return next((b for b in self.blocks if b[0] == opcode), None)


def exchanges(name: str) -> List[Exchange]:
    out: List[Exchange] = []
    current: Optional[Exchange] = None
    for transfer in transfers(name):
        if transfer.endpoint == EP_OUT and not transfer.completion and transfer.payload:
            current = Exchange(transfer.payload)
            out.append(current)
        elif current is None:
            continue
        elif transfer.endpoint == EP_IN_RAW and transfer.completion:
            current.raw_in = transfer.payload  # zero-length completions count (§10.1.3)
        elif transfer.endpoint == EP_OUT_RAW and not transfer.completion and transfer.payload:  # not a pipe reset
            current.raw_out = transfer.payload
        elif transfer.endpoint == EP_IN and transfer.completion:
            assert current.reply is None, 'two replies to one message in %s' % name
            current.reply = transfer.payload
    return out


def raw_reads(name: str) -> List[Tuple[int, bool, bool]]:
    """(bytes read, END, EOI) for every 0x0b exchange in ``name``, via our parser."""
    results = []
    for exchange in exchanges(name):
        block = exchange.block(p.OP_READ_RAW)
        if block is None:
            continue
        assert exchange.reply is not None and exchange.raw_in is not None, name
        requested = -int.from_bytes(block[4:8], 'little', signed=True)
        parsed = p.parse_raw_read_reply(exchange.reply, requested, exchange.raw_in)
        results.append((len(parsed.data), parsed.end, parsed.eoi))
    return results


def framed_reads(name: str) -> List[Tuple[int, bool]]:
    """(bytes read, END) for every 0x0a exchange, via ``parse_read_reply`` on its part of the reply."""
    results = []
    for exchange in exchanges(name):
        block = exchange.block(p.OP_READ)
        if block is None:
            continue
        assert exchange.reply is not None
        requested = 0x10000 - int.from_bytes(block[4:6], 'little')
        offset = 0
        for block_id, reply_block in p.split_reply_blocks(exchange.reply):
            if block_id in (p.BLOCK_PAD, p.BLOCK_DATA_15, p.BLOCK_DATA_30, p.BLOCK_READ_STATUS):
                break
            offset += len(reply_block)
        parsed = p.parse_read_reply(exchange.reply[offset:], requested)
        results.append((len(parsed.data), parsed.end))
    return results


class TestReplyParserDecodesNi:
    @pytest.mark.parametrize('name', ALL_PCAPS)
    def test_every_0x84_reply_splits_into_known_blocks(self, name):
        count = 0
        for transfer in transfers(name):
            if transfer.endpoint != EP_IN or not transfer.completion:
                continue
            blocks = p.split_reply_blocks(transfer.payload)
            assert sum(len(b) for _, b in blocks) + 4 == len(transfer.payload), transfer.payload.hex(' ')
            count += 1
        assert count > 0

    def test_trac_chunks(self):
        # §10.1.4: three full 20480-byte chunks, then 328 with END; then :TRAC:POIN:ACT? = 5 bytes.
        assert raw_reads('trac') == [(20480, False, False)] * 3 + [(328, True, True), (5, True, True)]

    def test_counts(self):
        # counts.pcap README: counts <= 65 return exactly the count; >= 100 return all 82 bytes.
        small = [1, 2, 8, 15, 16, 30, 31, 32, 60, 63, 64, 65]
        large = [100, 127, 128, 255, 256, 511, 512, 1023, 1024]
        assert framed_reads('counts') == [(n, False) for n in small] + [(82, True)] * len(large)
        assert raw_reads('counts') == [(82, True, True)] * 2  # 4096 and 20480

    def test_longwrite(self):
        exchange = next(e for e in exchanges('longwrite') if e.block(p.OP_WRITE_RAW) is not None)
        assert exchange.raw_out is not None and len(exchange.raw_out) == 2050
        assert exchange.raw_out.startswith(b'*CLS;') and exchange.raw_out.endswith(b'*CL\r\n')
        assert exchange.reply is not None
        parsed = p.parse_raw_write_reply(exchange.reply)
        assert parsed.transferred(2050) == 2050 and parsed.status.error == 0
        assert raw_reads('longwrite') == [(82, True, True)]  # the *IDN? afterwards

    def test_stb(self):
        polls = [p.parse_serial_poll_reply(e.reply) for e in exchanges('stb')
                 if e.block(p.OP_SERIAL_POLL) is not None and e.reply is not None]
        assert [poll.status_byte for poll in polls] == [0, 0, 0]
        assert all(poll.pad == 24 and poll.status.error == 0 for poll in polls)

    def test_srq(self):
        # *ESR? -> "1\\n" twice, *STB? and friends: five 2-byte answers with END (§10.1.3).
        assert raw_reads('srq') == [(2, True, True)] * 5
        pushes = [p.parse_srq_push(t.payload) for t in transfers('srq')
                  if t.endpoint == EP_INTR and t.completion and t.payload]
        assert [(push.ibsta, push.status_byte) for push in pushes] == [(0x1800, 0x60)]

    def test_srq_poll_explicit_poll_after_the_push_has_rqs_clear(self):
        # §10.4.2: the adapter polled the device itself; viReadSTB then returned 96 from the
        # push and the explicit 0x10 that followed returned 32 (ESB only).
        polls = [p.parse_serial_poll_reply(e.reply).status_byte for e in exchanges('srq_poll')
                 if e.block(p.OP_SERIAL_POLL) is not None and e.reply is not None]
        assert polls == [0x20]
        pushes = [p.parse_srq_push(t.payload).status_byte for t in transfers('srq_poll')
                  if t.endpoint == EP_INTR and t.completion and t.payload]
        assert pushes == [0x60]

    def test_timeouts_on_both_read_paths(self):
        # nolistener.pcap: 0x0b with nothing to read (§10.6.3); partial.pcap and eos.pcap: 0x0a.
        nolistener = [e for e in exchanges('nolistener') if e.block(p.OP_READ_RAW) is not None]
        first = nolistener[0]
        assert first.raw_in == b'' and first.reply is not None
        parsed = p.parse_raw_read_reply(first.reply, 20480, first.raw_in)
        assert parsed.status.error == 0x0A and parsed.data == b'' and not parsed.end
        assert framed_reads('partial') == [(10, False), (72, True), (0, False)]
        eos = framed_reads('eos')
        assert eos[-1] == (0, False) and sum(n for n, _ in eos[:-1]) == 82


# ---------------------------------------------------------------------------
# (c) the choice of instruction (§10.1.1, §10.5.2)
# ---------------------------------------------------------------------------

class AnsweringAdapter:
    """Just enough of an HS for a ``Controller`` to attach and send one data instruction.

    Reads are answered as timed out with nothing read and writes as complete;
    only the opcode the controller chose matters here.
    """

    max_packet_size = 512
    max_packet_size_raw = 512

    def __init__(self) -> None:
        self.opcodes: List[int] = []
        self._reply = b''

    def control_in(self, request, value, index, length, timeout_ms, request_type=0xC0) -> bytes:
        if request == 0x41:
            return bytes.fromhex('4178563412')
        return bytes.fromhex('40010001300102030003960000000000')

    def bulk_out(self, data: bytes, timeout_ms: int) -> None:
        opcode = data[0]
        self.opcodes.append(opcode)
        status = bytes((opcode, 0x01, 0x30, 0x00)) + bytes(4)
        if opcode == p.OP_REGISTER_WRITE:
            self._reply = status + bytes((data[1], 0, 0, 0))
        elif opcode == p.OP_READ:
            self._reply = bytes((p.BLOCK_READ_STATUS, 0x00, 0x20, 0x0A)) + data[4:6] + b'\xff\xff' + bytes((0x60, 0, 0, 0))
        elif opcode == p.OP_READ_RAW:
            self._reply = bytes((opcode, 0x00, 0x64, 0x0A)) + data[4:8] + bytes((0x60, 0, 0, 0))
        else:
            self._reply = status
        self._reply += p.TERMINATION_BLOCK

    def bulk_in(self, length: int, timeout_ms: int) -> bytes:
        return self._reply

    def bulk_out_raw(self, data: bytes, timeout_ms: int) -> int:
        return len(data)

    def bulk_in_raw(self, length: int, timeout_ms: int) -> bytes:
        return b''

    def close(self) -> None:
        pass


def captured_sizes(opcodes: Tuple[int, int]) -> List[Tuple[int, int]]:
    """(opcode, byte count) of every data instruction of the two ``opcodes`` in any capture, deduplicated."""
    found = set()
    for name in ALL_PCAPS:
        for exchange in exchanges(name):
            for block in exchange.blocks:
                if block[0] not in opcodes:
                    continue
                if block[0] == p.OP_READ:
                    size = 0x10000 - int.from_bytes(block[4:6], 'little')
                elif block[0] == p.OP_READ_RAW:
                    size = -int.from_bytes(block[4:8], 'little', signed=True)
                elif block[0] == p.OP_WRITE:
                    size = 0x10000 - int.from_bytes(block[1:3], 'little')
                else:
                    size = -int.from_bytes(block[8:12], 'little', signed=True)
                found.add((block[0], size))
    return sorted(found, key=lambda item: (item[1], item[0]))


def chosen_opcode(operation) -> int:
    adapter = AnsweringAdapter()
    controller = Controller(adapter, t.PID_HS, sleep=lambda s: None)
    controller.attach()
    try:
        operation(controller)
    except p.GpibTimeout:
        pass  # the adapter above answers every read as timed out
    return adapter.opcodes[-1]


class TestControllerChoosesNisInstruction:
    def test_reads(self):
        sizes = captured_sizes((p.OP_READ, p.OP_READ_RAW))
        counts = [count for _, count in sizes]
        # Both sides of the boundary, and the counts the second batch added around it.
        assert {1024, 1025, 2047, 2048, 2049, 4095, 4096, 20480} <= set(counts)
        assert len(set(counts)) == len(counts), 'NI sent one count both ways'
        for ni_opcode, count in sizes:
            ours = chosen_opcode(lambda c: c.read_raw(count, timeout_s=3.0))
            assert ours == ni_opcode, 'read of %d: NI 0x%02x, ours 0x%02x' % (count, ni_opcode, ours)
            assert (count >= RAW_READ_MIN_BYTES) == (ni_opcode == p.OP_READ_RAW)

    def test_writes(self):
        sizes = captured_sizes((p.OP_WRITE, p.OP_WRITE_RAW))
        lengths = [length for _, length in sizes]
        assert {17, 18, 512, 1024, 1025, 2048, 2049, 2050, 2502} <= set(lengths)
        assert len(set(lengths)) == len(lengths), 'NI sent one length both ways'
        for ni_opcode, length in sizes:
            ours = chosen_opcode(lambda c: c.write_raw(bytes(length), timeout_s=3.0))
            assert ours == ni_opcode, 'write of %d: NI 0x%02x, ours 0x%02x' % (length, ni_opcode, ours)
            assert (length >= RAW_WRITE_MIN_BYTES) == (ni_opcode == p.OP_WRITE_RAW)

    def test_a_framed_write_of_any_length_is_the_plain_layout(self):
        # §10.5.2: header, the data inline, zero padding to 4; nothing else changes with length.
        lengths = []
        for exchange in exchanges('write_thresholds'):
            block = exchange.block(p.OP_WRITE)
            if block is None:
                continue
            length = 0x10000 - int.from_bytes(block[1:3], 'little')
            lengths.append(length)
            assert len(block) == _pad4(8 + length) and not any(block[8 + length:])
            assert exchange.reply is not None
            status = p.parse_status_block(next(b for i, b in p.split_reply_blocks(exchange.reply) if i == p.OP_WRITE))
            assert status.error == 0 and status.transferred(length) == length
        assert lengths[:16] == [18, 24, 32, 48, 63, 64, 65, 100, 128, 255, 256, 257, 512, 1024, 1025, 2048]


# ---------------------------------------------------------------------------
# (d) the error paths of raw_errors.pcap (§10.6.5-10.6.7)
# ---------------------------------------------------------------------------

USBD_STATUS_STALL_PID = 0xC0000004
URB_FUNCTION_RESET_PIPE = 0x1E


class TestErrorPaths:
    def test_read_thresholds_all_return_the_same_82_bytes(self):
        assert raw_reads('read_thresholds') == [(82, True, True)] * 9

    def test_raw_write_with_no_listener(self):
        exchange = next(e for e in exchanges('raw_errors') if e.block(p.OP_WRITE_RAW) is not None)
        assert exchange.raw_out is not None and len(exchange.raw_out) == 2502 and exchange.reply is not None
        parsed = p.parse_raw_write_reply(exchange.reply)
        assert parsed.status.error == t.ERR_NO_LISTENER and parsed.count32 == -2502
        assert parsed.transferred(2502) == 0
        assert isinstance(p.error_for_code(parsed.status.error, 'write'), p.NoListener)

    def test_the_refused_data_is_a_stall_and_ni_resets_0x06_then_0x02(self):
        failed = [(x.endpoint, x.usbd_status) for x in transfers('raw_errors') if x.completion and x.usbd_status]
        assert failed == [(EP_OUT_RAW, USBD_STATUS_STALL_PID)]
        resets = [x.endpoint for x in transfers('raw_errors')
                  if x.function == URB_FUNCTION_RESET_PIPE and not x.completion]
        assert resets == [EP_OUT_RAW, EP_OUT]

    def test_no_capture_contains_a_stop_request_or_a_failed_transfer_elsewhere(self):
        for name in ALL_PCAPS:
            assert (0xC0, t.STOP_REQUEST.request) not in control_requests(name), name
            if name != 'raw_errors':
                assert not [x for x in transfers(name) if x.completion and x.usbd_status], name
        assert (0x40, t.SRQ_ACKNOWLEDGE.request) in control_requests('srq')  # the helper does see requests

    def test_raw_read_ended_by_the_adapter(self):
        timed_out = [e for e in exchanges('raw_errors') if e.block(p.OP_READ_RAW) is not None][0]
        assert timed_out.raw_in == b'' and timed_out.reply is not None   # completed, with zero bytes
        parsed = p.parse_raw_read_reply(timed_out.reply, 20480, timed_out.raw_in)
        assert parsed.status.error == t.ERR_TIMEOUT and parsed.data == b'' and not parsed.end
        assert isinstance(p.error_for_code(parsed.status.error, 'read'), p.GpibTimeout)

    def test_the_adapter_expires_later_than_nominal_and_before_the_host_wait(self):
        # §10.6.6: 4.195 s under the 3 s code, for the 0x0b and for the 0x10.
        waits = []
        for opcode in (p.OP_READ_RAW, p.OP_SERIAL_POLL):
            out = next(x for x in transfers('raw_errors') if x.endpoint == EP_OUT and not x.completion
                       and x.payload and any(b[0] == opcode for b in split_host_blocks(x.payload)))
            reply = next(x for x in transfers('raw_errors') if x.endpoint == EP_IN and x.completion and x.ts > out.ts)
            waits.append(reply.ts - out.ts)
        code, limit = p.effective_timeout(2.0)  # the scenario's VI_ATTR_TMO_VALUE
        assert code == 0xFC and limit == 3.0
        assert all(limit < wait < p.host_wait_s(limit, 600.0) for wait in waits), waits
        assert all(abs(wait - 4.196) < 0.01 for wait in waits), waits

    def test_serial_poll_that_timed_out_has_no_result_block(self):
        exchange = next(e for e in exchanges('raw_errors') if e.block(p.OP_SERIAL_POLL) is not None)
        assert exchange.reply is not None and len(exchange.reply) == 32
        assert [i for i, _ in p.split_reply_blocks(exchange.reply)] == [
            p.OP_STATUS_SNAPSHOT, p.BLOCK_SERIAL_POLL_STATUS, p.OP_REGISTER_WRITE]
        parsed = p.parse_serial_poll_reply(exchange.reply)
        assert parsed.status.error == t.ERR_TIMEOUT and parsed.status_byte is None
        assert isinstance(p.error_for_code(parsed.status.error, 'serial poll'), p.GpibTimeout)

    def test_the_session_recovers_in_the_same_capture(self):
        # §10.6.7: after the three failures NI opened address 24 and *IDN? returned its 82 bytes.
        assert raw_reads('raw_errors')[-1] == (82, True, True)

    def test_sad_poll(self):
        poll = next(e for e in exchanges('sad_poll') if e.block(p.OP_SERIAL_POLL) is not None)
        assert poll.block(p.OP_SERIAL_POLL) == p.serial_poll_block(24, 0xFC, sad=1)
        assert poll.reply is not None
        parsed = p.parse_serial_poll_reply(poll.reply)
        assert (parsed.pad, parsed.sad_byte, parsed.status_byte, parsed.status.error) == (24, 0x61, 4, 0)
        commands = [e.block(p.OP_COMMAND)[4:4 + 0x100 - e.block(p.OP_COMMAND)[1]] for e in exchanges('sad_poll')
                    if e.block(p.OP_COMMAND) is not None]
        assert t.addressed_command(24, t.CMD_SDC, sad=1, controller=0) in commands
        assert t.addressed_command(24, t.CMD_GET, sad=1, controller=0) in commands

    def test_ren_device_go_to_local_and_lockout(self):
        commands = [e.block(p.OP_COMMAND)[4:4 + 0x100 - e.block(p.OP_COMMAND)[1]] for e in exchanges('ren_device')
                    if e.block(p.OP_COMMAND) is not None]
        assert commands.count(t.addressed_command(24, t.CMD_GTL, controller=0)) == 2   # ADDRESS_GTL, DEASSERT_GTL
        assert commands.count(bytes((t.CMD_LLO,))) == 2                                # the two LLO modes
        ren_writes = [e.blocks for e in exchanges('ren_device')
                      if any(b == p.register_write_block([t.REN_ON_WRITE]) + b'\x00\x00' for b in e.blocks)]
        assert len(ren_writes) >= 4  # ASSERT, ASSERT_ADDRESS, ASSERT_LLO, ASSERT_ADDRESS_LLO

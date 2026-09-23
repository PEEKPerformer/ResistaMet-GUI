"""Fakes for the NI GPIB-USB driver tests (``resistamet_gui.gpib_usb``).

Transports that stand in for an adapter, from the strictest to the loosest:

* ``ScriptedTransport``: the exact bytes each call must carry and the canned
  reply it returns, in order; anything off-script fails with a hex diff.
* ``QueueingAdapter``: answers every message and keeps each reply queued
  until it is read, as the real pipe does.
* ``TalkingTransport``: one talker holding a message, for reads whose shape
  is not scripted.
* ``AnsweringAdapter``: just enough of an HS for one data instruction.
* ``SimulatedAdapter`` with ``FakeInstrument``: a behavioural HS with
  instruments on the bus, for the pyvisa-py sessions end to end.

Also the reply builders and the script fragments (attach, re-attach,
addressing) the controller tests are written in. The pyvisa-py stand-ins are
in ``gpib_usb_visa``, so that this module imports without pyvisa-py.
"""
from typing import Any, Dict, List, Optional, Tuple

from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t
from resistamet_gui.gpib_usb.controller import BUS_MIN_RATE_BPS, RAW_READ_SLICE_S, SHORT_WAIT_S, Controller
from resistamet_gui.gpib_usb.transport import AdapterInfo, TransportGone, TransportStall, TransportTimeout


def h(text: str) -> bytes:
    return bytes.fromhex(text.replace(' ', ''))


def hex_diff(expected: bytes, actual: bytes) -> str:
    first = next((i for i, (a, b) in enumerate(zip(expected, actual)) if a != b),
                 min(len(expected), len(actual)))
    return ('bulk_out mismatch at offset %d\n  expected: %s\n  actual:   %s\n  %s^'
            % (first, expected.hex(' '), actual.hex(' '), ' ' * (12 + 3 * first)))


class FakeClock:
    """A monotonic clock that moves only when told to: the controller's deadlines, made exact."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScriptedTransport:
    """Steps, in the order the controller must take them:

    ``('out', bytes)`` and ``('raw_out', bytes)`` -- what the next bulk OUT on
    the primary / alternate endpoint must carry; ``('in', bytes_or_exc[,
    expected_length[, elapsed_s]])``, ``('raw_in', ...)`` and ``('intr', ...)``
    -- what the next bulk IN on the primary / alternate / interrupt endpoint
    returns (or raises), and how far it moves ``clock`` first; ``('ctrl',
    params, reply)`` and ``('ctrl_out', params)`` -- the next control
    request; ``('clear_halt', endpoint[, exc])`` -- the next pipe reset.
    """

    max_packet_size = 512
    max_packet_size_raw = 512

    def __init__(self, script: List[Tuple[Any, ...]], clock: Optional[FakeClock] = None) -> None:
        self.script = list(script)
        #: Moved by the ``elapsed_s`` of an IN step; the controller's clock when ``attached`` built it.
        self.clock = clock if clock is not None else FakeClock()
        self.pos = 0
        self.closed = False
        self.sent: List[bytes] = []
        #: Calls the script did not expect. Kept as well as raised: the controller's pipe
        #: reset swallows whatever it is given, and ``assert_done`` must still fail.
        self.off_script: List[str] = []
        #: ('out', opcode, timeout_ms) and ('in', length, timeout_ms) in call order.
        self.timeouts: List[Tuple[str, int, int]] = []

    def _next(self, kind: str, what: str) -> Tuple[Any, ...]:
        if self.pos >= len(self.script):
            self.off_script.append('unexpected %s after the script ended: %s' % (kind, what))
            raise AssertionError(self.off_script[-1])
        step = self.script[self.pos]
        if step[0] != kind:
            self.off_script.append('step %d: expected %r, got %s %s' % (self.pos + 1, step[0], kind, what))
            raise AssertionError(self.off_script[-1])
        self.pos += 1
        return step

    def control_in(self, request, value, index, length, timeout_ms,
                   request_type=t.REQUEST_TYPE_VENDOR_DEVICE) -> bytes:
        step = self._next('ctrl', 'request 0x%02x' % request)
        expected = tuple(step[1])
        actual = (request, value, index, length) + ((request_type,) if len(expected) == 5 else ())
        if expected != actual:
            raise AssertionError('control_in %r, expected %r' % (actual, expected))
        if isinstance(step[2], Exception):
            raise step[2]
        return step[2]

    def control_out(self, request, value, index, data, timeout_ms,
                    request_type=t.REQUEST_TYPE_VENDOR_DEVICE_OUT) -> None:
        step = self._next('ctrl_out', 'request 0x%02x' % request)
        actual = (request_type, request, value, index, data)
        if tuple(step[1]) != actual:
            raise AssertionError('control_out %r, expected %r' % (actual, tuple(step[1])))
        if len(step) > 2 and isinstance(step[2], Exception):
            raise step[2]

    def bulk_out(self, data: bytes, timeout_ms: int) -> None:
        self._out('out', data, timeout_ms)
        self.sent.append(data)
        self.timeouts.append(('out', data[0], timeout_ms))

    def bulk_out_raw(self, data: bytes, timeout_ms: int) -> int:
        """('raw_out', bytes[, exc_or_accepted_count]): the third item, if an int, is the short count returned."""
        step = self._out('raw_out', data, timeout_ms)
        self.timeouts.append(('raw_out', len(data), timeout_ms))
        return step[2] if len(step) > 2 and isinstance(step[2], int) else len(data)

    def _out(self, kind: str, data: bytes, timeout_ms: int) -> Tuple[Any, ...]:
        step = self._next(kind, data[:64].hex(' '))
        if data != step[1]:
            raise AssertionError(hex_diff(step[1][:80], data[:80]))
        if len(step) > 2 and isinstance(step[2], Exception):
            raise step[2]
        return step

    def bulk_in(self, length: int, timeout_ms: int) -> bytes:
        return self._in('in', length, timeout_ms)

    def bulk_in_raw(self, length: int, timeout_ms: int) -> bytes:
        return self._in('raw_in', length, timeout_ms)

    def interrupt_in(self, length: int, timeout_ms: int) -> bytes:
        return self._in('intr', length, timeout_ms)

    def _in(self, kind: str, length: int, timeout_ms: int) -> bytes:
        step = self._next(kind, '%s(%d)' % (kind, length))
        self.timeouts.append((kind, length, timeout_ms))
        if len(step) > 2 and step[2] is not None and step[2] != length:
            raise AssertionError('%s asked for %d bytes, expected %d' % (kind, length, step[2]))
        if len(step) > 3:
            self.clock.advance(step[3])
        if isinstance(step[1], Exception):
            raise step[1]
        if len(step[1]) > length:
            raise AssertionError('reply of %d bytes would overflow the %d-byte buffer'
                                 % (len(step[1]), length))
        return step[1]

    def clear_halt(self, endpoint: int) -> None:
        step = self._next('clear_halt', 'endpoint 0x%02x' % endpoint)
        if step[1] != endpoint:
            raise AssertionError('clear_halt on 0x%02x, expected 0x%02x' % (endpoint, step[1]))
        if len(step) > 2 and isinstance(step[2], Exception):
            raise step[2]

    def close(self) -> None:
        self.closed = True

    def assert_done(self) -> None:
        assert not self.off_script, self.off_script
        remaining = self.script[self.pos:]
        assert not remaining, 'script steps not consumed: %r' % (remaining,)

    def in_timeouts_after(self, opcode: int) -> List[int]:
        """The bulk_in timeouts that followed each bulk_out of ``opcode``, up to the next bulk_out."""
        found = []
        collecting = False
        for kind, value, timeout_ms in self.timeouts:
            if kind == 'out':
                collecting = value == opcode
            elif collecting:
                found.append(timeout_ms)
        return found


def _pad4(n: int) -> int:
    return -(-n // 4) * 4


def split_host_message(message: bytes) -> List[bytes]:
    """The instruction blocks of a host message, each with its padding, up to the termination (§3.1)."""
    blocks: List[bytes] = []
    off = 0
    while message[off] != p.OP_TERMINATION:
        opcode = message[off]
        if opcode in (p.OP_STATUS_SNAPSHOT, p.OP_TAKE_CONTROL, p.OP_GO_TO_STANDBY, p.OP_INTERFACE_CLEAR):
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
    assert message[off:] == p.TERMINATION_BLOCK, message.hex(' ')
    return blocks


# --- reply builders ---------------------------------------------------------

def status_reply(opcode: int, error: int = 0, count: int = 0, ibsta: int = 0x0130) -> bytes:
    return (bytes((opcode,)) + ibsta.to_bytes(2, 'big') + bytes((error,))
            + (count & 0xFFFF).to_bytes(2, 'little') + b'\x00\x00' + h('04 00 00 00'))


def regwrite_reply(completed: int, error: int = 0, ibsta: int = 0x0130) -> bytes:
    return (h('09') + ibsta.to_bytes(2, 'big') + bytes((error,)) + h('00 00 00 00') + bytes((completed, 0, 0, 0))
            + h('04 00 00 00'))


def regread_reply(values: List[int]) -> bytes:
    out = b''
    for start in range(0, len(values), 3):
        chunk = bytes(values[start:start + 3])
        out += bytes((0x34,)) + chunk + b'\x00' * (3 - len(chunk))
    return out + bytes((0x35, len(values), 0, 0)) + h('04 00 00 00')


def read_reply(data: bytes, requested: int, *, end: bool = True, error: int = 0) -> bytes:
    blocks = b''
    for start in range(0, len(data), 15):
        chunk = data[start:start + 15]
        blocks += bytes((0x36,)) + chunk + b'\xee' * (15 - len(chunk))
    last_count = len(data) - ((len(data) - 1) // 15) * 15 if data else 0
    ibsta = 0x0100 | (t.IBSTA_END if end else 0) | (t.IBSTA_TIMO if error == t.ERR_TIMEOUT else 0)
    count = (len(data) - requested) & 0xFFFF
    # The 16-byte trailer as the GPIB-USB-HS sends it (no embedded 0x09 block).
    trailer = (bytes((0x38,)) + ibsta.to_bytes(2, 'big') + bytes((error,))
               + count.to_bytes(2, 'little') + b'\xff\xff'
               + bytes((0xE0 if end else 0x60, last_count, 0, 0)) + h('04 00 00 00'))
    return blocks + trailer


SERIAL_REPLY = h('41 78 56 34 12')
NOT_READY = h('40 01 00 01 30 01 00 00 00 00 00 00 00 00 00 00')
READY = h('40 01 00 01 30 01 02 03 00 03 96 00 00 00 00 00')
STATUS_8 = h('20 01 30 00 00 00 00 00')
STOP = ('ctrl', (0x20, 0, 0, 8), STATUS_8)
DRAIN_LENGTH = p.read_reply_buffer_size(p.MAX_TRANSFER_BYTES, 512)
RAW_DRAIN_LENGTH = p.raw_read_buffer_size(p.MAX_RAW_TRANSFER_BYTES, 512)
#: The §8.2 resync on an HS: stop, drain the primary IN, drain the alternate IN.
RAW_DRAIN = ('raw_in', TransportTimeout('nothing on the alternate endpoint'), RAW_DRAIN_LENGTH)

T3S = 0xFC  # 3 s, the timeout the worked examples use
SHORT_MS = int(SHORT_WAIT_S * 1000)
#: §7.2, §7.3: the larger expiry of the two timed units under the code sent plus 2 s, in
#: whole milliseconds (GPIB-USB-HS 013CC9DF under NI's driver; 01CEE482 under this one).
WAIT_3S_MS = 6196    # 0xfc: 4.196156 s (013CC9DF; 01CEE482 ends at 3.750) + 2 s
WAIT_10S_MS = 22000  # 0xfd: 20.000 s (01CEE482; 013CC9DF ends at 16.778) + 2 s
WAIT_30S_MS = 43250  # 0xfe: 41.250 s (01CEE482; 013CC9DF ends at 33.555) + 2 s


def attach_script(take_control_error: int = 5) -> List[Tuple[Any, ...]]:
    """§2.8 for an HS as system controller: steps 2, 5 and 7 (4 and 6 are optional and skipped)."""
    return [
        ('ctrl', (0x41, 0, 0, 16), SERIAL_REPLY),
        ('ctrl', (0x40, 0, 0, 16), READY),
        ('out', p.register_write_message(t.register_init_writes())), ('in', regwrite_reply(26), 16),
        ('out', p.interface_clear_message()), ('in', status_reply(0x0F), 12),
        ('out', p.register_write_message([t.REN_ON_WRITE])), ('in', regwrite_reply(1), 16),
        ('out', p.take_control_message(True)), ('in', status_reply(0x01, error=take_control_error), 12),
    ]


#: Before a re-attach the bulk pipes are reset: the OUT pair in NI's order (§10.6.5), then the IN pair.
CLEAR_HALTS = [('clear_halt', 0x06), ('clear_halt', 0x02), ('clear_halt', 0x84), ('clear_halt', 0x88)]


#: The stop-and-drain of §8.2 when nothing is queued (raw transfers off).
DRAIN = [STOP, ('in', TransportTimeout('nothing to drain'), DRAIN_LENGTH)]


def reattach_script(take_control_error: int = 5) -> List[Tuple[Any, ...]]:
    """What the operation after a fault does first on an HS: the pipe resets, then §2.8 again.

    After a malformed reply, that is: the stop-and-drain of §8.2 ran when the reply was seen.
    """
    return CLEAR_HALTS + attach_script(take_control_error)


def reattach_after_usb_fault_script(stale: Any = None, take_control_error: int = 5,
                                    raw: bool = False) -> List[Tuple[Any, ...]]:
    """The same after a USB error, which drains nothing when it happens: the stop-and-drain comes
    here, behind the pipe resets, and ``stale`` is the queued reply it finds (None: nothing)."""
    found = TransportTimeout('nothing to drain') if stale is None else stale
    return (CLEAR_HALTS + [STOP, ('in', found, DRAIN_LENGTH)] + ([RAW_DRAIN] if raw else [])
            + attach_script(take_control_error))


def srq_arm(ibsta: int = 0x0064) -> List[Tuple[Any, ...]]:
    """The 12-byte bank-2 0x03 write that arms the service-request push, and its reply (§10.11).

    The reply's ibsta carries SRQI when SRQ is already asserted: 0x1068 on the bench.
    """
    return [('out', p.ni_session_mark_message()), ('in', regwrite_reply(1, ibsta=ibsta), 16)]


def address_listener(pad: int = 22, code: int = T3S) -> List[Tuple[Any, ...]]:
    return [('out', p.command_message(t.address_listener_command(0, pad), code)), ('in', status_reply(0x0C))]


def address_talker(pad: int = 22, code: int = T3S) -> List[Tuple[Any, ...]]:
    return [('out', p.command_message(t.address_talker_command(0, pad), code)), ('in', status_reply(0x0C)),
            ('out', p.go_to_standby_message()), ('in', status_reply(0x06))]


def attached(extra: List[Tuple[Any, ...]], **kwargs) -> Tuple[Controller, ScriptedTransport]:
    """An attached HS over a script; the controller's clock is the script's, still unless a step moves it."""
    transport = ScriptedTransport(attach_script() + extra)
    controller = Controller(transport, t.PID_HS, sleep=lambda s: None, clock=transport.clock, **kwargs)
    controller.attach()
    return controller, transport


def attached_ni(extra: List[Tuple[Any, ...]], **kwargs) -> Tuple[Controller, ScriptedTransport]:
    """``attached`` with NI's instructions (0x0b, 0x0e, 0x10) switched on; a controller leaves them off by default."""
    return attached(extra, ni_instructions=True, **kwargs)


def raw_write_reply(requested: int, transferred: int, *, error: int = 0) -> bytes:
    """Our bare 0x0e reply: the 8-byte status block with its 32-bit count, then termination."""
    count = (transferred - requested).to_bytes(4, 'little', signed=True)
    return bytes((0x0E, 0x00, 0x28, error)) + count + h('04 00 00 00')


def raw_read_reply(requested: int, transferred: int, *, end: bool = True, error: int = 0) -> bytes:
    """Our two-block 0x0b reply: the 0x0b status with its tail, the clear-END write's status, termination."""
    count = (transferred - requested).to_bytes(4, 'little', signed=True)
    ibsta = 0x0064 | (t.IBSTA_END if end else 0)
    block = bytes((0x0B,)) + ibsta.to_bytes(2, 'big') + bytes((error,)) + count + bytes((0xE0 if end else 0x60, 0, 0, 0))
    return block + h('09 00 64 00') + count + h('01 00 00 00') + h('04 00 00 00')


# --- NI's instrument-session messages (§10), the opt-in raw paths ------------

def _regwrite_block(writes: int) -> bytes:
    """The 12-byte status of a register-write block inside a batched reply (§10.2.1)."""
    return h('09 00 64 00 00 00 ff ff') + bytes((writes, 0, 0, 0))


def ni_session(pad: int = 22, code: int = T3S, sad: Optional[int] = None,
               update: bool = False) -> List[Tuple[Any, ...]]:
    """NI's bank-2 session configuration before a raw instruction (§10.2.4): the 32-byte open
    form, or with ``update`` the 12-byte bank-2 0x03 write and the 28-byte update a new timeout
    code sends (§10.10.1); and their replies."""
    if update:
        return [('out', p.ni_session_mark_message()), ('in', _regwrite_block(1) + p.TERMINATION_BLOCK, 512),
                ('out', p.ni_session_update_message(pad, sad, code)),
                ('in', _regwrite_block(1) + _regwrite_block(4) + p.TERMINATION_BLOCK, 512)]
    return [('out', p.ni_session_open_message(pad, sad, code)),
            ('in', h('03 00 30 00 00 00 ff ff') + _regwrite_block(1) + _regwrite_block(4) + p.TERMINATION_BLOCK, 512)]


#: NI's close of the session on an address (§10.3.3) and its reply.
NI_SESSION_CLOSE = [('out', p.ni_session_close_message()),
                    ('in', _regwrite_block(1) + _regwrite_block(1) + p.TERMINATION_BLOCK, 512)]


def ni_read(count: int, code: int = T3S, pad: int = 22, **kwargs: Any) -> bytes:
    """NI's 40-byte read message (§10.1.2) from the controller at address 0."""
    return p.ni_read_raw_message(0, pad, kwargs.pop('sad', None), count, code, **kwargs)


def ni_write(length: int, code: int = T3S, pad: int = 22, eoi: bool = True, e: Optional[int] = None,
             sad: Optional[int] = None) -> bytes:
    """NI's 36-byte write message (§10.5.2) from the controller at address 0."""
    return p.ni_write_raw_message(0, pad, sad, length, code, eoi, e)


def ni_raw_read_reply(requested: int, transferred: int, *, end: bool = True, error: int = 0,
                      command_error: int = 0) -> bytes:
    """NI's 56-byte reply to its read message: snapshot, addressing, 0x0b with its tail, two writes."""
    count = (transferred - requested).to_bytes(4, 'little', signed=True)
    ibsta = 0x0064 | (t.IBSTA_END if end else 0)
    block = bytes((0x0B,)) + ibsta.to_bytes(2, 'big') + bytes((error,)) + count + bytes((0xE0 if end else 0x60, 0, 0, 0))
    return (h('03 00 28 00 00 00 ff ff') + bytes((0x0C, 0x00, 0x74, command_error)) + h('00 00 ff ff') + block
            + _regwrite_block(1) + _regwrite_block(1) + p.TERMINATION_BLOCK)


def ni_raw_write_reply(requested: int, transferred: int, *, error: int = 0) -> bytes:
    """NI's 40-byte reply to its write message: snapshot, addressing, 0x0e, one write."""
    count = (transferred - requested).to_bytes(4, 'little', signed=True)
    return (h('03 00 30 00 00 00 ff ff 0c 00 38 00 00 00 ff ff') + bytes((0x0E, 0x00, 0x28, error)) + count
            + _regwrite_block(1) + p.TERMINATION_BLOCK)


def raw_wait_ms(count: int, base_ms: int = WAIT_3S_MS) -> int:
    return base_ms + count * 1000 // BUS_MIN_RATE_BPS


def raw_wait_slices(total_ms: int, buffer: int, partial: bytes = b'') -> List[Tuple[Any, ...]]:
    """A 0x88 wait of ``total_ms`` that brings nothing: every slice times out, and so does every
    look at the primary IN between two slices. ``partial`` arrives with the last slice."""
    steps: List[Tuple[Any, ...]] = []
    taken = 0
    while total_ms > 0:
        slice_ms = min(int(RAW_READ_SLICE_S * 1000), total_ms)
        total_ms -= slice_ms
        last = total_ms == 0
        steps.append(('raw_in', TransportTimeout('nothing yet', partial=partial if last else b''), buffer - taken))
        if not last:
            steps.append(('in', TransportTimeout('no reply yet'), 512))
    return steps


class TalkingTransport:
    """An adapter with one talker holding ``message``, for reads whose shape is not scripted.

    Answers the attach and the addressing with plain successes, and every
    0x0a with the next ``count`` bytes of the message -- END with the last
    of them, a device timeout (error 0x0a, nothing read) once it is empty.
    Records the count of every 0x0a and the host wait of every bulk IN.
    """

    max_packet_size = 512
    max_packet_size_raw = 512

    def __init__(self, message: bytes) -> None:
        self.pending = message
        self.reply = b''
        self.sent: List[bytes] = []
        self.read_counts: List[int] = []
        self.in_timeouts: List[int] = []

    def control_in(self, request, value, index, length, timeout_ms, request_type=0xC0) -> bytes:
        return SERIAL_REPLY if request == 0x41 else READY

    def bulk_out(self, data: bytes, timeout_ms: int) -> None:
        self.sent.append(data)
        opcode = data[0]
        if opcode == p.OP_REGISTER_WRITE:
            self.reply = regwrite_reply(data[1])
        elif opcode == p.OP_READ:
            count = 0x10000 - int.from_bytes(data[4:6], 'little')
            self.read_counts.append(count)
            if not self.pending:
                self.reply = read_reply(b'', count, end=False, error=t.ERR_TIMEOUT)
            else:
                out, self.pending = self.pending[:count], self.pending[count:]
                self.reply = read_reply(out, count, end=not self.pending)
        else:
            self.reply = status_reply(opcode)

    def bulk_in(self, length: int, timeout_ms: int) -> bytes:
        self.in_timeouts.append(timeout_ms)
        assert len(self.reply) <= length, 'reply of %d bytes would overflow the %d-byte buffer' % (len(self.reply), length)
        reply, self.reply = self.reply, b''
        return reply

    def close(self) -> None:
        pass


def talking(message: bytes) -> Tuple[Controller, TalkingTransport]:
    transport = TalkingTransport(message)
    controller = Controller(transport, t.PID_HS, sleep=lambda s: None, clock=FakeClock())
    controller.attach()
    return controller, transport


class QueueingAdapter:
    """A fake whose replies stay queued until they are read, as they do in the real pipe.

    Every message is answered; ``fail_in`` / ``fail_stop`` make the next reply read / stop
    request raise once, leaving the reply it did not deliver in the queue.
    """

    max_packet_size = 512
    max_packet_size_raw = 512

    def __init__(self) -> None:
        self.queue: List[bytes] = []
        self.opcodes: List[int] = []
        self.written: List[bytes] = []
        self.fail_in: Optional[Exception] = None
        self.fail_stop: Optional[Exception] = None

    def control_in(self, request, value, index, length, timeout_ms, request_type=0xC0) -> bytes:
        if request == 0x20 and self.fail_stop is not None:
            failure, self.fail_stop = self.fail_stop, None
            raise failure
        return {0x41: SERIAL_REPLY, 0x40: READY}.get(request, STATUS_8)

    def bulk_out(self, data: bytes, timeout_ms: int) -> None:
        opcode = data[0]
        self.opcodes.append(opcode)
        if opcode == p.OP_REGISTER_WRITE:
            self.queue.append(regwrite_reply(data[1]))
        else:
            if opcode == p.OP_WRITE:
                self.written.append(data[8:8 + 0x10000 - int.from_bytes(data[1:3], 'little')])
            self.queue.append(status_reply(opcode))

    def bulk_in(self, length: int, timeout_ms: int) -> bytes:
        if self.fail_in is not None:
            failure, self.fail_in = self.fail_in, None
            raise failure
        if not self.queue:
            raise TransportTimeout('nothing queued')
        return self.queue.pop(0)

    def clear_halt(self, endpoint: int) -> None:
        pass

    def close(self) -> None:
        pass


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
    max_packet_size_raw = 512

    def __init__(self, instruments: Dict[int, FakeInstrument], serial_reply: bytes = h('41 78 56 34 12')) -> None:
        self.instruments = instruments
        self.serial_reply = serial_reply
        self.listening: List[int] = []
        self.talker: Optional[int] = None
        #: Between SPE and SPD the addressed talker answers with its status byte (§5.9).
        self.serial_poll_mode = False
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
        #: (length, EOI, replies of the blocks before it, blocks after it) of the 0x0e whose
        #: bytes the next bulk_out_raw must bring.
        self.pending_raw_write: Optional[Tuple[int, bool, bytes, List[bytes]]] = None
        #: Bank-2 registers as the last writes left them (§10.2.4).
        self.bank2: Dict[int, int] = {}
        self.raw_writes: List[bytes] = []
        #: Endpoints left halted by a STALL, and every pipe reset asked for, in order.
        self.halted: set = set()
        self.halts_cleared: List[int] = []
        self.messages: List[bytes] = []
        self.control_requests: List[int] = []
        self.bulk_in_timeouts: List[int] = []
        self.raw_in_timeouts: List[int] = []
        self.closed = False
        #: Raised by the next bulk_out, once.
        self.fail_next: Optional[Exception] = None
        #: Raised by the next control_in, once.
        self.fail_next_control: Optional[Exception] = None
        #: Answer a read that times out as GPIB-USB-HS 01CEE482 does (§5.2): one 0x36 block
        #: of stale bytes and min(requested, 15) in the last-block count, nothing read. Off,
        #: the form NI's captures show: no data block and a stale last-block byte.
        self.stale_timeout_block = False
        #: The talker's message ends without EOI on its last byte: the read that drains it
        #: reports no END, and the next read finds nothing and times out.
        self.withhold_eoi = False
        #: Set by a test to pull the cable: every USB call from then on raises TransportGone,
        #: as libusb's "no such device" does (§11.2), and is counted here.
        self.unplugged = False
        self.calls_while_unplugged = 0

    def _plugged(self) -> None:
        if self.unplugged:
            self.calls_while_unplugged += 1
            raise TransportGone('the device is no longer on the USB bus')

    def control_in(self, request, value, index, length, timeout_ms, request_type=0xC0) -> bytes:
        self._plugged()
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
        """One message, block by block in message order, one reply per block (§10.2.1)."""
        self._plugged()
        if self.fail_next is not None:
            failure, self.fail_next = self.fail_next, None
            raise failure
        self.messages.append(data)
        blocks = split_host_message(data)
        replies: List[bytes] = []
        for index, block in enumerate(blocks):
            if block[0] == p.OP_WRITE_RAW:
                # §10.5.2: the header now, the bytes on the alternate OUT next; the reply to the
                # 0x0e and to the blocks behind it only once those have arrived.
                length = -int.from_bytes(block[8:12], 'little', signed=True)
                self.pending_raw_write = (length, bool(block[6] & 0x08), b''.join(replies), blocks[index + 1:])
                return
            replies.append(self._block(block))
            if block[0] == p.OP_READ:
                break  # the two writes embedded in a 0x0a draw no status on the bench unit (§5.2)
        self.reply = b''.join(replies) + p.TERMINATION_BLOCK

    def _block(self, block: bytes) -> bytes:
        """The reply to one instruction block, without the termination block."""
        opcode = block[0]
        if opcode in (p.OP_TAKE_CONTROL, p.OP_INTERFACE_CLEAR):
            self.atn = True
            return self._status(opcode)
        if opcode == p.OP_GO_TO_STANDBY:
            self.atn = False
            return self._status(opcode)
        if opcode == p.OP_STATUS_SNAPSHOT:
            return self._status(opcode, ibsta=self.ibsta())
        if opcode == p.OP_REGISTER_WRITE:
            for start in range(3, 3 + 3 * block[1], 3):
                bank, addr, value = block[start:start + 3]
                if (bank, addr, value) == t.REN_ON_WRITE:
                    self.ren = True
                elif (bank, addr, value) == t.REN_OFF_WRITE:
                    self.ren = False
                elif bank == 2:
                    self.bank2[addr] = value
            return self._status(opcode) + bytes((block[1], 0, 0, 0))
        if opcode == p.OP_REGISTER_READ:
            return bytes((0x34, self.bus_lines(), 0, 0, 0x35, 1, 0, 0))
        if opcode == p.OP_COMMAND:
            return self._command(block)
        if opcode == p.OP_WRITE:
            return self._write(block)
        if opcode == p.OP_READ:
            return self._read(block)
        if opcode == p.OP_READ_RAW:
            return self._read_raw(block)
        if opcode == p.OP_SERIAL_POLL:
            return self._serial_poll(block)
        raise AssertionError('unexpected opcode 0x%02x' % opcode)

    def _command(self, data: bytes) -> bytes:
        count = 0x100 - data[1]
        command_bytes = data[4:4 + count]
        if not self.instruments:
            return self._status(p.OP_COMMAND, error=5, count=-count)
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
            elif byte == t.CMD_SPE:
                self.serial_poll_mode = True
            elif byte == t.CMD_SPD:
                self.serial_poll_mode = False
            elif byte == t.CMD_SDC:
                for pad in self.listening:
                    self.instruments[pad].cleared += 1
                    self.instruments[pad].pending = b''
            elif byte == t.CMD_DCL:
                for instrument in self.instruments.values():
                    instrument.cleared += 1
                    instrument.pending = b''
        return self._status(p.OP_COMMAND)

    def _write(self, data: bytes) -> bytes:
        length = 0x10000 - int.from_bytes(data[1:3], 'little')
        payload = data[8:8 + length]
        if not self.listening:
            return self._status(p.OP_WRITE, error=8, count=-length)
        self.atn = False
        for pad in self.listening:
            self.instruments[pad].accept(payload, bool(data[6] & 0x08))
        return self._status(p.OP_WRITE)

    def _serial_poll(self, data: bytes) -> bytes:
        """0x10 (§10.5.4): ``3a P S sb`` then a 0x39 status block; the latter alone, with error
        0x0a, for an absent device."""
        pad, sad_byte = data[4], data[5]
        instrument = self.instruments.get(pad)
        self.atn = True  # the adapter addresses the bus itself
        if instrument is None:
            # §10.6.6: a poll that times out is answered without the 0x3a block.
            return self._status(0x39, error=0x0A, ibsta=0x0074)
        return bytes((0x3A, pad, sad_byte, instrument.status_byte)) + self._status(0x39, ibsta=0x0074)

    def _write_raw(self, payload: bytes, eoi: bool) -> bytes:
        """The 0x0e block's reply once the bytes have arrived: an 8-byte status with a 32-bit count."""
        self.atn = False
        for pad in self.listening:
            self.instruments[pad].accept(payload, eoi)
        return bytes((p.OP_WRITE_RAW, 0x00, 0x28, 0x00)) + h('00 00 00 00')

    def _talker_output(self, requested: int, eos_mode: int, eos_char: int) -> Optional[Tuple[bytes, bool]]:
        """What the addressed talker gives up for one read: (bytes, END), or None when nothing is pending."""
        instrument = self.instruments.get(self.talker) if self.talker is not None else None
        if instrument is not None and self.serial_poll_mode:
            return bytes((instrument.status_byte,)), False
        if instrument is None or not instrument.pending:
            return None
        source = instrument.pending
        if eos_mode & 0x04 and bytes((eos_char,)) in source:
            cut = source.index(bytes((eos_char,))) + 1
        else:
            cut = len(source)
        cut = min(cut, requested)
        out, instrument.pending = source[:cut], source[cut:]
        end = ((not instrument.pending and not self.withhold_eoi)
               or bool(eos_mode & 0x04 and out.endswith(bytes((eos_char,)))))
        return out, end

    def _read_raw(self, data: bytes) -> bytes:
        """0x0b (§10.1.2-10.1.3): the bytes go to the alternate IN, the 12-byte 0x0b block comes
        back on the primary. Like NI's, it releases ATN itself: no 0x06 is needed after the 0x0c."""
        requested = -int.from_bytes(data[4:8], 'little', signed=True)
        self.atn = False
        result = self._talker_output(requested, data[1], data[2])
        if result is None:
            out, end, error = b'', False, 0x0A
        else:
            (out, end), error = result, 0
        self.raw_reply = out
        count = (len(out) - requested).to_bytes(4, 'little', signed=True)
        status = bytes((p.OP_READ_RAW,)) + (0x2064 if end else 0x0064).to_bytes(2, 'big') + bytes((error,))
        return status + count + bytes((0xE0 if end else 0x60, 0, 0, 0))

    def _read(self, data: bytes) -> bytes:
        """0x0a: data blocks and the 16-byte trailer as the bench unit sends it, less its termination."""
        requested = 0x10000 - int.from_bytes(data[4:6], 'little')
        eos_mode, eos_char = data[1], data[2]
        if self.atn:
            return self._status(0x38, error=2, count=-requested) + h('60 00 00 00')
        result = self._talker_output(requested, eos_mode, eos_char)
        if result is None:
            status = self._status(0x38, error=0x0A, count=-requested, ibsta=0x0020)
            if self.stale_timeout_block and requested <= 15:
                return h('36 00 20 00 aa 55 ff ff 04 00 00 00 04 00 00 00') + status + bytes((0xE0, requested, 0, 0))
            return status + h('e0 5e 00 00')
        out, end = result
        blocks = b''
        for start in range(0, len(out), 15):
            chunk = out[start:start + 15]
            blocks += bytes((0x36,)) + chunk + b'\xee' * (15 - len(chunk))
        last_count = len(out) - ((len(out) - 1) // 15) * 15 if out else 0
        status = self._status(0x38, count=len(out) - requested,
                              ibsta=0x2100 if end else 0x0100)
        return blocks + status + bytes((0xE0 if end else 0x60, last_count, 0, 0))

    def bulk_in(self, length: int, timeout_ms: int) -> bytes:
        self._plugged()
        self.bulk_in_timeouts.append(timeout_ms)
        assert len(self.reply) <= length, 'reply of %d bytes would overflow %d' % (len(self.reply), length)
        reply, self.reply = self.reply, b''
        return reply

    # The alternate pair and the interrupt endpoint; behaviour is added with the
    # instructions that use them.
    def bulk_out_raw(self, data: bytes, timeout_ms: int) -> int:
        self._plugged()
        if 0x06 in self.halted:
            raise TransportStall('raw bulk write was refused with a STALL')
        assert self.pending_raw_write is not None, 'raw bulk OUT with no 0x0e outstanding'
        length, eoi, before, after = self.pending_raw_write
        assert len(data) == length, 'the 0x0e announced %d bytes, %d arrived' % (length, len(data))
        self.pending_raw_write = None
        if not self.listening:
            # §10.6.5: the data is refused with a STALL, the endpoint stays halted until it is
            # reset, and the reply with error 8 and the whole count comes by itself.
            self.halted.add(0x06)
            count = (-length).to_bytes(4, 'little', signed=True)
            self.reply = (before + bytes((p.OP_WRITE_RAW, 0x00, 0x28, 0x08)) + count
                          + b''.join(self._block(block) for block in after) + p.TERMINATION_BLOCK)
            raise TransportStall('raw bulk write was refused with a STALL')
        self.raw_writes.append(data)
        self.reply = (before + self._write_raw(data, eoi) + b''.join(self._block(block) for block in after)
                      + p.TERMINATION_BLOCK)
        return len(data)

    def clear_halt(self, endpoint: int) -> None:
        self._plugged()
        self.halted.discard(endpoint)
        self.halts_cleared.append(endpoint)

    def bulk_in_raw(self, length: int, timeout_ms: int) -> bytes:
        self._plugged()
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
        """Every message whose first block is ``opcode``."""
        return [m for m in self.messages if m[0] == opcode]

    def blocks(self, opcode: int) -> List[bytes]:
        """Every block of ``opcode`` in every message, in order: NI's messages lead with 0x03."""
        return [block for m in self.messages for block in split_host_message(m) if block[0] == opcode]


def fake_adapter_info(serial: Optional[str] = '01234567', bus: int = 20, address: int = 5) -> AdapterInfo:
    return AdapterInfo(model='GPIB-USB-HS', vendor_id=t.VENDOR_ID, product_id=t.PID_HS, bus=bus,
                       address=address, serial=serial, endpoint_out=0x02, endpoint_in=0x84,
                       endpoint_interrupt=0x81, needs_firmware=False, device=None)

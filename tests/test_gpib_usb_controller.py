"""Controller and device_ops sequencing over a scripted transport.

The fake is a script: the exact bytes each ``bulk_out`` must carry and the
canned bytes each ``bulk_in`` / ``control_in`` returns, in order. Anything
off-script fails with a hex diff, so a wrong byte in an addressing sequence
reads as "expected 3f 40 36, got 3f 40 38 at offset 6", not as a hang. The
fake also records the timeout every bulk call was given, so the host-wait
rule of §7.2 is checked, not assumed.
"""
from typing import Any, List, Tuple

import pytest

from resistamet_gui.gpib_usb import device_ops as ops
from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t
from resistamet_gui.gpib_usb.controller import DRAIN_WAIT_S, RECOVERY_WAIT_S, SHORT_WAIT_S, Controller
from resistamet_gui.gpib_usb.protocol import AdapterNotReady, GpibError, GpibTimeout, NoListener, ProtocolError
from resistamet_gui.gpib_usb.transport import TransportError, TransportTimeout


def h(text: str) -> bytes:
    return bytes.fromhex(text.replace(' ', ''))


def hex_diff(expected: bytes, actual: bytes) -> str:
    first = next((i for i, (a, b) in enumerate(zip(expected, actual)) if a != b),
                 min(len(expected), len(actual)))
    return ('bulk_out mismatch at offset %d\n  expected: %s\n  actual:   %s\n  %s^'
            % (first, expected.hex(' '), actual.hex(' '), ' ' * (12 + 3 * first)))


class ScriptedTransport:
    """Steps: ('out', bytes) | ('in', bytes_or_exc[, expected_length]) | ('ctrl', params, reply)."""

    max_packet_size = 512

    def __init__(self, script: List[Tuple[Any, ...]]) -> None:
        self.script = list(script)
        self.pos = 0
        self.closed = False
        self.sent: List[bytes] = []
        #: ('out', opcode, timeout_ms) and ('in', length, timeout_ms) in call order.
        self.timeouts: List[Tuple[str, int, int]] = []

    def _next(self, kind: str, what: str) -> Tuple[Any, ...]:
        if self.pos >= len(self.script):
            raise AssertionError('unexpected %s after the script ended: %s' % (kind, what))
        step = self.script[self.pos]
        self.pos += 1
        if step[0] != kind:
            raise AssertionError('step %d: expected %r, got %s %s' % (self.pos, step[0], kind, what))
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

    def bulk_out(self, data: bytes, timeout_ms: int) -> None:
        step = self._next('out', data.hex(' '))
        if data != step[1]:
            raise AssertionError(hex_diff(step[1], data))
        self.sent.append(data)
        self.timeouts.append(('out', data[0], timeout_ms))

    def bulk_in(self, length: int, timeout_ms: int) -> bytes:
        step = self._next('in', 'bulk_in(%d)' % length)
        self.timeouts.append(('in', length, timeout_ms))
        if len(step) > 2 and step[2] != length:
            raise AssertionError('bulk_in asked for %d bytes, expected %d' % (length, step[2]))
        if isinstance(step[1], Exception):
            raise step[1]
        if len(step[1]) > length:
            raise AssertionError('reply of %d bytes would overflow the %d-byte buffer'
                                 % (len(step[1]), length))
        return step[1]

    def close(self) -> None:
        self.closed = True

    def assert_done(self) -> None:
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


# --- reply builders ---------------------------------------------------------

def status_reply(opcode: int, error: int = 0, count: int = 0, ibsta: int = 0x0130) -> bytes:
    return (bytes((opcode,)) + ibsta.to_bytes(2, 'big') + bytes((error,))
            + (count & 0xFFFF).to_bytes(2, 'little') + b'\x00\x00' + h('04 00 00 00'))


def regwrite_reply(completed: int, error: int = 0) -> bytes:
    return (h('09 01 30') + bytes((error,)) + h('00 00 00 00') + bytes((completed, 0, 0, 0))
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
    trailer = (bytes((0x38,)) + ibsta.to_bytes(2, 'big') + bytes((error,))
               + count.to_bytes(2, 'little') + b'\x00\x00' + bytes((0, last_count, 0, 0))
               + h('09 00 00 00 00 00 00 00 02 00 00 00 04 00 00 00'))
    return blocks + trailer


SERIAL_REPLY = h('41 78 56 34 12')
NOT_READY = h('40 01 00 01 30 01 00 00 00 00 00 00 00 00 00 00')
READY = h('40 01 00 01 30 01 02 03 00 03 96 00 00 00 00 00')
STATUS_8 = h('20 01 30 00 00 00 00 00')
STOP = ('ctrl', (0x20, 0, 0, 8), STATUS_8)
DRAIN_LENGTH = p.read_reply_buffer_size(p.MAX_TRANSFER_BYTES, 512)

T3S = 0xFC  # 3 s, the timeout the worked examples use
SHORT_MS = int(SHORT_WAIT_S * 1000)
WAIT_3S_MS = 5000   # 3 s row + max(2, 1.5)


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


def address_listener(pad: int = 22, code: int = T3S) -> List[Tuple[Any, ...]]:
    return [('out', p.command_message(t.address_listener_command(0, pad), code)), ('in', status_reply(0x0C))]


def address_talker(pad: int = 22, code: int = T3S) -> List[Tuple[Any, ...]]:
    return [('out', p.command_message(t.address_talker_command(0, pad), code)), ('in', status_reply(0x0C)),
            ('out', p.go_to_standby_message()), ('in', status_reply(0x06))]


def attached(extra: List[Tuple[Any, ...]], **kwargs) -> Tuple[Controller, ScriptedTransport]:
    transport = ScriptedTransport(attach_script() + extra)
    controller = Controller(transport, t.PID_HS, sleep=lambda s: None, **kwargs)
    controller.attach()
    return controller, transport


# ---------------------------------------------------------------------------
# attach / close
# ---------------------------------------------------------------------------

class TestAttach:
    def test_hs_attach_sequence_in_order(self):
        controller, transport = attached([])
        transport.assert_done()
        assert controller.serial_number == 0x12345678
        # The worked-example bytes for REN on appear verbatim in step 7.
        assert h('09 01 00 01 0a 1f 00 00 04 00 00 00') in transport.sent

    def test_short_wait_on_attach_replies(self):
        _, transport = attached([])
        assert [tm for kind, _, tm in transport.timeouts] == [SHORT_MS] * 8

    def test_readiness_poll_repeats_until_ready(self):
        naps: List[float] = []
        script = [('ctrl', (0x41, 0, 0, 16), SERIAL_REPLY)]
        script += [('ctrl', (0x40, 0, 0, 16), NOT_READY)] * 3
        script += [('ctrl', (0x40, 0, 0, 16), TransportTimeout('no answer'))]
        script += [('ctrl', (0x40, 0, 0, 16), READY)] + attach_script()[2:]
        controller = Controller(ScriptedTransport(script), t.PID_HS, sleep=naps.append)
        controller.attach()
        assert naps == [0.1] * 4

    def test_never_ready_raises_after_50_polls(self):
        script = [('ctrl', (0x41, 0, 0, 16), SERIAL_REPLY)]
        script += [('ctrl', (0x40, 0, 0, 16), NOT_READY)] * 50
        transport = ScriptedTransport(script)
        controller = Controller(transport, t.PID_HS, sleep=lambda s: None)
        with pytest.raises(AdapterNotReady):
            controller.attach()
        transport.assert_done()

    def test_wrong_serial_echo_raises(self):
        controller = Controller(ScriptedTransport([('ctrl', (0x41, 0, 0, 16), h('40 00 00 00 00'))]),
                                t.PID_HS, sleep=lambda s: None)
        with pytest.raises(AdapterNotReady):
            controller.attach()

    def test_register_init_must_complete_26_writes(self):
        script = attach_script()[:4]
        script[3] = ('in', regwrite_reply(24), 16)
        script += [STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH)]  # §8.2 resync
        transport = ScriptedTransport(script)
        controller = Controller(transport, t.PID_HS, sleep=lambda s: None)
        with pytest.raises(ProtocolError):
            controller.attach()
        transport.assert_done()

    def test_take_control_error_5_is_tolerated_but_error_3_is_not(self):
        attached([])  # error 5 scripted by default
        script = attach_script(take_control_error=3)
        with pytest.raises(GpibError) as info:
            Controller(ScriptedTransport(script), t.PID_HS, sleep=lambda s: None).attach()
        assert info.value.code == 3

    def test_not_system_controller_skips_step_7(self):
        script = attach_script()[:4]
        script[2] = ('out', p.register_write_message(t.register_init_writes(system_controller=False)))
        transport = ScriptedTransport(script)
        Controller(transport, t.PID_HS, sleep=lambda s: None).attach(system_controller=False)
        transport.assert_done()

    def test_hs_plus_extras_follow_the_readiness_poll(self):
        script = [
            ('ctrl', (0x41, 0, 0, 16), SERIAL_REPLY + bytes(11)),
            ('ctrl', (0x40, 0, 0, 16), h('40 00 00 00 00 00 0f 00 00 30 00 00 00 00 00 00')),
            ('ctrl', (0x48, 0, 0, 16, 0xC0), h('48 f3 30 00 00 00 00 00 00 00 00 00 00 00 00 00')),
            ('ctrl', (0x4B, 1, 0, 2, 0xC0), h('4b 00')),
            ('ctrl', (0xF8, 0, 1, 9, 0xC1), h('f8 01 00 00 00 01 00 00 00')),
        ] + attach_script()[2:]
        transport = ScriptedTransport(script)
        Controller(transport, t.PID_HS_PLUS, sleep=lambda s: None).attach()
        transport.assert_done()

    def test_usb_b_reads_its_serial_from_registers(self):
        script = [
            ('out', p.register_read_message(t.USB_B_SERIAL_REGISTERS)),
            ('in', regread_reply([0x78, 0x56, 0x34, 0x12]), 32),
        ] + attach_script()[2:]
        transport = ScriptedTransport(script)
        controller = Controller(transport, t.PID_USB_B, sleep=lambda s: None)
        controller.attach()
        transport.assert_done()
        assert controller.serial_number == 0x12345678

    def test_unsupported_and_firmwareless_devices_are_refused(self):
        with pytest.raises(ValueError):
            Controller(ScriptedTransport([]), 0x1234)
        with pytest.raises(AdapterNotReady):
            Controller(ScriptedTransport([]), t.PID_USB_B_PRE_FIRMWARE)

    def test_operations_before_attach_raise(self):
        controller = Controller(ScriptedTransport([]), t.PID_HS)
        with pytest.raises(AdapterNotReady):
            controller.write(22, b'x', timeout_s=1.0)
        with pytest.raises(AdapterNotReady):
            controller.read(22, max_bytes=1, timeout_s=1.0)

    def test_attach_is_idempotent(self):
        controller, transport = attached([])
        controller.attach()
        transport.assert_done()

    def test_close_sends_shutdown_writes_and_releases(self):
        controller, transport = attached([
            ('out', p.register_write_message(t.SHUTDOWN_WRITES)), ('in', regwrite_reply(2), 16),
        ])
        controller.close()
        controller.close()
        transport.assert_done()
        assert transport.closed
        with pytest.raises(AdapterNotReady):
            controller.write(22, b'x', timeout_s=1.0)

    def test_close_still_releases_when_the_shutdown_write_fails(self):
        controller, transport = attached([
            ('out', p.register_write_message(t.SHUTDOWN_WRITES)), ('in', regwrite_reply(2, error=3), 16),
        ])
        controller.close()
        transport.assert_done()
        assert transport.closed

    def test_close_still_releases_when_the_shutdown_reply_is_malformed(self):
        controller, transport = attached([
            ('out', p.register_write_message(t.SHUTDOWN_WRITES)), ('in', h('09 00'), 16),
        ])
        controller.close()
        assert transport.closed

    def test_close_without_attach_only_releases(self):
        transport = ScriptedTransport([])
        Controller(transport, t.PID_HS).close()
        assert transport.closed


# ---------------------------------------------------------------------------
# write / read
# ---------------------------------------------------------------------------

class TestWrite:
    def test_write_with_eoi_uses_the_worked_example_bytes(self):
        controller, transport = attached([
            ('out', h('0c fd 00 fc 3f 40 36 00 04 00 00 00')), ('in', status_reply(0x0C), 12),
            ('out', h('0d fa ff fc 00 00 08 00 2a 49 44 4e 3f 0a 00 00 04 00 00 00')),
            ('in', status_reply(0x0D), 12),
        ])
        assert controller.write(22, b'*IDN?\n', timeout_s=3.0) == 6
        transport.assert_done()

    def test_write_without_eoi_and_with_secondary_address(self):
        controller, transport = attached([
            ('out', p.command_message(bytes((0x3F, 0x40, 0x36, 0x62)), T3S)), ('in', status_reply(0x0C)),
            ('out', p.write_message(b'AB', T3S, send_eoi=False)), ('in', status_reply(0x0D)),
        ])
        assert controller.write(22, b'AB', sad=2, send_eoi=False, timeout_s=3.0) == 2
        transport.assert_done()

    def test_empty_write_touches_nothing(self):
        controller, transport = attached([])
        assert controller.write(22, b'', timeout_s=3.0) == 0
        transport.assert_done()

    def test_error_8_raises_no_listener(self):
        controller, transport = attached(address_listener() + [
            ('out', p.write_message(b'*IDN?\n', T3S, True)), ('in', status_reply(0x0D, error=8, count=-6)),
        ])
        with pytest.raises(NoListener) as info:
            controller.write(22, b'*IDN?\n', timeout_s=3.0)
        assert info.value.code == 8
        transport.assert_done()

    def test_error_5_on_addressing_raises_no_listener(self):
        controller, _ = attached([
            ('out', p.command_message(t.address_listener_command(0, 22), T3S)),
            ('in', status_reply(0x0C, error=5, count=-3)),
        ])
        with pytest.raises(NoListener) as info:
            controller.write(22, b'x', timeout_s=3.0)
        assert info.value.code == 5

    def test_readdress_false_skips_a_repeat_addressing(self):
        controller, transport = attached(address_listener() + [
            ('out', p.write_message(b'A', T3S, True)), ('in', status_reply(0x0D)),
            ('out', p.write_message(b'B', T3S, True)), ('in', status_reply(0x0D)),
        ])
        controller.write(22, b'A', timeout_s=3.0, readdress=False)
        controller.write(22, b'B', timeout_s=3.0, readdress=False)
        transport.assert_done()

    def test_a_failure_forgets_who_was_addressed(self):
        controller, transport = attached(address_listener() + [
            ('out', p.write_message(b'A', T3S, True)), ('in', status_reply(0x0D, error=8, count=-1)),
        ] + address_listener() + [
            ('out', p.write_message(b'B', T3S, True)), ('in', status_reply(0x0D)),
        ])
        with pytest.raises(NoListener):
            controller.write(22, b'A', timeout_s=3.0, readdress=False)
        controller.write(22, b'B', timeout_s=3.0, readdress=False)
        transport.assert_done()

    def test_large_writes_split_at_0xffff_with_eoi_on_the_last(self):
        data = bytes(0xFFFF) + b'Z'
        controller, transport = attached(address_listener() + [
            ('out', p.write_message(bytes(0xFFFF), T3S, send_eoi=False)), ('in', status_reply(0x0D)),
            ('out', p.write_message(b'Z', T3S, send_eoi=True)), ('in', status_reply(0x0D)),
        ])
        assert controller.write(22, data, timeout_s=3.0) == 0x10000
        transport.assert_done()

    def test_host_wait_expiry_on_a_write_takes_the_stop_path(self):
        controller, transport = attached(address_listener() + [
            ('out', p.write_message(b'A', T3S, True)), ('in', TransportTimeout('host wait')),
            STOP, ('in', status_reply(0x0D, error=1, count=-1), 12),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.write(22, b'A', timeout_s=3.0)
        assert info.value.code == 1
        transport.assert_done()


class TestRead:
    IDN_REPLY = h('36 41 42 43 44 45 0a ee ee ee ee ee ee ee ee ee'
                  '38 21 00 00 06 ff 00 00 aa 06 00 00 09 00 00 00 00 00 00 00 02 00 00 00 04 00 00 00')

    def test_read_ending_in_eoi_uses_the_worked_example_bytes(self):
        controller, transport = attached([
            ('out', p.command_message(bytes((0x3F, 0x20, 0x56)), T3S)), ('in', status_reply(0x0C), 12),
            ('out', h('06 00 00 00 04 00 00 00')), ('in', status_reply(0x06), 12),
            ('out', h('0a 00 00 fc 00 ff 00 00 09 02 00 01 0a 51 01 0a 55 00 00 00 04 00 00 00')),
            ('in', self.IDN_REPLY, 512),
        ])
        assert controller.read(22, max_bytes=256, timeout_s=3.0) == (b'ABCDE\n', True)
        transport.assert_done()

    def test_read_ending_on_the_count(self):
        controller, transport = attached(address_talker() + [
            ('out', p.read_message(5, T3S)), ('in', read_reply(b'ABCDE', 5, end=False), 512),
        ])
        assert controller.read(22, max_bytes=5, timeout_s=3.0) == (b'ABCDE', False)
        transport.assert_done()

    def test_read_with_eos_character(self):
        controller, transport = attached(address_talker() + [
            ('out', h('0a 14 0a fc 00 ff 00 00 09 02 00 01 0a 51 01 0a 55 00 00 00 04 00 00 00')),
            ('in', read_reply(b'1.5E+0\n', 256), 512),
        ])
        assert controller.read(22, max_bytes=256, timeout_s=3.0, eos=0x0A, eos_8bit=True) == (b'1.5E+0\n', True)
        transport.assert_done()

    def test_multi_block_reply_is_reassembled(self):
        data = bytes(range(40))
        controller, _ = attached(address_talker() + [
            ('out', p.read_message(1000, T3S)),
            ('in', read_reply(data, 1000), p.read_reply_buffer_size(1000, 512)),
        ])
        assert p.read_reply_buffer_size(1000, 512) == 1536  # (34 x 32 + 28) rounded to 512
        assert controller.read(22, max_bytes=1000, timeout_s=3.0) == (data, True)

    def test_device_timeout_raises_gpib_timeout_with_partial_data(self):
        controller, transport = attached(address_talker() + [
            ('out', p.read_message(256, T3S)), ('in', read_reply(b'AB', 256, end=False, error=0x0A), 512),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.read(22, max_bytes=256, timeout_s=3.0)
        assert info.value.partial == b'AB'
        assert info.value.code == 0x0A
        transport.assert_done()

    def test_host_wait_expiry_sends_the_stop_request_and_reports_a_timeout(self):
        controller, transport = attached(address_talker(code=t.TIMEOUT_DISABLED_CODE) + [
            ('out', p.read_message(256, t.TIMEOUT_DISABLED_CODE)),
            ('in', TransportTimeout('host wait expired')),
            STOP,
            ('in', read_reply(b'', 256, end=False, error=1), 512),
        ], infinite_wait_s=0.01)
        with pytest.raises(GpibTimeout):
            controller.read(22, max_bytes=256, timeout_s=None)
        transport.assert_done()
        assert transport.in_timeouts_after(0x0A) == [10, int(RECOVERY_WAIT_S * 1000)]

    def test_error_2_and_3_are_plain_gpib_errors(self):
        controller, _ = attached(address_talker() + [
            ('out', p.read_message(256, T3S)), ('in', read_reply(b'', 256, end=False, error=2), 512),
        ])
        with pytest.raises(GpibError) as info:
            controller.read(22, max_bytes=256, timeout_s=3.0)
        assert info.value.code == 2 and not isinstance(info.value, GpibTimeout)

    def test_zero_length_read_touches_nothing(self):
        controller, transport = attached([])
        assert controller.read(22, max_bytes=0, timeout_s=3.0) == (b'', False)
        transport.assert_done()

    def test_read_raw_sends_only_the_read_instruction(self):
        controller, transport = attached([
            ('out', p.read_message(8, T3S)), ('in', read_reply(b'\x40', 8), 512),
        ])
        assert controller.read_raw(8, timeout_s=3.0) == (b'\x40', True)
        transport.assert_done()


# ---------------------------------------------------------------------------
# §7.2 host waits
# ---------------------------------------------------------------------------

class TestHostWait:
    def test_five_second_request_uses_the_ten_second_row(self):
        controller, transport = attached(address_talker(code=0xFD) + [
            ('out', p.read_message(8, 0xFD)), ('in', read_reply(b'x', 8), 512),
        ])
        controller.read(22, max_bytes=8, timeout_s=5.0)
        assert transport.in_timeouts_after(0x0C) == [15000]  # 10 s + max(2, 5)
        assert transport.in_timeouts_after(0x06) == [SHORT_MS]
        assert transport.in_timeouts_after(0x0A) == [15000]

    def test_three_second_request(self):
        controller, transport = attached(address_listener() + [
            ('out', p.write_message(b'A', T3S, True)), ('in', status_reply(0x0D)),
        ])
        controller.write(22, b'A', timeout_s=3.0)
        assert transport.in_timeouts_after(0x0C) == [WAIT_3S_MS]
        assert transport.in_timeouts_after(0x0D) == [WAIT_3S_MS]

    def test_disabled_timeout_uses_the_application_wait(self):
        controller, transport = attached(address_listener(code=0xF0) + [
            ('out', p.write_message(b'A', 0xF0, True)), ('in', status_reply(0x0D)),
        ], infinite_wait_s=42.0)
        controller.write(22, b'A', timeout_s=None)
        assert transport.in_timeouts_after(0x0D) == [42000]

    def test_bulk_out_always_uses_the_short_wait(self):
        controller, transport = attached(address_listener() + [
            ('out', p.write_message(b'A', T3S, True)), ('in', status_reply(0x0D)),
        ])
        controller.write(22, b'A', timeout_s=3.0)
        assert {tm for kind, _, tm in transport.timeouts if kind == 'out'} == {SHORT_MS}


# ---------------------------------------------------------------------------
# the ATN rule and command chunking
# ---------------------------------------------------------------------------

class TestCommands:
    def test_standby_follows_every_addressing_command_before_a_read(self):
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ] + address_talker() + [
            ('out', p.read_message(8, T3S)), ('in', read_reply(b'x', 8), 512),
        ])
        controller.command(b'\x14', timeout_s=3.0)
        controller.read(22, max_bytes=8, timeout_s=3.0)
        opcodes = [message[0] for message in transport.sent[-4:]]
        assert opcodes == [0x0C, 0x0C, 0x06, 0x0A]

    def test_command_bytes_are_chunked_at_16(self):
        data = bytes(range(0x20, 0x20 + 20))
        controller, transport = attached([
            ('out', p.command_message(data[:16], T3S)), ('in', status_reply(0x0C)),
            ('out', p.command_message(data[16:], T3S)), ('in', status_reply(0x0C)),
        ])
        assert controller.command(data, timeout_s=3.0) == 20
        assert transport.sent[-2][1] == 0xF0 and transport.sent[-1][1] == 0xFC
        transport.assert_done()

    def test_partial_acceptance_is_counted(self):
        controller, _ = attached([
            ('out', p.command_message(b'\x3f\x40\x36', T3S)), ('in', status_reply(0x0C, count=-1)),
        ])
        assert controller.command(b'\x3f\x40\x36', timeout_s=3.0) == 2

    def test_host_wait_expiry_on_command_bytes_takes_the_stop_path(self):
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', TransportTimeout('host wait')),
            STOP, ('in', status_reply(0x0C, error=1, count=-1), 12),
        ])
        with pytest.raises(GpibTimeout):
            controller.command(b'\x14', timeout_s=3.0)
        transport.assert_done()

    def test_bus_control_operations(self):
        controller, transport = attached([
            ('out', p.interface_clear_message()), ('in', status_reply(0x0F)),
            ('out', p.register_write_message([t.REN_OFF_WRITE])), ('in', regwrite_reply(1)),
            ('out', p.take_control_message(False)), ('in', status_reply(0x01)),
            ('out', p.go_to_standby_message()), ('in', status_reply(0x06)),
            ('out', p.register_read_message([t.BSR_REGISTER])), ('in', regread_reply([0x81]), 32),
            ('ctrl', (0x21, 0x0200, 0, 8), STATUS_8),
            ('ctrl', (0x20, 0, 0, 8), STATUS_8),
        ])
        controller.interface_clear()
        controller.remote_enable(False)
        controller.take_control(synchronous=False)
        controller.go_to_standby()
        assert controller.bus_lines() == 0x81
        assert controller.status().ibsta == 0x0130
        assert controller.abort().id == 0x20
        transport.assert_done()


# ---------------------------------------------------------------------------
# §8.2 faults: resynchronise, then re-attach on the next operation
# ---------------------------------------------------------------------------

class TestFaults:
    def test_malformed_reply_stops_drains_and_reattaches_before_the_next_operation(self):
        controller, transport = attached([
            ('out', p.command_message(t.address_listener_command(0, 22), T3S)),
            ('in', status_reply(0x0D)),                       # wrong id echoed
            STOP, ('in', b'\x00' * 12, DRAIN_LENGTH),         # a stale reply drained
        ] + attach_script() + address_listener() + [
            ('out', p.write_message(b'A', T3S, True)), ('in', status_reply(0x0D)),
        ])
        with pytest.raises(ProtocolError):
            controller.write(22, b'A', timeout_s=3.0)
        assert controller.write(22, b'A', timeout_s=3.0) == 1
        transport.assert_done()
        drain = [tm for kind, length, tm in transport.timeouts if kind == 'in' and length == DRAIN_LENGTH]
        assert drain == [int(DRAIN_WAIT_S * 1000)]

    def test_second_timeout_after_a_stop_is_a_protocol_error_and_resyncs(self):
        controller, transport = attached(address_listener() + [
            ('out', p.write_message(b'A', T3S, True)), ('in', TransportTimeout('host wait')),
            STOP, ('in', TransportTimeout('still nothing'), 12),
            STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH),
        ])
        with pytest.raises(ProtocolError):
            controller.write(22, b'A', timeout_s=3.0)
        transport.assert_done()

    def test_usb_error_marks_the_adapter_for_reattach(self):
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', TransportError('pipe stalled')),
        ] + attach_script() + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(TransportError):
            controller.command(b'\x14', timeout_s=3.0)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_close_after_a_fault_skips_the_shutdown_write(self):
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', h('0c 00'), 12),
            STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH),
        ])
        with pytest.raises(ProtocolError):
            controller.command(b'\x14', timeout_s=3.0)
        controller.close()
        transport.assert_done()
        assert transport.closed

    def test_reattach_failure_surfaces_as_not_ready(self):
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', h('0c 00'), 12),
            STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH),
            ('ctrl', (0x41, 0, 0, 16), h('00 00 00 00 00')),
        ])
        with pytest.raises(ProtocolError):
            controller.command(b'\x14', timeout_s=3.0)
        with pytest.raises(AdapterNotReady):
            controller.command(b'\x14', timeout_s=3.0)
        transport.assert_done()


# ---------------------------------------------------------------------------
# device_ops
# ---------------------------------------------------------------------------

class TestDeviceOps:
    def test_device_clear(self):
        controller, transport = attached([
            ('out', p.command_message(bytes((0x3F, 0x36, 0x04)), T3S)), ('in', status_reply(0x0C)),
        ])
        ops.device_clear(controller, 22)
        transport.assert_done()

    def test_trigger_and_go_to_local_carry_the_given_timeout(self):
        controller, transport = attached([
            ('out', p.command_message(bytes((0x3F, 0x38, 0x08)), 0xFA)), ('in', status_reply(0x0C)),
            ('out', p.command_message(bytes((0x3F, 0x38, 0x01)), 0xFA)), ('in', status_reply(0x0C)),
            ('out', p.command_message(bytes((0x11,)), 0xFA)), ('in', status_reply(0x0C)),
            ('out', p.command_message(bytes((0x3F, 0x38, 0x11)), 0xFA)), ('in', status_reply(0x0C)),
        ])
        ops.trigger(controller, 24, timeout_s=0.3)
        ops.go_to_local(controller, 24, timeout_s=0.3)
        ops.local_lockout(controller, timeout_s=0.3)
        ops.local_lockout(controller, 24, timeout_s=0.3)
        transport.assert_done()

    def test_serial_poll_sequence(self):
        controller, transport = attached([
            ('out', p.command_message(bytes((0x3F, 0x20, 0x18, 0x56)), T3S)), ('in', status_reply(0x0C)),
            ('out', p.go_to_standby_message()), ('in', status_reply(0x06)),
            ('out', p.read_message(1, T3S)), ('in', read_reply(b'\x40', 1), 512),
            ('out', p.command_message(bytes((0x19, 0x5F)), T3S)), ('in', status_reply(0x0C)),
        ])
        assert ops.serial_poll(controller, 22) == 0x40
        transport.assert_done()

    def test_serial_poll_leaves_poll_mode_even_when_the_read_times_out(self):
        controller, transport = attached([
            ('out', p.command_message(bytes((0x3F, 0x20, 0x18, 0x56)), T3S)), ('in', status_reply(0x0C)),
            ('out', p.go_to_standby_message()), ('in', status_reply(0x06)),
            ('out', p.read_message(1, T3S)), ('in', read_reply(b'', 1, end=False, error=0x0A), 512),
            ('out', p.command_message(bytes((0x19, 0x5F)), T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(GpibTimeout):
            ops.serial_poll(controller, 22)
        transport.assert_done()

    def test_serial_poll_cleanup_failure_does_not_mask_the_read_failure(self):
        controller, transport = attached([
            ('out', p.command_message(bytes((0x3F, 0x20, 0x18, 0x56)), T3S)), ('in', status_reply(0x0C)),
            ('out', p.go_to_standby_message()), ('in', status_reply(0x06)),
            ('out', p.read_message(1, T3S)), ('in', read_reply(b'', 1, end=False, error=0x0A), 512),
            ('out', p.command_message(bytes((0x19, 0x5F)), T3S)), ('in', status_reply(0x0C, error=5, count=-2)),
        ])
        with pytest.raises(GpibTimeout):
            ops.serial_poll(controller, 22)
        transport.assert_done()

    def test_serial_poll_cleanup_failure_alone_is_raised(self):
        controller, _ = attached([
            ('out', p.command_message(bytes((0x3F, 0x20, 0x18, 0x56)), T3S)), ('in', status_reply(0x0C)),
            ('out', p.go_to_standby_message()), ('in', status_reply(0x06)),
            ('out', p.read_message(1, T3S)), ('in', read_reply(b'\x40', 1), 512),
            ('out', p.command_message(bytes((0x19, 0x5F)), T3S)), ('in', status_reply(0x0C, error=5, count=-2)),
        ])
        with pytest.raises(NoListener):
            ops.serial_poll(controller, 22)


def probe_steps(pad: int, ndac: bool) -> List[Tuple[Any, ...]]:
    return [
        ('out', p.command_message(bytes((0x3F, 0x20 + pad)), T3S)), ('in', status_reply(0x0C)),
        ('out', p.go_to_standby_message()), ('in', status_reply(0x06)),
        ('out', p.register_read_message([t.BSR_REGISTER])), ('in', regread_reply([0x20 if ndac else 0x00]), 32),
        ('out', p.take_control_message(True)), ('in', status_reply(0x01)),
        ('out', p.command_message(b'\x3f', T3S)), ('in', status_reply(0x0C)),
    ]


class TestFindListeners:
    def test_ndac_marks_a_listener(self):
        controller, transport = attached(probe_steps(5, False) + probe_steps(22, True) + probe_steps(24, False))
        assert ops.find_listeners(controller, [5, 22, 24]) == [22]
        transport.assert_done()

    def test_own_address_is_skipped(self):
        controller, transport = attached(probe_steps(1, True))
        assert ops.find_listeners(controller, [0, 1]) == [1]
        transport.assert_done()

    def test_empty_bus_stops_at_the_first_error_5(self):
        controller, transport = attached([
            ('out', p.command_message(bytes((0x3F, 0x21)), T3S)), ('in', status_reply(0x0C, error=5, count=-2)),
        ])
        assert ops.find_listeners(controller, [1, 2, 3]) == []
        transport.assert_done()

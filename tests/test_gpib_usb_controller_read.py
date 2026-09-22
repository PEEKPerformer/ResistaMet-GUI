"""Controller: framed reads (0x0a, §5.2), capped at 1024 bytes per instruction (§11.2)."""
from typing import List

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t
from resistamet_gui.gpib_usb.controller import (DEFAULT_INFINITE_WAIT_S, FRAMED_READ_MAX_BYTES,
                                                RAW_READ_MIN_BYTES, BUS_MIN_RATE_BPS, RECOVERY_WAIT_S)
from resistamet_gui.gpib_usb.protocol import GpibError, GpibTimeout
from resistamet_gui.gpib_usb.transport import TransportTimeout
from tests.fakes.gpib_usb import (STOP, T3S, address_listener, address_talker, attached, h, read_reply,
                                  status_reply, talking)


def piece_wait_ms(code: int, count: int = 1024) -> int:
    """The host wait for one framed piece of ``count`` bytes sent with ``code`` (§7.2)."""
    return int((p.host_wait_s(code, DEFAULT_INFINITE_WAIT_S) + count / BUS_MIN_RATE_BPS) * 1000)


class TestRead:
    #: The *IDN? reply as GPIB-USB-HS 01CEE482 sent it for a Keithley 2400 at PAD 3
    #: (2026-09-18): three 0x37 blocks and the 16-byte trailer. The specification's
    #: worked example drew a 28-byte trailer; the device disagreed.
    IDN_TEXT = b'KEITHLEY INSTRUMENTS INC.,MODEL 2400,1175680,C30   Mar 17 2006 09:29:29/A02  /K/J\n'
    IDN_REPLY = h(
        '37 00 4b 45 49 54 48 4c 45 59 20 49 4e 53 54 52 55 4d 45 4e 54 53 20 49 4e 43 2e 2c 4d 4f 44 45'
        '37 00 4c 20 32 34 30 30 2c 31 31 37 35 36 38 30 2c 43 33 30 20 20 20 4d 61 72 20 31 37 20 32 30'
        '37 00 30 36 20 30 39 3a 32 39 3a 32 39 2f 41 30 32 20 20 2f 4b 2f 4a 0a 00 00 00 00 00 00 00 00'
        '38 20 20 00 52 ff ff ff e0 16 00 00 04 00 00 00')

    def test_read_ending_in_eoi_as_observed_on_the_bench(self):
        controller, transport = attached([
            ('out', h('0c fd 00 fc 3f 20 43 00 04 00 00 00')), ('in', h('0c 00 6c 00 00 00 ff ff 04 00 00 00'), 12),
            ('out', h('06 00 00 00 04 00 00 00')), ('in', h('06 00 20 00 aa 55 ff ff 04 00 00 00'), 12),
            ('out', h('0a 00 00 fc 00 ff 00 00 09 02 00 01 0a 51 01 0a 55 00 00 00 04 00 00 00')),
            ('in', self.IDN_REPLY, 512),
        ])
        assert controller.read(3, max_bytes=256, timeout_s=3.0) == (self.IDN_TEXT, True)
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
        assert transport.in_timeouts_after(0x0A) == [10 + 256, int(RECOVERY_WAIT_S * 1000)]  # + 1 ms per byte

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

    #: Timed-out reads of 1 and 10 bytes as GPIB-USB-HS 01CEE482 sent them (2026-09-21, §5.2):
    #: one stale 0x36 block, min(requested, 15) in the last-block count, nothing read.
    STALE_TIMEOUT_REPLIES = [
        (1, h('36 00 20 00 aa 55 ff ff 04 00 00 00 4e 53 54 52 38 00 20 0a ff ff ff ff e0 01 00 00 04 00 00 00')),
        (10, h('36 00 20 00 aa 55 ff ff 04 00 00 00 04 00 00 00 38 00 20 0a f6 ff ff ff e0 0a 00 00 04 00 00 00')),
    ]

    @pytest.mark.parametrize('count, reply', STALE_TIMEOUT_REPLIES)
    def test_a_timed_out_read_with_a_stale_last_block_count_is_a_timeout_not_a_fault(self, count, reply):
        # The adapter ended the read itself and said so, so the next operation is ordinary: no
        # stop request, no drain, no pipe reset, no re-attach. Sizing the data by the last-block
        # byte made this reply a ProtocolError, and the timeout was reported as an I/O error
        # after a resync.
        controller, transport = attached(address_talker() + [
            ('out', p.read_message(count, T3S)), ('in', reply, 512),
        ] + address_listener() + [
            ('out', p.write_message(b'*IDN?\n', T3S, True)), ('in', status_reply(0x0D)),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.read(22, max_bytes=count, timeout_s=3.0)
        assert info.value.code == 0x0A and info.value.partial == b''
        assert controller.write(22, b'*IDN?\n', timeout_s=3.0) == 6
        transport.assert_done()
        assert not [step for step in transport.script if step[0] == 'ctrl' and step[1][0] == 0x20]
        assert not [step for step in transport.script if step[0] == 'clear_halt']


def framed_counts(messages: List[bytes]) -> List[int]:
    """The requested count of every 0x0a among ``messages``."""
    return [0x10000 - int.from_bytes(m[4:6], 'little') for m in messages if m[0] == p.OP_READ]


class TestFramedReadCap:
    """A framed 0x0a never asks for more than 1024 bytes (§10.1.1, §10.1.8, §11.2).

    On bench unit 01CEE482 a 20480-byte framed read whose answer ran to
    7000 bytes drew no reply and left the adapter unusable until it was
    unplugged, while NI's driver never sends a framed read above 1024. So a
    larger request is a sequence of 0x0a instructions of at most 1024 after
    one addressing, each with its own host wait, and a timeout on a later
    piece returns the earlier ones as the partial data.
    """

    #: The read instruction of the three pieces of a 3000-byte read: ``0a m e t cl ch 00 00``
    #: with -1024 = 0xfc00 and -952 = 0xfc48, then the embedded two-write block (§5.2).
    PIECE_1024 = h('0a 00 00 fc 00 fc 00 00 09 02 00 01 0a 51 01 0a 55 00 00 00 04 00 00 00')
    PIECE_952 = h('0a 00 00 fc 48 fc 00 00 09 02 00 01 0a 51 01 0a 55 00 00 00 04 00 00 00')
    #: Host receive buffer for either count: 35 x 32 + 28 = 1148 and 32 x 32 + 28 = 1052, both rounded to 1536.
    BUFFER = 1536

    def test_the_cap_is_the_last_count_ni_sends_framed(self):
        assert FRAMED_READ_MAX_BYTES == 1024 == RAW_READ_MIN_BYTES - 1
        assert p.read_message(1024, T3S) == self.PIECE_1024
        assert p.read_message(952, T3S) == self.PIECE_952

    def test_a_3000_byte_request_is_three_pieces_of_1024_1024_952_after_one_addressing(self):
        data = bytes(range(256)) * 11 + bytes(range(184))
        assert len(data) == 3000
        controller, transport = attached(address_talker() + [
            ('out', self.PIECE_1024), ('in', read_reply(data[:1024], 1024, end=False), self.BUFFER),
            ('out', self.PIECE_1024), ('in', read_reply(data[1024:2048], 1024, end=False), self.BUFFER),
            ('out', self.PIECE_952), ('in', read_reply(data[2048:], 952, end=True), self.BUFFER),
        ])
        assert controller.read(22, max_bytes=3000, timeout_s=3.0) == (data, True)
        transport.assert_done()
        # One 0x0c and one 0x06, then the three reads: nothing re-addresses between pieces.
        assert [m[0] for m in transport.sent[-5:]] == [p.OP_COMMAND, p.OP_GO_TO_STANDBY, p.OP_READ, p.OP_READ, p.OP_READ]

    def test_pyvisa_s_chunk_reads_a_3000_byte_answer_as_1024_1024_and_a_short_third(self):
        data = bytes(range(256)) * 11 + bytes(range(184))
        controller, transport = attached(address_talker() + [
            ('out', self.PIECE_1024), ('in', read_reply(data[:1024], 1024, end=False), self.BUFFER),
            ('out', self.PIECE_1024), ('in', read_reply(data[1024:2048], 1024, end=False), self.BUFFER),
            ('out', self.PIECE_1024), ('in', read_reply(data[2048:], 1024, end=True), self.BUFFER),
        ])
        assert controller.read(22, max_bytes=20480, timeout_s=3.0) == (data, True)
        transport.assert_done()

    def test_an_answer_shorter_than_the_count_ends_on_end_after_one_piece(self):
        controller, transport = attached(address_talker() + [
            ('out', self.PIECE_1024), ('in', read_reply(TestRead.IDN_TEXT, 1024), self.BUFFER),
        ])
        assert controller.read(22, max_bytes=20480, timeout_s=3.0) == (TestRead.IDN_TEXT, True)
        transport.assert_done()   # the script holds no second 0x0a

    def test_read_raw_is_capped_the_same_way(self):
        # The board-level read (INTFC session) reaches the loop without addressing.
        controller, transport = attached([
            ('out', self.PIECE_1024), ('in', read_reply(bytes(1024), 1024, end=False), self.BUFFER),
            ('out', self.PIECE_1024), ('in', read_reply(b'end', 1024), self.BUFFER),
        ])
        assert controller.read_raw(20480, timeout_s=3.0) == (bytes(1024) + b'end', True)
        transport.assert_done()

    def test_a_timeout_on_the_second_piece_returns_the_first_piece_as_the_partial(self):
        first = bytes(range(256)) * 4
        controller, transport = attached(address_talker() + [
            ('out', self.PIECE_1024), ('in', read_reply(first, 1024, end=False), self.BUFFER),
            ('out', self.PIECE_1024), ('in', read_reply(b'', 1024, end=False, error=0x0A), self.BUFFER),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.read(22, max_bytes=3000, timeout_s=3.0)
        assert info.value.partial == first and info.value.code == 0x0A
        transport.assert_done()

    def test_the_partial_data_of_the_piece_that_timed_out_is_kept_as_well(self):
        first = bytes(range(256)) * 4
        controller, transport = attached(address_talker() + [
            ('out', self.PIECE_1024), ('in', read_reply(first, 1024, end=False), self.BUFFER),
            ('out', self.PIECE_1024), ('in', read_reply(b'tail', 1024, end=False, error=0x0A), self.BUFFER),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.read(22, max_bytes=3000, timeout_s=3.0)
        assert info.value.partial == first + b'tail'
        transport.assert_done()

    def test_a_timed_out_piece_is_no_fault_and_the_next_operation_is_ordinary(self):
        controller, transport = attached(address_talker() + [
            ('out', self.PIECE_1024), ('in', read_reply(bytes(1024), 1024, end=False), self.BUFFER),
            ('out', self.PIECE_1024), ('in', read_reply(b'', 1024, end=False, error=0x0A), self.BUFFER),
        ] + address_listener() + [
            ('out', p.write_message(b'*IDN?\n', T3S, True)), ('in', status_reply(0x0D)),
        ])
        with pytest.raises(GpibTimeout):
            controller.read(22, max_bytes=3000, timeout_s=3.0)
        assert controller.write(22, b'*IDN?\n', timeout_s=3.0) == 6
        transport.assert_done()

    def test_each_piece_gets_the_host_wait_of_one_instruction_not_of_the_request(self):
        # The code bounds an instruction (§7.1, §10.1.8); the wait for a piece is the reply wait
        # of the code plus the piece's own bytes at BUS_MIN_RATE_BPS, the same for a request of
        # 1024 and of 35000. Summed over the request it would be 34 s longer for the second.
        code = 0xFB
        controller, transport = attached(address_talker(code=code) + [
            ('out', p.read_message(1024, code)), ('in', read_reply(TestRead.IDN_TEXT, 1024), self.BUFFER),
        ] + address_talker(code=code) + [
            ('out', p.read_message(1024, code)), ('in', read_reply(TestRead.IDN_TEXT, 1024), self.BUFFER),
        ])
        controller.read(22, max_bytes=1024, timeout_s=1.0)
        controller.read(22, max_bytes=35000, timeout_s=1.0)
        one_piece = int((p.host_wait_s(code, DEFAULT_INFINITE_WAIT_S) + 1024 / BUS_MIN_RATE_BPS) * 1000)
        assert transport.in_timeouts_after(0x0A) == [one_piece, one_piece]
        transport.assert_done()

    def test_a_35_kb_answer_under_the_one_second_code_is_35_pieces(self):
        message = bytes(i & 0xFF for i in range(35000))
        controller, transport = talking(message)
        assert controller.read(22, max_bytes=35000, timeout_s=1.0) == (message, True)
        assert transport.read_counts == [1024] * 34 + [184]
        opcodes = [m[0] for m in transport.sent]
        assert opcodes.count(p.OP_COMMAND) == 1 and opcodes.count(p.OP_GO_TO_STANDBY) == 1
        assert p.OP_READ_RAW not in opcodes
        # 35 round trips of one message each, every one waited for as one instruction.
        one_piece = int((p.host_wait_s(0xFB, DEFAULT_INFINITE_WAIT_S) + 1024 / BUS_MIN_RATE_BPS) * 1000)
        assert transport.in_timeouts[-35:-1] == [one_piece] * 34

    @settings(max_examples=150, deadline=None, database=None, derandomize=True)
    @given(max_bytes=st.integers(1, 70000), answer_length=st.integers(0, 70000))
    def test_no_framed_0x0a_ever_carries_a_count_above_1024(self, max_bytes, answer_length):
        message = bytes(i & 0xFF for i in range(answer_length))
        controller, transport = talking(message)
        if answer_length == 0:
            with pytest.raises(GpibTimeout) as info:
                controller.read(22, max_bytes=max_bytes, timeout_s=3.0)
            assert info.value.partial == b''
        else:
            assert controller.read(22, max_bytes=max_bytes, timeout_s=3.0) == (message[:max_bytes],
                                                                              answer_length <= max_bytes)
        counts = transport.read_counts
        assert counts == framed_counts(transport.sent)
        assert max(counts) <= FRAMED_READ_MAX_BYTES
        # Full pieces up to the one that ends on END, the count or the timeout; each piece
        # asks for what is left of the request, capped.
        pieces = -(-min(max_bytes, max(answer_length, 1)) // FRAMED_READ_MAX_BYTES)
        assert counts == [min(FRAMED_READ_MAX_BYTES, max_bytes - FRAMED_READ_MAX_BYTES * i) for i in range(pieces)]
        opcodes = [m[0] for m in transport.sent]
        assert opcodes.count(p.OP_COMMAND) == 1 and opcodes.count(p.OP_GO_TO_STANDBY) == 1
        assert p.OP_READ_RAW not in opcodes


class TestReadDeadline:
    """A read is bounded by its timeout as a whole (§7.1, §10.10.2).

    NI sends one instruction for the read, whose code bounds it from its
    start: a 0x0b still receiving the 2420's 61 kB trace answer at 5.3 kB/s
    ended at the code's expiry with 5543 bytes and error 0x0a. The framed
    path reads in pieces of 1024; each took the session's code afresh, so
    the same answer under a 1 s timeout read on for as long as it kept
    coming. Now each later piece gets the code for the time left.
    """

    PIECE = 0.19   # 1024 bytes at the 2420's 5.3 kB/s (§10.10.2)

    def test_a_long_answer_stops_at_the_code_s_expiry_with_what_it_has(self):
        # NI's 0x0b under 0xfb ended with 5543 bytes at 1.050 s (§10.10.2). 1.0 s goes out as
        # 0xfb, whose shortest expiry is 1.0498 s: that is the deadline. 0.86, 0.67, 0.48 s left:
        # 0xfb; 0.29 s: 0xfb (0xfa ends at 0.2634); 0.10 s: 0xf9. Then 1.14 s have passed.
        chunk = bytes(range(256)) * 4
        pieces = [0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0xF9]
        script = address_talker(pad=24, code=0xFB)
        for code in pieces:
            script += [('out', p.read_message(1024, code)),
                       ('in', read_reply(chunk, 1024, end=False), piece_wait_buffer(), self.PIECE)]
        controller, transport = attached(script)
        with pytest.raises(GpibTimeout) as info:
            controller.read(24, max_bytes=20480, timeout_s=1.0)
        assert info.value.partial == chunk * 6 and info.value.code == t.ERR_TIMEOUT
        assert 'timeout ran out after 6144 of 20480 bytes' in str(info.value)
        transport.assert_done()   # the script holds no seventh read
        # Each piece waits as one instruction of its own code, not of the session's.
        assert transport.in_timeouts_after(0x0A) == [piece_wait_ms(code) for code in pieces]

    def test_the_deadline_is_counted_from_the_start_of_the_read(self):
        # The addressing belongs to the read: time it takes is time the pieces do not get.
        controller, transport = attached([
            ('out', p.command_message(t.address_talker_command(0, 22), 0xFB)),
            ('in', status_reply(0x0C), None, 0.9),
            ('out', p.go_to_standby_message()), ('in', status_reply(0x06)),
            ('out', p.read_message(1024, 0xFB)),
            ('in', read_reply(bytes(1024), 1024, end=False), piece_wait_buffer(), 0.15),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.read(22, max_bytes=2048, timeout_s=1.0)
        assert info.value.partial == bytes(1024)
        transport.assert_done()

    def test_a_little_time_left_is_the_shortest_code_captured_not_one_below_it(self):
        controller, transport = attached(address_talker(code=0xFB) + [
            ('out', p.read_message(1024, 0xFB)),
            ('in', read_reply(bytes(1024), 1024, end=False), piece_wait_buffer(), 1.0497),
            ('out', p.read_message(1024, 0xF5)), ('in', read_reply(b'end', 1024), piece_wait_buffer()),
        ])
        assert controller.read(22, max_bytes=2048, timeout_s=1.0) == (bytes(1024) + b'end', True)
        transport.assert_done()

    def test_an_answer_within_the_timeout_is_whole(self):
        controller, transport = attached(address_talker(code=0xFB) + [
            ('out', p.read_message(1024, 0xFB)),
            ('in', read_reply(bytes(1024), 1024, end=False), piece_wait_buffer(), 0.3),
            ('out', p.read_message(1024, 0xFB)), ('in', read_reply(b'tail\n', 1024), piece_wait_buffer(), 0.1),
        ])
        assert controller.read(22, max_bytes=20480, timeout_s=1.0) == (bytes(1024) + b'tail\n', True)
        transport.assert_done()

    def test_no_timeout_has_no_deadline(self):
        controller, transport = attached(address_talker(code=0xF0) + [
            ('out', p.read_message(1024, 0xF0)),
            ('in', read_reply(bytes(1024), 1024, end=False), piece_wait_buffer(), 5000.0),
            ('out', p.read_message(1024, 0xF0)), ('in', read_reply(b'end', 1024), piece_wait_buffer()),
        ])
        assert controller.read(22, max_bytes=2048, timeout_s=None) == (bytes(1024) + b'end', True)
        transport.assert_done()

    def test_an_answer_whose_first_byte_comes_late_is_read_whole(self):
        # The application's 5 s goes out as 0xfd, under which the adapter waits up to 16.78 s
        # for the first byte. An instrument that answers after 8 s fills the first piece; NI's
        # single 0x0b would go on to read the rest, and so do the later pieces here, with the
        # 8.78 s left before 0xfd's expiry and the code for them (0xfd, then 0xfc).
        answer = bytes(range(256)) * 8 + b'end\n'
        controller, transport = attached(address_talker(code=0xFD) + [
            ('out', p.read_message(1024, 0xFD)),
            ('in', read_reply(answer[:1024], 1024, end=False), piece_wait_buffer(), 8.0 + self.PIECE),
            ('out', p.read_message(1024, 0xFD)),
            ('in', read_reply(answer[1024:2048], 1024, end=False), piece_wait_buffer(), 5.0),
            ('out', p.read_message(1024, 0xFC)), ('in', read_reply(answer[2048:], 1024), piece_wait_buffer()),
        ])
        assert controller.read(22, max_bytes=20480, timeout_s=5.0) == (answer, True)
        transport.assert_done()

    def test_read_raw_has_the_same_deadline(self):
        controller, transport = attached([
            ('out', p.read_message(1024, 0xFB)),
            ('in', read_reply(bytes(1024), 1024, end=False), piece_wait_buffer(), 1.05),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.read_raw(2048, timeout_s=1.0)
        assert info.value.partial == bytes(1024)
        transport.assert_done()


def piece_wait_buffer() -> int:
    """The host receive buffer for one 1024-byte framed piece (§8.6)."""
    return p.read_reply_buffer_size(1024, 512)

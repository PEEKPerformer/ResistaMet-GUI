"""Controller: raw reads, 0x0b with the data on the alternate bulk IN (§10.1), and the
switch that turns the raw transfers on.
"""
import pytest

from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t
from resistamet_gui.gpib_usb.controller import (DRAIN_WAIT_S, RAW_READ_MIN_BYTES, BUS_MIN_RATE_BPS,
                                                RECOVERY_WAIT_S, RAW_READ_SLICE_S, RAW_REPLY_POLL_S, Controller)
from resistamet_gui.gpib_usb.protocol import GpibError, GpibTimeout, NoListener, ProtocolError
from resistamet_gui.gpib_usb.transport import TransportTimeout
from tests.fakes.gpib_usb import (DRAIN_LENGTH, RAW_DRAIN, SHORT_MS, STOP, T3S, ScriptedTransport,
                                  address_listener, address_talker, attach_script, attached, attached_ni, h,
                                  ni_raw_read_reply, ni_read, ni_session, raw_read_reply, raw_wait_slices,
                                  read_reply, regread_reply, regwrite_reply, status_reply)


IDN_2420 = b'KEITHLEY INSTRUMENTS INC.,MODEL 2420,1230523,C30   Mar 17 2006 09:29:29/A02  /H/L\n'


class TestRawRead:
    """Reads of RAW_READ_MIN_BYTES and more: 0x0b, data on the alternate bulk IN (§10.1)."""

    def test_idn_over_0x0b_with_ni_bytes(self):
        # counts.pcap 13.4323 / 13.4419 / 13.4424: the 0x0b of 4096 with the 3 s code, in NI's
        # 40-byte message (§10.1.2) -- snapshot, addressing with 0xfd, no 0x06, the 0x0b with the
        # session's character in e, the clear-END and bank-2 writes -- and NI's 56-byte reply.
        # Before it, NI's bank-2 session configuration for PAD 24 (§10.2.4).
        controller, transport = attached_ni(ni_session(pad=24) + [
            ('out', h('03 00 00 00 0c fd 00 fd 3f 20 58 00 0b 00 0a fc 00 f0 ff ff'
                      '09 01 00 01 0a 55 00 00 09 01 00 02 03 01 00 00 04 00 00 00')),
            ('raw_in', IDN_2420, 4608),
            ('in', h('03 00 28 00 00 00 ff ff 0c 00 74 00 00 00 ff ff 0b 20 64 00 52 f0 ff ff e0 00 00 00'
                     '09 00 64 00 52 f0 ff ff 01 00 00 00 09 00 64 00 52 f0 ff ff 01 00 00 00 04 00 00 00'), 512),
        ])
        assert controller.read(24, max_bytes=4096, timeout_s=3.0, termchar=0x0A) == (IDN_2420, True)
        transport.assert_done()

    def test_the_session_configuration_follows_the_address_and_the_code(self):
        # §10.2.4: the 32-byte form for a new address, the 28-byte update for a new code on the
        # same one, nothing when both repeat; NI's close of the last session at the close.
        controller, transport = attached_ni(ni_session(pad=24) + [
            ('out', ni_read(4096, pad=24)), ('raw_in', b'a\n', 4608), ('in', ni_raw_read_reply(4096, 2), 512),
            ('out', ni_read(4096, pad=24)), ('raw_in', b'b\n', 4608), ('in', ni_raw_read_reply(4096, 2), 512),
        ] + ni_session(pad=24, code=0xFB, update=True) + [
            ('out', ni_read(4096, 0xFB, pad=24)), ('raw_in', b'c\n', 4608), ('in', ni_raw_read_reply(4096, 2), 512),
        ] + ni_session(pad=5, code=0xFB) + [
            ('out', ni_read(4096, 0xFB, pad=5)), ('raw_in', b'd\n', 4608), ('in', ni_raw_read_reply(4096, 2), 512),
            ('out', p.ni_session_close_message()),
            ('in', h('09 00 64 00 00 00 ff ff 01 00 00 00 09 00 64 00 00 00 ff ff 01 00 00 00 04 00 00 00'), 512),
            ('out', p.register_write_message(t.SHUTDOWN_WRITES)), ('in', regwrite_reply(2), 16),
        ])
        for pad, timeout_s in ((24, 3.0), (24, 3.0), (24, 1.0), (5, 1.0)):
            controller.read(pad, max_bytes=4096, timeout_s=timeout_s)
        controller.close()
        transport.assert_done()

    def test_a_session_configuration_the_adapter_did_not_complete_is_a_fault(self):
        controller, transport = attached_ni([
            ('out', p.ni_session_open_message(22, None, T3S)),
            ('in', h('03 00 30 00 00 00 ff ff 09 00 64 00 00 00 ff ff 01 00 00 00'
                     '09 00 64 00 00 00 ff ff 02 00 00 00 04 00 00 00'), 512),
            STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH), RAW_DRAIN,
        ])
        with pytest.raises(ProtocolError):
            controller.read(22, max_bytes=4096, timeout_s=3.0)
        transport.assert_done()

    def test_an_addressing_error_in_ni_s_message_is_raised(self):
        # The 0x0c of the message is answered in its own block (§10.2.1); error 5, nothing on the
        # bus to take the command bytes, is the error that matters, not the 0x0b's behind it.
        controller, transport = attached_ni(ni_session() + [
            ('out', ni_read(4096)), ('raw_in', b'', 4608),
            ('in', ni_raw_read_reply(4096, 0, end=False, error=0x0A, command_error=5), 512),
        ])
        with pytest.raises(NoListener) as info:
            controller.read(22, max_bytes=4096, timeout_s=3.0)
        assert info.value.code == 5
        transport.assert_done()

    def test_the_wait_for_ni_s_message_allows_for_its_addressing_block_too(self):
        # §7.2: a message with two timed blocks can run to the sum of their expiries; NI's read
        # carries the addressing 0x0c under 0xfd (20.0 s on 01CEE482) beside the 0x0b.
        controller, transport = attached_ni(ni_session(code=t.TIMEOUT_DISABLED_CODE) + [
            ('out', ni_read(4096, t.TIMEOUT_DISABLED_CODE)),
        ] + raw_wait_slices(24106, 4608) + [
            STOP, ('raw_in', b'', 4608), ('in', ni_raw_read_reply(4096, 0, end=False, error=1), 512),
        ], infinite_wait_s=0.01)
        with pytest.raises(GpibTimeout):
            controller.read(22, max_bytes=4096, timeout_s=None)
        transport.assert_done()
        waits = [tm for kind, _, tm in transport.timeouts if kind == 'raw_in'][:-1]
        assert sum(waits) == int((0.01 + 20.0 + 4096 / BUS_MIN_RATE_BPS) * 1000)

    def test_the_threshold_is_ni_s_1024_1025(self):
        # §10.1.1: counts.pcap 12.8188 is the last 0x0a (1024), read_thresholds.pcap 0.3160 the
        # first 0x0b (1025); the instruction blocks below are NI's bytes but for e, which NI
        # fills with the termination character and this driver leaves 0x00 with the compare off.
        assert RAW_READ_MIN_BYTES == 1025
        controller, transport = attached_ni(address_talker() + [
            ('out', h('0a 00 00 fc 00 fc 00 00') + p.read_message(1024, T3S)[8:]),
            ('in', read_reply(b'x', 1024), p.read_reply_buffer_size(1024, 512)),
        ] + ni_session() + [
            ('out', ni_read(1025)), ('raw_in', b'x', p.raw_read_buffer_size(1025, 512)),
            ('in', ni_raw_read_reply(1025, 1), 512),
        ])
        assert ni_read(1025)[12:20] == h('0b 00 00 fc ff fb ff ff')
        assert controller.read(22, max_bytes=1024, timeout_s=3.0) == (b'x', True)
        assert controller.read(22, max_bytes=1025, timeout_s=3.0) == (b'x', True)
        transport.assert_done()

    def test_data_is_read_before_the_reply_and_the_reply_wait_is_short(self):
        controller, transport = attached_ni(ni_session() + [
            ('out', ni_read(20480)),
            ('raw_in', IDN_2420, 20992),
            ('in', ni_raw_read_reply(20480, 82), 512),
        ])
        controller.read(22, max_bytes=20480, timeout_s=3.0)
        kinds = [kind for kind, _, _ in transport.timeouts][-3:]
        assert kinds == ['out', 'raw_in', 'in']
        assert transport.timeouts[-2] == ('raw_in', 20992, int(RAW_READ_SLICE_S * 1000))  # the first slice
        assert transport.timeouts[-1] == ('in', 512, SHORT_MS)

    def test_full_chunk_has_end_clear(self):
        data = bytes(range(256)) * 80
        controller, transport = attached_ni(ni_session() + [
            ('out', ni_read(20480)),
            ('raw_in', data, 20992),
            ('in', ni_raw_read_reply(20480, 20480, end=False), 512),
        ])
        assert controller.read(22, max_bytes=20480, timeout_s=3.0) == (data, False)
        transport.assert_done()

    def test_request_above_one_instruction_is_one_of_ni_s_messages_per_piece(self):
        # NI re-addresses before every chunk, each being a whole message (trac.pcap, §10.1.4).
        first, second = bytes(0xFFFF), b'tail\n'
        controller, transport = attached_ni(ni_session() + [
            ('out', ni_read(0xFFFF)), ('raw_in', first, 66048),
            ('in', ni_raw_read_reply(0xFFFF, 0xFFFF, end=False), 512),
            ('out', ni_read(70000 - 0xFFFF)), ('raw_in', second, 4608),
            ('in', ni_raw_read_reply(70000 - 0xFFFF, len(second)), 512),
        ])
        assert controller.read(22, max_bytes=70000, timeout_s=3.0) == (first + second, True)
        transport.assert_done()

    def test_a_read_split_over_two_instructions_shares_one_deadline(self):
        # §7.1, §10.10.2: the timeout bounds the read, not each instruction of it. 2.5 of the
        # 3 s are gone after the first: the second carries the code for the 0.5 s left.
        first, second = bytes(0xFFFF), b'tail\n'
        controller, transport = attached_ni(ni_session() + [
            ('out', ni_read(0xFFFF)), ('raw_in', first, 66048, 2.5),
            ('in', ni_raw_read_reply(0xFFFF, 0xFFFF, end=False), 512),
        ] + ni_session(code=0xFB, update=True) + [
            ('out', ni_read(70000 - 0xFFFF, 0xFB)), ('raw_in', second, 4608),
            ('in', ni_raw_read_reply(70000 - 0xFFFF, len(second)), 512),
        ])
        assert controller.read(22, max_bytes=70000, timeout_s=3.0) == (first + second, True)
        transport.assert_done()

    def test_no_instruction_starts_after_the_deadline(self):
        first = bytes(0xFFFF)
        controller, transport = attached_ni(ni_session() + [
            ('out', ni_read(0xFFFF)), ('raw_in', first, 66048, 3.0),
            ('in', ni_raw_read_reply(0xFFFF, 0xFFFF, end=False), 512),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.read(22, max_bytes=70000, timeout_s=3.0)
        assert info.value.partial == first
        transport.assert_done()

    def test_loop_stops_at_the_request_and_a_small_last_chunk_stays_raw(self):
        # The requested count decides the instruction once per read (§10.1.1): the 1-byte tail
        # of a 0x0b read is a 0x0b too, not a framed 0x0a in the middle of the message. The
        # odd byte arrives padded to two on the alternate IN (§10.1.3).
        controller, transport = attached_ni([
            ('out', p.read_raw_message(0xFFFF, T3S)), ('raw_in', bytes(0xFFFF), 66048),
            ('in', raw_read_reply(0xFFFF, 0xFFFF, end=False), 512),
            ('out', p.read_raw_message(1, T3S)), ('raw_in', b'z\x00', 512),
            ('in', raw_read_reply(1, 1, end=False), 512),
        ])
        data, end = controller.read_raw(0x10000, timeout_s=3.0)
        assert len(data) == 0x10000 and data[-1:] == b'z' and not end
        transport.assert_done()
        assert p.OP_READ not in [message[0] for message in transport.sent]

    def test_a_tail_just_under_the_threshold_stays_raw_and_a_small_request_stays_framed(self):
        tail = RAW_READ_MIN_BYTES - 1
        controller, transport = attached_ni([
            ('out', p.read_raw_message(0xFFFF, T3S)), ('raw_in', bytes(0xFFFF), 66048),
            ('in', raw_read_reply(0xFFFF, 0xFFFF, end=False), 512),
            ('out', p.read_raw_message(tail, T3S)), ('raw_in', b'end\n', 1536),
            ('in', raw_read_reply(tail, 4), 512),
            ('out', p.read_message(tail, T3S)), ('in', read_reply(b'end\n', tail)),
        ])
        data, end = controller.read_raw(0xFFFF + tail, timeout_s=3.0)
        assert len(data) == 0xFFFF + 4 and end
        assert controller.read_raw(tail, timeout_s=3.0) == (b'end\n', True)   # the same count asked alone
        assert [message[0] for message in transport.sent[-3:]] == [p.OP_READ_RAW, p.OP_READ_RAW, p.OP_READ]

    def test_zero_length_transfer_on_a_device_timeout(self):
        # nolistener.pcap 9.8121 / 9.8126: IN88 0 B, then error 0x0a with count -20480.
        controller, transport = attached_ni(ni_session(pad=5) + [
            ('out', ni_read(20480, pad=5)),
            ('raw_in', b'', 20992),
            ('in', ni_raw_read_reply(20480, 0, end=False, error=0x0A), 512),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.read(5, max_bytes=20480, timeout_s=3.0)
        assert info.value.partial == b'' and info.value.code == 0x0A
        transport.assert_done()

    def test_a_device_timeout_needs_no_stop_request_and_no_reattach(self):
        # raw_errors.pcap 6.1075-10.3034: the adapter ends the 0x88 transfer itself with a
        # zero-length packet at its timeout, the reply follows, and the next operation is
        # ordinary (§10.6.6, §10.6.7). NI's message and reply, byte for byte.
        controller, transport = attached_ni(ni_session(pad=5) + [
            ('out', h('03 00 00 00 0c fd 00 fd 3f 20 45 00 0b 00 0a fc 00 b0 ff ff'
                      '09 01 00 01 0a 55 00 00 09 01 00 02 03 01 00 00 04 00 00 00')),
            ('raw_in', b'', 20992),
            ('in', h('03 00 28 00 3a f6 ff ff 0c 00 74 00 00 00 00 00 0b 00 64 0a 00 b0 ff ff 60 00 00 00'
                     '09 00 64 00 00 b0 ff ff 01 00 00 00 09 00 64 00 00 b0 ff ff 01 00 00 00 04 00 00 00'), 512),
        ] + address_listener(pad=24) + [
            ('out', p.write_message(b'*IDN?\n', T3S, True)), ('in', status_reply(0x0D)),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.read(5, max_bytes=20480, timeout_s=3.0, termchar=0x0A)
        assert info.value.code == 0x0A and info.value.partial == b''
        assert controller.write(24, b'*IDN?\n', timeout_s=3.0) == 6
        transport.assert_done()  # no ('ctrl', 0x20 ...) step anywhere in the script

    def test_partial_data_on_a_device_timeout(self):
        controller, transport = attached_ni(ni_session() + [
            ('out', ni_read(4096)),
            ('raw_in', b'PART', 4608),
            ('in', ni_raw_read_reply(4096, 4, end=False, error=0x0A), 512),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.read(22, max_bytes=4096, timeout_s=3.0)
        assert info.value.partial == b'PART'

    def test_timeout_in_a_later_chunk_keeps_the_earlier_ones(self):
        controller, _ = attached_ni([
            ('out', p.read_raw_message(0xFFFF, T3S)), ('raw_in', bytes(0xFFFF), 66048),
            ('in', raw_read_reply(0xFFFF, 0xFFFF, end=False), 512),
            ('out', p.read_raw_message(4096, T3S)), ('raw_in', b'AB', 4608),
            ('in', raw_read_reply(4096, 2, end=False, error=0x0A), 512),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.read_raw(0xFFFF + 4096, timeout_s=3.0)
        assert len(info.value.partial) == 0xFFFF + 2

    def test_transfer_longer_than_the_count_is_cut_to_the_count(self):
        # trac.pcap 13.0075 / 13.0079: 6 bytes on 0x88 for a 5-byte answer.
        controller, transport = attached_ni(ni_session(pad=24) + [
            ('out', ni_read(20480, pad=24)),
            ('raw_in', h('31 31 30 33 0a 00'), 20992),
            ('in', ni_raw_read_reply(20480, 5), 512),
        ])
        assert controller.read(24, max_bytes=20480, timeout_s=3.0) == (b'1103\n', True)

    def test_fewer_bytes_than_the_count_is_a_fault_that_resyncs(self):
        controller, transport = attached_ni(ni_session() + [
            ('out', ni_read(4096)),
            ('raw_in', b'SHORT', 4608),
            ('in', ni_raw_read_reply(4096, 82), 512),
            STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH), RAW_DRAIN,
        ])
        with pytest.raises(ProtocolError):
            controller.read(22, max_bytes=4096, timeout_s=3.0)
        transport.assert_done()

    def test_host_wait_expiry_on_the_data_stops_the_device_and_reports_a_timeout(self):
        controller, transport = attached_ni([
            ('out', p.read_raw_message(4096, t.TIMEOUT_DISABLED_CODE)),
        ] + raw_wait_slices(4106, 4608) + [
            STOP,
            ('raw_in', b'', 4608),
            ('in', raw_read_reply(4096, 0, end=False, error=1), 512),
        ], infinite_wait_s=0.01)
        with pytest.raises(GpibTimeout) as info:
            controller.read_raw(4096, timeout_s=None)
        assert info.value.code == 1 and info.value.partial == b''
        transport.assert_done()
        waits = [tm for kind, _, tm in transport.timeouts if kind == 'raw_in']
        assert waits == [1000, 1000, 1000, 1000, 106, int(RECOVERY_WAIT_S * 1000)]
        assert sum(waits[:-1]) == int((0.01 + 4096 / BUS_MIN_RATE_BPS) * 1000)  # the whole host wait, in slices
        looks = [tm for kind, length, tm in transport.timeouts[:-1] if kind == 'in' and length == 512]
        assert looks == [int(RAW_REPLY_POLL_S * 1000)] * 4
        assert transport.timeouts[-1] == ('in', 512, int(RECOVERY_WAIT_S * 1000))

    def test_an_error_reply_is_seen_within_a_slice_though_the_data_transfer_never_ends(self):
        # §10.6.7 does not show whether the adapter completes the 0x88 transfer for read errors
        # other than the timeout. If it does not, the reply is already waiting on the primary IN:
        # it must not take the whole transfer wait (26 s here) to find, and an instruction that
        # has reported its error gets no stop request.
        controller, transport = attached_ni([
            ('out', p.read_raw_message(20480, T3S)),
            ('raw_in', TransportTimeout('nothing'), 20992),
            ('in', raw_read_reply(20480, 0, end=False, error=3), 512),
            ('raw_in', TransportTimeout('nothing to collect'), 20992),
        ])
        with pytest.raises(GpibError) as info:
            controller.read_raw(20480, timeout_s=3.0)
        assert info.value.code == 3
        transport.assert_done()
        assert [(kind, tm) for kind, _, tm in transport.timeouts[-3:]] == [
            ('raw_in', 1000), ('in', int(RAW_REPLY_POLL_S * 1000)), ('raw_in', int(DRAIN_WAIT_S * 1000))]

    def test_normal_data_arriving_in_a_later_slice(self):
        controller, transport = attached_ni([
            ('out', p.read_raw_message(20480, T3S)),
            ('raw_in', TransportTimeout('still formatting'), 20992), ('in', TransportTimeout('no reply yet'), 512),
            ('raw_in', TransportTimeout('still formatting'), 20992), ('in', TransportTimeout('no reply yet'), 512),
            ('raw_in', IDN_2420, 20992),
            ('in', raw_read_reply(20480, len(IDN_2420)), 512),
        ])
        assert controller.read_raw(20480, timeout_s=3.0) == (IDN_2420, True)
        transport.assert_done()
        assert transport.timeouts[-1] == ('in', 512, SHORT_MS)

    def test_data_split_over_two_slices_is_joined(self):
        first, second = bytes(range(256)) * 2, b'tail\n\x00'
        controller, transport = attached_ni([
            ('out', p.read_raw_message(20480, T3S)),
            ('raw_in', TransportTimeout('mid-transfer', partial=first), 20992),
            ('in', TransportTimeout('no reply yet'), 512),
            ('raw_in', second, 20992 - 512),
            ('in', raw_read_reply(20480, 517), 512),
        ])
        assert controller.read_raw(20480, timeout_s=3.0) == (first + b'tail\n', True)
        transport.assert_done()

    def test_a_reply_that_overtakes_the_end_of_the_data_still_collects_it(self):
        # A transfer that ends at the edge of a slice is reported as a timeout with everything
        # in ``partial``; the reply then says the read was good, and nothing is lost.
        controller, transport = attached_ni([
            ('out', p.read_raw_message(20480, T3S)),
            ('raw_in', TransportTimeout('at the deadline', partial=IDN_2420), 20992),
            ('in', raw_read_reply(20480, len(IDN_2420)), 512),
            ('raw_in', TransportTimeout('nothing more'), 20992 - len(IDN_2420)),
        ])
        assert controller.read_raw(20480, timeout_s=3.0) == (IDN_2420, True)
        transport.assert_done()

    def test_a_device_timeout_ended_by_the_adapter_s_zero_length_packet_in_a_later_slice(self):
        # §10.6.7: the adapter ends a timed-out 0x0b itself, with a zero-length transfer on 0x88.
        controller, transport = attached_ni([
            ('out', p.read_raw_message(20480, 0xFB)),
            ('raw_in', TransportTimeout('nothing yet'), 20992), ('in', TransportTimeout('no reply yet'), 512),
            ('raw_in', b'', 20992),
            ('in', raw_read_reply(20480, 0, end=False, error=0x0A), 512),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.read_raw(20480, timeout_s=1.0)
        assert info.value.code == 0x0A
        transport.assert_done()
        assert 0x20 not in [step[1][0] for step in transport.script if step[0] == 'ctrl'][3:]  # no stop request

    def test_partial_data_at_the_host_wait_is_kept_and_completed_after_the_stop(self):
        # The transport received 4 bytes when its wait expired (pyusb's partial count); after the
        # stop the device completes the transfer with 2 more and the reply counts 6.
        controller, transport = attached_ni([
            ('out', p.read_raw_message(4096, t.TIMEOUT_DISABLED_CODE)),
        ] + raw_wait_slices(4106, 4608, partial=b'PART') + [
            STOP,
            ('raw_in', b'IA', 4604),
            ('in', raw_read_reply(4096, 6, end=False, error=1), 512),
        ], infinite_wait_s=0.01)
        with pytest.raises(GpibTimeout) as info:
            controller.read_raw(4096, timeout_s=None)
        assert info.value.partial == b'PARTIA' and info.value.code == 1
        transport.assert_done()

    def test_partial_data_with_nothing_more_after_the_stop(self):
        controller, transport = attached_ni([
            ('out', p.read_raw_message(4096, t.TIMEOUT_DISABLED_CODE)),
        ] + raw_wait_slices(4106, 4608, partial=b'PART') + [
            STOP,
            ('raw_in', TransportTimeout('nothing more'), 4604),
            ('in', raw_read_reply(4096, 4, end=False, error=1), 512),
        ], infinite_wait_s=0.01)
        with pytest.raises(GpibTimeout) as info:
            controller.read_raw(4096, timeout_s=None)
        assert info.value.partial == b'PART'
        transport.assert_done()

    def test_stopped_read_whose_data_never_completes_is_fine_when_nothing_was_read(self):
        controller, transport = attached_ni([
            ('out', p.read_raw_message(4096, t.TIMEOUT_DISABLED_CODE)),
        ] + raw_wait_slices(4106, 4608) + [
            STOP,
            ('raw_in', TransportTimeout('still nothing'), 4608),
            ('in', raw_read_reply(4096, 0, end=False, error=1), 512),
        ], infinite_wait_s=0.01)
        with pytest.raises(GpibTimeout):
            controller.read_raw(4096, timeout_s=None)
        transport.assert_done()

    def test_stopped_read_whose_data_never_completes_but_was_read_is_a_fault(self):
        controller, transport = attached_ni([
            ('out', p.read_raw_message(4096, t.TIMEOUT_DISABLED_CODE)),
        ] + raw_wait_slices(4106, 4608) + [
            STOP,
            ('raw_in', TransportTimeout('still nothing'), 4608),
            ('in', raw_read_reply(4096, 40, end=False, error=1), 512),
            STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH), RAW_DRAIN,
        ], infinite_wait_s=0.01)
        with pytest.raises(ProtocolError):
            controller.read_raw(4096, timeout_s=None)
        transport.assert_done()

    def test_reply_missing_after_the_data_takes_the_stop_path(self):
        controller, transport = attached_ni([
            ('out', p.read_raw_message(4096, T3S)),
            ('raw_in', b'AB', 4608),
            ('in', TransportTimeout('no reply'), 512),
            STOP,
            ('in', raw_read_reply(4096, 2, end=False, error=1), 512),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.read_raw(4096, timeout_s=3.0)
        assert info.value.partial == b'AB'
        transport.assert_done()

    def test_no_reply_even_after_the_stop_is_a_fault(self):
        controller, transport = attached_ni([
            ('out', p.read_raw_message(4096, T3S)),
            ('raw_in', b'', 4608),
            ('in', TransportTimeout('no reply'), 512),
            STOP,
            ('in', TransportTimeout('still no reply'), 512),
            STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH), RAW_DRAIN,
        ])
        with pytest.raises(ProtocolError):
            controller.read_raw(4096, timeout_s=3.0)
        transport.assert_done()

    def test_the_eos_character_reaches_the_instruction_and_plain_reads_send_00_00(self):
        controller, transport = attached_ni([
            ('out', h('0b 00 00 fc 00 f0 ff ff 09 01 00 01 0a 55 00 00 04 00 00 00')),
            ('raw_in', b'x', 4608), ('in', raw_read_reply(4096, 1), 512),
            ('out', h('0b 14 2c fc 00 f0 ff ff 09 01 00 01 0a 55 00 00 04 00 00 00')),
            ('raw_in', b'x', 4608), ('in', raw_read_reply(4096, 1), 512),
        ])
        controller.read_raw(4096, timeout_s=3.0)
        controller.read_raw(4096, timeout_s=3.0, eos=0x2C, eos_8bit=True)
        transport.assert_done()

    def test_a_model_without_the_alternate_pair_stays_framed(self):
        script = [
            ('out', p.register_read_message(t.USB_B_SERIAL_REGISTERS)),
            ('in', regread_reply([0x78, 0x56, 0x34, 0x12]), 32),
        ] + attach_script()[2:] + [
            ('out', p.read_message(1024, T3S)),   # pyvisa's 20480, capped at FRAMED_READ_MAX_BYTES per 0x0a
            ('in', read_reply(b'x', 1024), p.read_reply_buffer_size(1024, 512)),
        ]
        transport = ScriptedTransport(script)
        controller = Controller(transport, t.PID_USB_B, ni_instructions=True, sleep=lambda s: None)
        controller.attach()
        assert controller.read_raw(20480, timeout_s=3.0) == (b'x', True)
        transport.assert_done()


class TestRawTransfersSwitch:
    """Controller(raw_transfers=...): off unless asked for, and only on a model with the alternate pair."""

    def test_default_is_framed_even_on_a_model_with_the_pair(self):
        controller, transport = attached(address_talker() + [
            ('out', p.read_message(1024, T3S)),   # pyvisa's 20480, capped at FRAMED_READ_MAX_BYTES per 0x0a
            ('in', read_reply(b'x', 1024), p.read_reply_buffer_size(1024, 512)),
        ] + address_listener() + [
            ('out', p.write_message(bytes(3000), T3S, True)), ('in', status_reply(0x0D)),
        ])
        assert controller.raw_transfers is False and controller.ni_instructions is False
        # pyvisa's chunk is 20480 bytes: the application's first read is this one.
        assert controller.read(22, max_bytes=20480, timeout_s=3.0) == (b'x', True)
        assert controller.write(22, bytes(3000), timeout_s=3.0) == 3000
        transport.assert_done()

    def test_switched_on_large_transfers_go_raw(self):
        controller, _ = attached_ni([])
        assert controller.raw_transfers is True and controller.ni_instructions is True

    def test_switched_off_reads_and_writes_stay_framed(self):
        controller, transport = attached(address_talker() + [
            ('out', p.read_message(1024, T3S)),   # pyvisa's 20480, capped at FRAMED_READ_MAX_BYTES per 0x0a
            ('in', read_reply(b'x', 1024), p.read_reply_buffer_size(1024, 512)),
        ] + address_listener() + [
            ('out', p.write_message(bytes(3000), T3S, True)), ('in', status_reply(0x0D)),
        ], ni_instructions=False)
        assert controller.raw_transfers is False
        assert controller.read(22, max_bytes=20480, timeout_s=3.0) == (b'x', True)
        assert controller.write(22, bytes(3000), timeout_s=3.0) == 3000
        transport.assert_done()

    def test_switched_off_resync_does_not_touch_the_alternate_endpoint(self):
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', h('0c 00'), 12),
            STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH),
        ], ni_instructions=False)
        with pytest.raises(ProtocolError):
            controller.command(b'\x14', timeout_s=3.0)
        transport.assert_done()

    def test_switched_on_without_the_pair_is_still_framed(self):
        script = [
            ('out', p.register_read_message(t.USB_B_SERIAL_REGISTERS)),
            ('in', regread_reply([0x78, 0x56, 0x34, 0x12]), 32),
        ] + attach_script()[2:]
        controller = Controller(ScriptedTransport(script), t.PID_USB_B, ni_instructions=True, sleep=lambda s: None)
        controller.attach()
        assert controller.raw_transfers is False
        assert controller.ni_instructions is True  # the serial poll needs no alternate pair

"""Controller: the wait for a service request (§10.4, §10.11, §10.12)."""
from typing import List, Optional

import pytest

from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t
from resistamet_gui.gpib_usb.controller import SRQ_WAIT_SLICE_S, Controller
from resistamet_gui.gpib_usb.protocol import AdapterGone, AdapterNotReady, GpibError, GpibTimeout, ProtocolError
from resistamet_gui.gpib_usb.srq import SRQ_PUSH_AFTER_SRQI_S
from resistamet_gui.gpib_usb.transport import TransportError, TransportGone, TransportTimeout
from tests.fakes.gpib_usb import (STATUS_8, T3S, ScriptedTransport, attach_script, attached, h,
                                  reattach_after_usb_fault_script, regwrite_reply, srq_arm, status_reply)


SRQ_PUSH = h('30 18 00 60 31 a1 01 00')  # srq.pcap 1.5266: status byte 0x60 = RQS | ESB
BENCH_PUSH = h('30 03 00 60 31 a1 01 00')  # unit 01CEE482, 2026-09-23 (§10.11): bytes 1-2 not SRQI
NOTICE = h('31 a5 01 00')  # unit 013CC9DF, 2026-09-23 (§10.12): no status byte, the device not polled
ACK_3B = ('ctrl_out', (0x40, 0x3B, 0, 0, b''))
NOTHING = ('intr', TransportTimeout('nothing'), 64)
ARM = srq_arm()
SRQI_ARM = srq_arm(ibsta=0x1068)  # §10.11: the write sent while SRQ was asserted


def waited(slices: int) -> list:
    """``slices`` slices with nothing pushed, each after its arming write."""
    return (ARM + [NOTHING]) * slices


def intr_slices(transport: ScriptedTransport) -> List[int]:
    return [tm for kind, _, tm in transport.timeouts if kind == 'intr']


def arms_sent(transport: ScriptedTransport) -> int:
    return transport.sent.count(p.ni_session_mark_message())


class TestArming:
    def test_the_arming_write_is_the_12_bytes_nis_driver_sends(self):
        assert p.ni_session_mark_message() == h('09 01 00 02 03 01 00 00 04 00 00 00')

    def test_the_write_goes_out_before_the_interrupt_read(self):
        controller, transport = attached(ARM + [('intr', SRQ_PUSH, 64), ACK_3B])
        assert controller.wait_srq(1.0) == 0x60
        transport.assert_done()
        assert transport.timeouts[-1] == ('intr', 64, 15)

    def test_the_write_is_sent_again_before_every_slice(self):
        # §10.4.3: NI's write every 15 ms while nothing is pending, about 65 in 1 s.
        controller, transport = attached(waited(66) + ARM + [('intr', SRQ_PUSH, 64), ACK_3B])
        assert controller.wait_srq(1.0) == 0x60
        transport.assert_done()
        assert arms_sent(transport) == 67
        assert intr_slices(transport) == [15] * 66 + [10]

    def test_every_wait_arms_again(self):
        # One write arms one push (§10.11): a second wait must not rely on the first one's.
        controller, transport = attached(ARM + [('intr', SRQ_PUSH, 64), ACK_3B]
                                         + ARM + [('intr', BENCH_PUSH, 64), ACK_3B])
        assert controller.wait_srq(1.0) == 0x60
        assert controller.wait_srq(1.0) == 0x60
        transport.assert_done()

    def test_the_benchs_push_yields_its_status_byte(self):
        controller, transport = attached(ARM + [('intr', BENCH_PUSH, 64), ACK_3B])
        assert controller.wait_srq(1.0) == 0x60
        transport.assert_done()

    def test_a_write_the_adapter_fails_ends_the_wait_with_its_error(self):
        controller, transport = attached([('out', p.ni_session_mark_message()),
                                          ('in', regwrite_reply(1, error=0x07), 16)])
        with pytest.raises(GpibError):
            controller.wait_srq(1.0)
        transport.assert_done()
        assert intr_slices(transport) == []


class TestSrqiInTheReply:
    def test_srqi_then_the_push_returns_its_status_byte_and_stops_the_writes(self):
        controller, transport = attached(SRQI_ARM + [NOTHING, NOTHING, ('intr', SRQ_PUSH, 64), ACK_3B])
        assert controller.wait_srq(1.0) == 0x60
        transport.assert_done()
        assert arms_sent(transport) == 1
        assert intr_slices(transport) == [15, 15, 15]

    def test_srqi_after_some_slices(self):
        controller, transport = attached(waited(3) + SRQI_ARM + [('intr', BENCH_PUSH, 64), ACK_3B])
        assert controller.wait_srq(1.0) == 0x60
        transport.assert_done()
        assert arms_sent(transport) == 4

    def test_srqi_with_no_push_is_a_request_whose_status_byte_is_not_known(self):
        assert SRQ_PUSH_AFTER_SRQI_S == 0.25
        controller, transport = attached(SRQI_ARM + [NOTHING] * 17)
        assert controller.wait_srq(1.0) is None
        transport.assert_done()   # no 0x3b: there was no push to acknowledge
        assert arms_sent(transport) == 1
        assert intr_slices(transport) == [15] * 16 + [10]

    def test_the_push_after_srqi_is_waited_for_past_a_shorter_timeout(self):
        # The request has been seen: the timeout no longer applies, only the wait for its push.
        controller, transport = attached(SRQI_ARM + [NOTHING] * 5 + [('intr', SRQ_PUSH, 64), ACK_3B])
        assert controller.wait_srq(0.01) == 0x60
        transport.assert_done()
        assert intr_slices(transport) == [15] * 6


class TestFourBytePacket:
    """Unit 013CC9DF: ``31 a5 nn 00`` after a write whose reply carried SRQI (§10.12)."""

    def test_the_packet_is_a_request_with_no_status_byte_returned_at_once(self):
        controller, transport = attached(SRQI_ARM + [('intr', NOTICE, 64), ACK_3B])
        assert controller.wait_srq(1.0) is None
        transport.assert_done()   # the 0x3b went out, as after the push
        assert arms_sent(transport) == 1
        assert intr_slices(transport) == [15]   # not the 0.25 s allowed for a push after SRQI

    def test_the_packet_without_srqi_first_is_taken_the_same_way(self):
        controller, transport = attached(waited(2) + ARM + [('intr', NOTICE, 64), ACK_3B])
        assert controller.wait_srq(1.0) is None
        transport.assert_done()
        assert intr_slices(transport) == [15, 15, 15]

    def test_several_in_a_row_in_one_session(self):
        # §10.12: 31 a5 01 00, 31 a5 02 00, 31 a5 03 00 on three rounds, the 0x3b after each.
        script = []
        for n in (1, 2, 3):
            script += SRQI_ARM + [('intr', bytes((0x31, 0xA5, n, 0x00)), 64), ACK_3B]
        controller, transport = attached(script)
        assert [controller.wait_srq(1.0) for _ in range(3)] == [None, None, None]
        transport.assert_done()
        assert intr_slices(transport) == [15, 15, 15]
        assert arms_sent(transport) == 3

    def test_the_8_byte_push_after_4_byte_packets_still_yields_its_status_byte(self):
        controller, transport = attached(SRQI_ARM + [('intr', NOTICE, 64), ACK_3B]
                                         + SRQI_ARM + [('intr', h('31 a5 02 00'), 64), ACK_3B]
                                         + ARM + [('intr', BENCH_PUSH, 64), ACK_3B])
        assert controller.wait_srq(1.0) is None
        assert controller.wait_srq(1.0) is None
        assert controller.wait_srq(1.0) == 0x60
        transport.assert_done()

    @pytest.mark.parametrize('packet', ['31', '31 a5', '31 a5 01', '30 18 00 60', '31 a1 01 00',
                                        '31 a5 01 00 00', '30 18 00 60 31 a1 01'])
    def test_a_packet_of_no_form_seen_is_a_protocol_error_without_an_acknowledge(self, packet):
        controller, transport = attached(SRQI_ARM + [('intr', h(packet), 64)] + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(ProtocolError):
            controller.wait_srq(1.0)
        controller.command(b'\x14', timeout_s=3.0)  # no re-attach: the bulk pipes were untouched
        transport.assert_done()


class TestWaitSrq:
    def test_nothing_pending_is_a_timeout_without_an_acknowledge(self):
        controller, transport = attached(waited(17))
        with pytest.raises(GpibTimeout):
            controller.wait_srq(0.25)
        transport.assert_done()
        assert intr_slices(transport) == [15] * 16 + [10]

    def test_the_wait_is_sliced_so_a_close_can_be_noticed(self):
        assert SRQ_WAIT_SLICE_S == 0.015
        controller, transport = attached(waited(2) + ARM + [('intr', SRQ_PUSH, 64), ACK_3B])
        assert controller.wait_srq(0.04) == 0x60
        assert intr_slices(transport) == [15, 15, 10]
        controller, transport = attached(waited(3))
        with pytest.raises(GpibTimeout):
            controller.wait_srq(0.04)
        transport.assert_done()

    @pytest.mark.parametrize('timeout_s, slices', [(0.031, [15, 15, 1]), (0.0151, [15]),
                                                   (0.0004, [1]), (0.03, [15, 15])])
    def test_no_slice_is_ever_zero_milliseconds(self, timeout_s, slices):
        # libusb reads a timeout of 0 as no timeout at all: the read would never return and
        # close() would release the transport under it.
        controller, transport = attached(waited(len(slices)))
        with pytest.raises(GpibTimeout):
            controller.wait_srq(timeout_s)
        transport.assert_done()
        assert intr_slices(transport) == slices

    def test_infinite_wait_uses_the_controller_wait_in_slices(self):
        controller, transport = attached(waited(1) + ARM + [('intr', SRQ_PUSH, 64), ACK_3B],
                                         infinite_wait_s=0.02)
        controller.wait_srq(None)
        assert intr_slices(transport) == [15, 5]

    def test_close_during_a_wait_ends_the_wait_first_and_then_releases_the_transport(self):
        import threading

        class Closing(ScriptedTransport):
            controller: Controller
            closer: threading.Thread

            def interrupt_in(self, length, timeout_ms):
                # The first slice: another thread closes the controller while we block; it must
                # not get past close() until this wait has left.
                self.closer = threading.Thread(target=self.controller.close)
                self.closer.start()
                self.closer.join(0.2)
                assert self.closer.is_alive(), 'close() returned while the interrupt read was pending'
                assert not self.closed
                return super().interrupt_in(length, timeout_ms)

        transport = Closing(attach_script() + ARM + [
            ('intr', TransportTimeout('slice over'), 64),
            # No second write: the closed controller sends nothing more for the wait. Only after
            # the wait has left does close() reach the bus and the transport.
            ('out', p.register_write_message(t.SHUTDOWN_WRITES)), ('in', regwrite_reply(2), 16),
        ])
        controller = Controller(transport, t.PID_HS, sleep=lambda s: None)
        transport.controller = controller
        controller.attach()
        with pytest.raises(AdapterNotReady):
            controller.wait_srq(10.0)
        transport.closer.join(2.0)
        assert not transport.closer.is_alive() and transport.closed
        transport.assert_done()

    def test_push_arriving_as_the_controller_closes_is_not_acknowledged(self):
        class Closing(ScriptedTransport):
            controller: Controller

            def interrupt_in(self, length, timeout_ms):
                push = super().interrupt_in(length, timeout_ms)
                self.controller._closed = True  # closed between the push and the acknowledge
                return push

        transport = Closing(attach_script() + ARM + [('intr', SRQ_PUSH, 64)])
        controller = Controller(transport, t.PID_HS, sleep=lambda s: None)
        transport.controller = controller
        controller.attach()
        with pytest.raises(AdapterNotReady):
            controller.wait_srq(1.0)
        transport.assert_done()

    @pytest.mark.parametrize('timeout_s', [0, 0.0, -1.0, -0.001])
    def test_a_non_positive_timeout_is_refused_before_any_read(self, timeout_s):
        # A 0 ms interrupt read would block without limit (libusb: 0 = no timeout), unsliced,
        # which is the pending-transfer hazard the slicing exists to avoid.
        controller, transport = attached([])
        with pytest.raises(ValueError):
            controller.wait_srq(timeout_s)
        transport.assert_done()
        assert intr_slices(transport) == []
        assert controller._srq_idle.is_set()  # nothing was left half-armed

    def test_a_wait_sees_the_adapter_gone_that_another_thread_found(self):
        # The lock is released while the interrupt read blocks; an operation in between that
        # finds the adapter gone must end the wait before its next write touches USB.
        class Found(ScriptedTransport):
            controller: Controller

            def interrupt_in(self, length, timeout_ms):
                with pytest.raises(AdapterGone):
                    self.controller.status()   # another thread, while this read blocks
                return super().interrupt_in(length, timeout_ms)

        transport = Found(attach_script() + ARM + [
            ('ctrl', (0x21, 0x0200, 0, 8), TransportGone('the device is no longer on the USB bus')),
            ('intr', TransportTimeout('nothing'), 64),
        ])
        controller = Controller(transport, t.PID_HS, sleep=lambda s: None)
        transport.controller = controller
        controller.attach()
        with pytest.raises(AdapterGone):
            controller.wait_srq(5.0)
        transport.assert_done()   # no second write, no second interrupt read

    def test_the_wait_for_the_push_after_srqi_sees_a_close(self):
        class Closing(ScriptedTransport):
            controller: Controller

            def interrupt_in(self, length, timeout_ms):
                self.controller._closed = True
                return super().interrupt_in(length, timeout_ms)

        transport = Closing(attach_script() + SRQI_ARM + [NOTHING])
        controller = Controller(transport, t.PID_HS, sleep=lambda s: None)
        transport.controller = controller
        controller.attach()
        with pytest.raises(AdapterNotReady):
            controller.wait_srq(1.0)
        transport.assert_done()

    def test_only_one_wait_at_a_time(self):
        class Nested(ScriptedTransport):
            controller: Controller
            second: Optional[Exception] = None

            def interrupt_in(self, length, timeout_ms):
                try:
                    self.controller.wait_srq(1.0)
                except Exception as exc:  # noqa: BLE001 - recorded for the assertion below
                    self.second = exc
                return super().interrupt_in(length, timeout_ms)

        transport = Nested(attach_script() + ARM + [('intr', SRQ_PUSH, 64), ACK_3B])
        controller = Controller(transport, t.PID_HS, sleep=lambda s: None)
        transport.controller = controller
        controller.attach()
        assert controller.wait_srq(1.0) == 0x60
        assert isinstance(transport.second, GpibError) and 'in progress' in str(transport.second)
        transport.assert_done()

    def test_the_lock_is_free_while_the_wait_blocks(self):
        import threading

        class Waiting(ScriptedTransport):
            controller: Controller

            def interrupt_in(self, length, timeout_ms):
                # Another thread must be able to use the adapter meanwhile.
                done = threading.Event()
                thread = threading.Thread(target=lambda: (self.controller.status(), done.set()))
                thread.start()
                thread.join(2.0)
                assert done.is_set(), 'controller.status() blocked behind the interrupt read'
                return super().interrupt_in(length, timeout_ms)

        transport = Waiting(attach_script() + ARM + [
            ('ctrl', (0x21, 0x0200, 0, 8), STATUS_8),   # the other thread's status query
            ('intr', SRQ_PUSH, 64), ACK_3B,
        ])
        controller = Controller(transport, t.PID_HS, sleep=lambda s: None)
        transport.controller = controller
        controller.attach()
        assert controller.wait_srq(1.0) == 0x60
        transport.assert_done()

    def test_usb_error_on_the_interrupt_endpoint_marks_a_reattach(self):
        controller, transport = attached(ARM + [
            ('intr', TransportError('device gone'), 64),
        ] + reattach_after_usb_fault_script() + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(TransportError):
            controller.wait_srq(1.0)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_short_push_is_a_protocol_error_without_a_resync(self):
        controller, transport = attached(ARM + [('intr', h('30 18'), 64)] + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(ProtocolError):
            controller.wait_srq(1.0)
        controller.command(b'\x14', timeout_s=3.0)  # no re-attach: the bulk pipes were untouched
        transport.assert_done()

    def test_wait_before_attach_or_after_close_raises(self):
        controller = Controller(ScriptedTransport([]), t.PID_HS)
        with pytest.raises(AdapterNotReady):
            controller.wait_srq(1.0)
        controller, _ = attached([('out', p.register_write_message(t.SHUTDOWN_WRITES)), ('in', regwrite_reply(2), 16)])
        controller.close()
        with pytest.raises(AdapterNotReady):
            controller.wait_srq(1.0)

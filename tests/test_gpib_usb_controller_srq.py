"""Controller: the wait for a service request (§10.4)."""
from typing import Optional

import pytest

from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t
from resistamet_gui.gpib_usb.controller import SRQ_WAIT_SLICE_S, Controller
from resistamet_gui.gpib_usb.protocol import AdapterNotReady, GpibError, GpibTimeout, ProtocolError
from resistamet_gui.gpib_usb.transport import TransportError, TransportTimeout
from tests.fakes.gpib_usb import (STATUS_8, T3S, ScriptedTransport, attach_script, attached, h,
                                  reattach_after_usb_fault_script, regwrite_reply, status_reply)


SRQ_PUSH = h('30 18 00 60 31 a1 01 00')  # srq.pcap 1.5266: status byte 0x60 = RQS | ESB
ACK_3B = ('ctrl_out', (0x40, 0x3B, 0, 0, b''))


class TestWaitSrq:
    def test_push_yields_the_status_byte_and_is_acknowledged(self):
        controller, transport = attached([('intr', SRQ_PUSH, 64), ACK_3B])
        assert controller.wait_srq(1.0) == 0x60
        transport.assert_done()
        assert transport.timeouts[-1] == ('intr', 64, 1000)

    def test_nothing_pending_is_a_timeout_without_an_acknowledge(self):
        controller, transport = attached([('intr', TransportTimeout('nothing'), 64)])
        with pytest.raises(GpibTimeout):
            controller.wait_srq(0.25)
        transport.assert_done()
        assert transport.timeouts[-1] == ('intr', 64, 250)

    def test_the_wait_is_sliced_so_a_close_can_be_noticed(self):
        assert SRQ_WAIT_SLICE_S == 1.0
        nothing = ('intr', TransportTimeout('nothing'), 64)
        controller, transport = attached([nothing, nothing, ('intr', SRQ_PUSH, 64), ACK_3B])
        assert controller.wait_srq(2.5) == 0x60
        assert [tm for kind, _, tm in transport.timeouts if kind == 'intr'] == [1000, 1000, 500]
        controller, transport = attached([nothing, nothing, nothing])
        with pytest.raises(GpibTimeout):
            controller.wait_srq(2.5)
        transport.assert_done()

    @pytest.mark.parametrize('timeout_s, slices', [(2.001, [1000, 1000, 1]), (1.0005, [1000]),
                                                   (0.0004, [1]), (2.0, [1000, 1000])])
    def test_no_slice_is_ever_zero_milliseconds(self, timeout_s, slices):
        # libusb reads a timeout of 0 as no timeout at all: the read would never return and
        # close() would release the transport under it.
        controller, transport = attached([('intr', TransportTimeout('nothing'), 64)] * len(slices))
        with pytest.raises(GpibTimeout):
            controller.wait_srq(timeout_s)
        transport.assert_done()
        assert [tm for kind, _, tm in transport.timeouts if kind == 'intr'] == slices

    def test_infinite_wait_uses_the_controller_wait_in_slices(self):
        controller, transport = attached([('intr', TransportTimeout('nothing'), 64), ('intr', SRQ_PUSH, 64), ACK_3B],
                                         infinite_wait_s=1.5)
        controller.wait_srq(None)
        assert [tm for kind, _, tm in transport.timeouts if kind == 'intr'] == [1000, 500]

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

        transport = Closing(attach_script() + [
            ('intr', TransportTimeout('slice over'), 64),
            # Only after the wait has left does close() reach the bus and the transport.
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

        transport = Closing(attach_script() + [('intr', SRQ_PUSH, 64)])
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
        assert [kind for kind, _, _ in transport.timeouts if kind == 'intr'] == []
        assert controller._srq_idle.is_set()  # nothing was left half-armed

    def test_only_one_wait_at_a_time(self):
        import threading

        class Nested(ScriptedTransport):
            controller: Controller
            second: Optional[Exception] = None

            def interrupt_in(self, length, timeout_ms):
                try:
                    self.controller.wait_srq(1.0)
                except Exception as exc:  # noqa: BLE001 - recorded for the assertion below
                    self.second = exc
                return super().interrupt_in(length, timeout_ms)

        transport = Nested(attach_script() + [('intr', SRQ_PUSH, 64), ACK_3B])
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

        transport = Waiting(attach_script() + [
            ('ctrl', (0x21, 0x0200, 0, 8), STATUS_8),   # the other thread's status query
            ('intr', SRQ_PUSH, 64), ACK_3B,
        ])
        controller = Controller(transport, t.PID_HS, sleep=lambda s: None)
        transport.controller = controller
        controller.attach()
        assert controller.wait_srq(1.0) == 0x60
        transport.assert_done()

    def test_usb_error_on_the_interrupt_endpoint_marks_a_reattach(self):
        controller, transport = attached([
            ('intr', TransportError('device gone'), 64),
        ] + reattach_after_usb_fault_script() + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(TransportError):
            controller.wait_srq(1.0)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_short_push_is_a_protocol_error_without_a_resync(self):
        controller, transport = attached([('intr', h('30 18'), 64)] + [
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

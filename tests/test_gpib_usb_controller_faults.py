"""Controller: the fault rule of §8.2 and the stale reply a USB fault leaves queued."""
import pytest

from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t
from resistamet_gui.gpib_usb.controller import DRAIN_WAIT_S, Controller
from resistamet_gui.gpib_usb.protocol import AdapterNotReady, GpibError, ProtocolError
from resistamet_gui.gpib_usb.transport import TransportError, TransportTimeout
from tests.fakes.gpib_usb import (CLEAR_HALTS, DRAIN, DRAIN_LENGTH, RAW_DRAIN, STOP, T3S, QueueingAdapter,
                                  ScriptedTransport, address_listener, attach_script, attached, attached_ni, h,
                                  reattach_after_usb_fault_script, reattach_script, regread_reply,
                                  regwrite_reply, status_reply)


class TestFaults:
    def test_malformed_reply_stops_drains_and_reattaches_before_the_next_operation(self):
        controller, transport = attached([
            ('out', p.command_message(t.address_listener_command(0, 22), T3S)),
            ('in', status_reply(0x0D)),                       # wrong id echoed
            STOP, ('in', b'\x00' * 12, DRAIN_LENGTH),   # a stale reply drained
        ] + reattach_script() + address_listener() + [
            ('out', p.write_message(b'A', T3S, True)), ('in', status_reply(0x0D)),
        ])
        with pytest.raises(ProtocolError):
            controller.write(22, b'A', timeout_s=3.0)
        assert controller.write(22, b'A', timeout_s=3.0) == 1
        transport.assert_done()
        drain = [tm for kind, length, tm in transport.timeouts if kind == 'in' and length == DRAIN_LENGTH]
        assert drain == [int(DRAIN_WAIT_S * 1000)]
        assert not [kind for kind, _, _ in transport.timeouts if kind == 'raw_in']

    def test_with_ni_instructions_on_the_alternate_in_is_drained_as_well(self):
        controller, transport = attached_ni([
            ('out', p.command_message(t.address_listener_command(0, 22), T3S)),
            ('in', status_reply(0x0D)),                       # wrong id echoed
            STOP, ('in', b'\x00' * 12, DRAIN_LENGTH), RAW_DRAIN,
        ])
        with pytest.raises(ProtocolError):
            controller.write(22, b'A', timeout_s=3.0)
        transport.assert_done()
        raw_drain = [tm for kind, length, tm in transport.timeouts if kind == 'raw_in']
        assert raw_drain == [int(DRAIN_WAIT_S * 1000)]

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
        ] + reattach_after_usb_fault_script() + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(TransportError):
            controller.command(b'\x14', timeout_s=3.0)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_the_first_attach_resets_no_pipe(self):
        # The attach of a healthy adapter is bench-proven as it is; the resets belong to the
        # re-attach after a fault alone. An unscripted clear_halt would fail assert_done.
        _, transport = attached([])
        transport.assert_done()
        assert not [step for step in transport.script if step[0] == 'clear_halt']

    def test_a_pipe_reset_that_fails_before_the_reattach_is_logged_and_the_attach_goes_ahead(self, caplog):
        resets = [('clear_halt', 0x06, TransportError('clear halt failed')), ('clear_halt', 0x02),
                  ('clear_halt', 0x84, TransportError('clear halt failed')), ('clear_halt', 0x88)]
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', TransportError('pipe stalled')),
        ] + resets + DRAIN + attach_script() + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(TransportError):
            controller.command(b'\x14', timeout_s=3.0)
        with caplog.at_level('WARNING', logger='resistamet_gui.gpib_usb.controller'):
            assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()
        failed = [r.getMessage() for r in caplog.records if 'clearing the halt' in r.getMessage()]
        assert len(failed) == 2 and '0x06' in failed[0] and '0x84' in failed[1]

    def test_a_transport_without_clear_halt_still_reattaches(self):
        class NoClearHalt(ScriptedTransport):
            clear_halt = None  # type: ignore[assignment]

        transport = NoClearHalt(attach_script() + [
            ('out', p.command_message(b'\x14', T3S)), ('in', TransportError('pipe stalled')),
        ] + DRAIN + attach_script() + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        controller = Controller(transport, t.PID_HS, sleep=lambda s: None)
        controller.attach()
        with pytest.raises(TransportError):
            controller.command(b'\x14', timeout_s=3.0)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_a_model_without_the_alternate_pair_resets_its_primary_pipes_only(self):
        usb_b_attach = [
            ('out', p.register_read_message(t.USB_B_SERIAL_REGISTERS)),
            ('in', regread_reply([0x78, 0x56, 0x34, 0x12]), 32),
        ] + attach_script()[2:]
        transport = ScriptedTransport(usb_b_attach + [
            ('out', p.command_message(b'\x14', T3S)), ('in', TransportError('pipe stalled')),
            ('clear_halt', 0x02), ('clear_halt', 0x82),
        ] + DRAIN + usb_b_attach + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        controller = Controller(transport, t.PID_USB_B, sleep=lambda s: None)
        controller.attach()
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

    def test_reattach_failure_surfaces_as_not_ready_and_is_retried(self):
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', h('0c 00'), 12),
            STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH),
        ] + CLEAR_HALTS + [
            ('ctrl', (0x41, 0, 0, 16), h('00 00 00 00 00')),       # re-attach 1 fails
        ] + reattach_after_usb_fault_script() + [                  # re-attach 2 drains again, succeeds
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(ProtocolError):
            controller.command(b'\x14', timeout_s=3.0)
        with pytest.raises(AdapterNotReady):
            controller.command(b'\x14', timeout_s=3.0)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_reattach_failing_with_a_bus_error_is_retried_too(self):
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', TransportError('pipe stalled')),
        ] + reattach_after_usb_fault_script(take_control_error=3) + reattach_after_usb_fault_script() + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(TransportError):
            controller.command(b'\x14', timeout_s=3.0)
        with pytest.raises(GpibError) as info:
            controller.command(b'\x14', timeout_s=3.0)
        assert info.value.code == 3
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_fault_during_the_reattach_drains_exactly_once_more(self):
        bad_init = attach_script()[:4]
        bad_init[3] = ('in', regwrite_reply(20), 16)               # malformed mid-attach
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', h('0c 00'), 12),
            STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH),
        ] + CLEAR_HALTS + bad_init + [
            STOP, ('in', TransportTimeout('drained again'), DRAIN_LENGTH),
        ] + reattach_script() + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(ProtocolError):
            controller.command(b'\x14', timeout_s=3.0)
        with pytest.raises(ProtocolError):
            controller.command(b'\x14', timeout_s=3.0)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()


class TestStaleReplyAfterAUsbFault:
    """The operation after a fault is the run's ``:OUTP OFF``; it must not be the one that pays for it."""

    @staticmethod
    def faulted(adapter: QueueingAdapter) -> Controller:
        controller = Controller(adapter, t.PID_HS, sleep=lambda s: None)
        controller.attach()
        return controller

    def test_outp_off_lands_on_the_first_attempt_after_a_usb_error_on_a_reply(self):
        adapter = QueueingAdapter()
        controller = self.faulted(adapter)
        adapter.fail_in = TransportError('EIO')      # the addressing reply stays queued
        with pytest.raises(TransportError):
            controller.read(24, max_bytes=20480, timeout_s=5.0)
        assert controller.write(24, b':OUTP OFF\n', timeout_s=5.0) == 10
        assert adapter.written == [b':OUTP OFF\n'] and adapter.queue == []

    def test_outp_off_lands_on_the_first_attempt_after_a_stop_request_that_timed_out(self):
        adapter = QueueingAdapter()
        controller = self.faulted(adapter)
        adapter.fail_in = TransportTimeout('host wait')
        adapter.fail_stop = TransportTimeout('control timeout')
        with pytest.raises(TransportTimeout):
            controller.read(24, max_bytes=20480, timeout_s=5.0)
        assert controller.write(24, b':OUTP OFF\n', timeout_s=5.0) == 10
        assert adapter.written == [b':OUTP OFF\n'] and adapter.queue == []

    def test_a_usb_error_drains_behind_the_pipe_resets_of_the_reattach(self):
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', TransportError('EIO')),
        ] + reattach_after_usb_fault_script(stale=status_reply(0x0C)) + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(TransportError):
            controller.command(b'\x14', timeout_s=3.0)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()
        drain = [tm for kind, length, tm in transport.timeouts if kind == 'in' and length == DRAIN_LENGTH]
        assert drain == [int(DRAIN_WAIT_S * 1000)]

    def test_a_stop_request_that_fails_is_a_fault(self):
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', TransportTimeout('host wait')),
            ('ctrl', (0x20, 0, 0, 8), TransportTimeout('control timeout')),
        ] + reattach_after_usb_fault_script(stale=status_reply(0x0C, error=1)) + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(TransportTimeout):
            controller.command(b'\x14', timeout_s=3.0)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_a_message_the_adapter_did_not_take_in_time_is_a_fault(self):
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S), TransportTimeout('OUT not accepted')),
        ] + reattach_after_usb_fault_script() + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(TransportTimeout):
            controller.command(b'\x14', timeout_s=3.0)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_with_ni_instructions_on_the_alternate_in_is_drained_too(self):
        controller, transport = attached_ni([
            ('out', p.command_message(b'\x14', T3S)), ('in', TransportError('EIO')),
        ] + reattach_after_usb_fault_script(stale=status_reply(0x0C), raw=True) + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(TransportError):
            controller.command(b'\x14', timeout_s=3.0)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_a_malformed_reply_is_drained_once_not_again_at_the_reattach(self):
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', h('0c 00'), 12),
            STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH),
        ] + reattach_script() + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(ProtocolError):
            controller.command(b'\x14', timeout_s=3.0)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_a_usb_error_during_the_reattach_drains_once_more(self):
        failed_attach = attach_script()[:3] + [('in', TransportError('EIO again'), 16)]
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', TransportError('EIO')),
        ] + CLEAR_HALTS + [STOP, ('in', status_reply(0x0C), DRAIN_LENGTH)] + failed_attach
          + reattach_after_usb_fault_script(stale=regwrite_reply(26)) + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(TransportError):
            controller.command(b'\x14', timeout_s=3.0)
        with pytest.raises(TransportError):
            controller.command(b'\x14', timeout_s=3.0)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

"""Controller: the fault rule of §8.2 and the stale reply a USB fault leaves queued."""
import errno

import pytest

from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t
from resistamet_gui.gpib_usb.controller import DRAIN_WAIT_S, Controller
from resistamet_gui.gpib_usb.link import PRESENCE_POLL_S, PRESENCE_SETTLE_S
from resistamet_gui.gpib_usb.protocol import AdapterGone, AdapterNotReady, GpibError, ProtocolError
from resistamet_gui.gpib_usb.transport import TransportError, TransportGone, TransportStall, TransportTimeout
from tests.fakes.gpib_usb import (CLEAR_HALTS, DRAIN, DRAIN_LENGTH, RAW_DRAIN, STOP, T3S, QueueingAdapter,
                                  ScriptedTransport, address_listener, address_talker, attach_script, attached,
                                  attached_ni, h, ni_session, ni_write, reattach_after_usb_fault_script,
                                  reattach_script, regread_reply, regwrite_reply, srq_arm, status_reply)


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
        assert transport.halts_cleared == []

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

    def test_a_message_the_adapter_did_not_take_in_time_is_the_hung_adapter(self):
        # §8.17: a working adapter takes a message at once, the reply to the one before having
        # been read; one whose OUT FIFO is full NAKs it. Reported with the replug advice, and the
        # next operation re-attaches, which finds the state again or, here, an adapter that works.
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S), TransportTimeout('OUT not accepted')),
        ] + reattach_after_usb_fault_script() + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(AdapterNotReady) as info:
            controller.command(b'\x14', timeout_s=3.0)
        assert 'the adapter is hung. Unplug it and plug it back in.' in str(info.value)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_the_raw_instructions_header_not_taken_is_the_hung_adapter_too(self):
        controller, transport = attached_ni([
            ('out', p.read_raw_message(20480, T3S), TransportTimeout('OUT not accepted')),
        ])
        with pytest.raises(AdapterNotReady) as info:
            controller.read_raw(20480, timeout_s=3.0)
        assert 'hung' in str(info.value)
        transport.assert_done()

    def test_a_write_whose_data_the_bus_does_not_take_stays_a_transfer_timeout(self):
        # The 0x0d carries its data, and the adapter takes the message only as fast as the
        # instrument takes the bytes (§10.5.2): a timeout there is the transfer's, not a hang.
        controller, transport = attached([
            ('out', p.write_message(b'A', T3S, True), TransportTimeout('listener stopped')),
        ] + reattach_after_usb_fault_script() + [
            ('out', p.write_message(b'A', T3S, True)), ('in', status_reply(0x0D)),
        ])
        with pytest.raises(TransportTimeout) as info:
            controller.write_raw(b'A', timeout_s=3.0)
        assert not isinstance(info.value, AdapterNotReady)
        assert controller.write_raw(b'A', timeout_s=3.0) == 1   # a fault: re-attached first
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


def unplugged() -> TransportGone:
    """What the transport raises for libusb's "no such device" (errno 19 on the bench, §11.2)."""
    return TransportGone('bulk read failed: the device is no longer on the USB bus')


class TestAdapterGone:
    """Spec §11.2, "Hot-unplug mid-run": the adapter is gone, so nothing is recovered."""

    def test_an_unplug_mid_read_ends_the_read_at_once_with_no_recovery(self):
        # The bench ran clear halts on four pipes, a stop request, a drain and a re-attach on a
        # handle that no longer existed. None of them may happen: the script ends at the error.
        controller, transport = attached(address_talker(pad=24) + [
            ('out', p.read_message(1024, T3S)), ('in', unplugged()),
        ])
        with pytest.raises(AdapterGone) as info:
            controller.read(24, max_bytes=20480, timeout_s=3.0)
        assert 'no longer on the USB bus' in str(info.value) and 'Plug it back in' in str(info.value)
        assert isinstance(info.value, AdapterNotReady) and isinstance(info.value.__cause__, TransportGone)
        assert controller.adapter_gone
        transport.assert_done()

    def test_every_later_operation_fails_the_same_way_without_touching_usb(self):
        controller, transport = attached(address_listener(pad=24) + [
            ('out', p.write_message(b':OUTP OFF\n', T3S, True), unplugged()),
        ])
        with pytest.raises(AdapterGone):
            controller.write(24, b':OUTP OFF\n', timeout_s=3.0)
        # The run's cleanup tries the output twice; neither reaches USB (an unscripted call
        # would fail assert_done), and neither re-attaches.
        for operation in (lambda: controller.write(24, b':OUTP OFF\n', timeout_s=3.0),
                          lambda: controller.read(24, max_bytes=10, timeout_s=3.0),
                          lambda: controller.command(b'\x14'), controller.status, controller.bus_lines,
                          controller.interface_clear, controller.attach):
            with pytest.raises(AdapterGone):
                operation()
        controller.close()
        transport.assert_done()
        assert transport.closed  # the handle is released; no shutdown write was sent

    def test_the_adapter_gone_during_the_recovery_from_another_fault_ends_it(self):
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', TransportError('EIO')),
            ('clear_halt', 0x06, unplugged()),
        ])
        with pytest.raises(TransportError):
            controller.command(b'\x14', timeout_s=3.0)
        with pytest.raises(AdapterGone):
            controller.command(b'\x14', timeout_s=3.0)  # no more resets, no stop request, no attach
        transport.assert_done()

    def test_the_adapter_gone_while_draining_a_malformed_reply_is_reported_as_gone(self):
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', h('0c 00'), 12),
            STOP, ('in', unplugged(), DRAIN_LENGTH),
        ])
        with pytest.raises(AdapterGone):
            controller.command(b'\x14', timeout_s=3.0)
        transport.assert_done()

    def test_a_raw_write_whose_data_finds_the_adapter_gone_reads_no_reply_and_resets_no_pipe(self):
        controller, transport = attached_ni(ni_session(pad=24) + [
            ('out', ni_write(2049, pad=24)), ('raw_out', bytes(2049), unplugged()),
        ])
        with pytest.raises(AdapterGone):
            controller.write(24, bytes(2049), timeout_s=3.0)
        transport.assert_done()

    def test_a_wait_for_a_service_request_that_finds_the_adapter_gone(self):
        controller, transport = attached(srq_arm() + [('intr', unplugged(), 64)])
        with pytest.raises(AdapterGone):
            controller.wait_srq(1.0)
        with pytest.raises(AdapterGone):
            controller.wait_srq(1.0)
        transport.assert_done()

    def test_a_close_that_finds_the_adapter_gone_records_it(self):
        # Nothing before the close saw the unplug; its shutdown write does. The controller must
        # say so, for the board registry to look for the device that comes back, and a later
        # call must fail as gone without a USB call (an unscripted one fails assert_done).
        controller, transport = attached([
            ('out', p.register_write_message(t.SHUTDOWN_WRITES), unplugged()),
        ])
        assert not controller.adapter_gone
        controller.close()
        assert controller.adapter_gone and transport.closed
        with pytest.raises(AdapterGone):
            controller.status()
        transport.assert_done()


def io_error() -> TransportError:
    """macOS, the transfer in flight when the cable is pulled: errno 5 (§10.11)."""
    error = TransportError('bulk read failed: [Errno 5] Input/Output Error')
    error.errno = errno.EIO
    return error


def other_error() -> TransportError:
    """macOS, every later request on the open handle: libusb's "Other error", no errno (§10.11)."""
    error = TransportError('control request 0x20 failed: Other error')
    error.backend_code = -99
    return error


def absent(extra, build=attached):
    """``attached`` (or ``attached_ni``), with the adapter no longer found on the bus from the first fault on."""
    controller, transport = build(extra)
    transport.present = False
    return controller, transport


class TestAdapterGoneOnMacos:
    """§10.11: on macOS the open handle never says "no such device"; the bus is looked at instead."""

    def test_an_unplug_mid_read_is_gone_on_the_first_failed_call_with_no_usb_after_it(self):
        script = address_talker(pad=24) + [('out', p.read_message(1024, T3S)), ('in', io_error())]
        controller, transport = absent(script)
        with pytest.raises(AdapterGone) as info:
            controller.read(24, max_bytes=20480, timeout_s=3.0)
        assert 'no longer on the USB bus' in str(info.value)
        assert isinstance(info.value.__cause__, TransportGone)
        assert info.value.__cause__.errno == errno.EIO
        assert controller.adapter_gone
        transport.assert_done()   # no pipe reset, stop request, drain or re-attach
        assert transport.presence_checks == [len(attach_script()) + len(script)]
        # The run's cleanup: every later call fails the same way and touches no USB.
        for operation in (lambda: controller.write(24, b':OUTP OFF\n', timeout_s=3.0),
                          controller.status, controller.attach):
            with pytest.raises(AdapterGone):
                operation()
        controller.close()
        transport.assert_done()
        assert transport.closed

    def test_other_error_on_the_first_call_after_an_idle_unplug_is_gone_as_well(self):
        controller, transport = absent([
            ('out', p.command_message(t.address_listener_command(0, 24), T3S), other_error()),
        ])
        with pytest.raises(AdapterGone):
            controller.write(24, b':OUTP OFF\n', timeout_s=3.0)
        transport.assert_done()

    @pytest.mark.parametrize('present, looks_at_the_fault', [(True, 21), (None, 1)])
    def test_a_real_fault_with_the_adapter_present_is_recovered_as_before(self, present, looks_at_the_fault):
        # None: a transport that cannot tell, and nothing is waited for. Either way the pipes are
        # reset, the stale reply drained and the attach re-run, as for any USB error.
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', io_error()),
        ] + reattach_after_usb_fault_script() + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        transport.present = present
        slept = []
        controller._link.sleep = slept.append
        with pytest.raises(TransportError) as info:
            controller.command(b'\x14', timeout_s=3.0)
        assert not isinstance(info.value, TransportGone) and not controller.adapter_gone
        # The extra delay a present adapter pays for the look at the bus is bounded: 100 ms.
        assert len(transport.presence_checks) == looks_at_the_fault
        assert slept == [PRESENCE_POLL_S] * (looks_at_the_fault - 1)
        assert sum(slept) == pytest.approx(PRESENCE_SETTLE_S if present else 0.0)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()
        assert len(transport.presence_checks) == looks_at_the_fault + 1   # and one before the re-attach

    def test_the_bench_sequence_waits_for_libusb_to_drop_the_adapter(self):
        # macOS, 2026-09-23: the adapter was still listed when the first I/O error arrived and
        # for some milliseconds after; the look at the bus waits for it to go instead of
        # starting the recovery (which then failed ten requests in a row on the bench).
        script = address_talker(pad=24) + [('out', p.read_message(1024, T3S)), ('in', io_error())]
        controller, transport = attached(script)
        transport.present = [True, True, True, False]
        slept = []
        controller._link.sleep = slept.append
        with pytest.raises(AdapterGone):
            controller.read(24, max_bytes=20480, timeout_s=3.0)
        transport.assert_done()   # no pipe reset, stop request, drain or re-attach
        assert len(transport.presence_checks) == 4 and slept == [PRESENCE_POLL_S] * 3

    def test_an_adapter_that_leaves_during_the_recovery_ends_it_at_the_first_failed_request(self):
        # Present through the whole wait at the fault and at the look before the re-attach; gone
        # when the first pipe reset fails with "Other error": nothing more is sent.
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', io_error()),
            ('clear_halt', 0x06, other_error()),
        ])
        with pytest.raises(TransportError):
            controller.command(b'\x14', timeout_s=3.0)
        transport.present = [True, False]
        with pytest.raises(AdapterGone):
            controller.command(b'\x14', timeout_s=3.0)
        assert controller.adapter_gone
        transport.assert_done()

    def test_the_stop_request_of_a_resync_failing_with_the_adapter_gone_ends_it(self):
        controller, transport = absent([
            ('out', p.command_message(b'\x14', T3S)), ('in', h('0c 00'), 12),
            ('ctrl', (0x20, 0, 0, 8), other_error()),
        ])
        with pytest.raises(AdapterGone):
            controller.command(b'\x14', timeout_s=3.0)   # no drain
        transport.assert_done()

    def test_a_recovery_request_that_fails_with_the_adapter_present_does_not_end_it(self):
        controller, transport = attached([
            ('out', p.command_message(b'\x14', T3S)), ('in', io_error()),
            ('clear_halt', 0x06, other_error()), ('clear_halt', 0x02), ('clear_halt', 0x84), ('clear_halt', 0x88),
            STOP, ('in', TransportTimeout('nothing to drain'), DRAIN_LENGTH),
        ] + attach_script() + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(TransportError):
            controller.command(b'\x14', timeout_s=3.0)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_an_adapter_that_leaves_after_the_fault_is_gone_before_the_re_attach(self):
        controller, transport = attached([('out', p.command_message(b'\x14', T3S)), ('in', io_error())])
        with pytest.raises(TransportError):
            controller.command(b'\x14', timeout_s=3.0)
        transport.present = False
        with pytest.raises(AdapterGone):
            controller.command(b'\x14', timeout_s=3.0)   # no pipe reset, no stop request, no attach
        transport.assert_done()

    def test_a_timeout_or_a_stall_is_not_looked_into(self):
        # Both are answers from a device that is there.
        controller, transport = absent([('out', p.command_message(b'\x14', T3S), TransportStall('refused'))])
        with pytest.raises(TransportStall):
            controller.command(b'\x14', timeout_s=3.0)
        assert controller._link.gone_instead(TransportTimeout('no reply')) is None
        assert controller._link.gone_instead(TransportGone('said so itself')) is None
        assert not controller.adapter_gone and transport.presence_checks == []
        transport.assert_done()

    def test_the_linux_sequence_is_unchanged_and_needs_no_look_at_the_bus(self):
        controller, transport = absent(address_talker(pad=24) + [
            ('out', p.read_message(1024, T3S)), ('in', unplugged()),
        ])
        with pytest.raises(AdapterGone) as info:
            controller.read(24, max_bytes=20480, timeout_s=3.0)
        assert info.value.__cause__.errno is None   # the transport's own TransportGone
        transport.assert_done()
        assert transport.presence_checks == []

    def test_a_raw_write_whose_data_fails_with_the_adapter_gone_reads_no_reply_and_resets_no_pipe(self):
        controller, transport = absent(ni_session(pad=24) + [
            ('out', ni_write(2049, pad=24)), ('raw_out', bytes(2049), io_error()),
        ], build=attached_ni)
        with pytest.raises(AdapterGone):
            controller.write(24, bytes(2049), timeout_s=3.0)
        transport.assert_done()

    def test_a_wait_for_a_service_request_whose_read_fails_with_the_adapter_gone(self):
        controller, transport = absent(srq_arm() + [('intr', io_error(), 64)])
        with pytest.raises(AdapterGone):
            controller.wait_srq(1.0)
        with pytest.raises(AdapterGone):
            controller.wait_srq(1.0)
        transport.assert_done()

    def test_a_close_whose_shutdown_write_finds_the_adapter_gone_records_it(self):
        controller, transport = absent([
            ('out', p.register_write_message(t.SHUTDOWN_WRITES), other_error()),
        ])
        controller.close()
        assert controller.adapter_gone and transport.closed
        transport.assert_done()

"""Controller: the attach sequence (§2.8), close (§2.9), and what either refuses."""
from typing import List

import pytest

from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t
from resistamet_gui.gpib_usb.controller import IFC_SETTLE_S, RECOVERY_WAIT_S, Controller
from resistamet_gui.gpib_usb.protocol import AdapterNotReady, GpibError, ProtocolError
from resistamet_gui.gpib_usb.transport import TransportTimeout
from tests.fakes.gpib_usb import (DRAIN_LENGTH, NOT_READY, READY, SERIAL_REPLY, SHORT_MS, STOP, T3S,
                                  ScriptedTransport, address_listener, attach_script, attached, h,
                                  regread_reply, regwrite_reply, status_reply)


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
        assert naps == [0.1] * 4 + [IFC_SETTLE_S]

    def test_attach_settles_after_ifc_and_ren_before_any_addressing(self):
        # Bench: a Keithley 2400 dropped command bytes sent within ~1 ms of IFC/REN.
        events: List[str] = []
        transport = ScriptedTransport(attach_script() + address_listener() + [
            ('out', p.write_message(b'A', T3S, True)), ('in', status_reply(0x0D)),
        ])
        original_out = transport.bulk_out

        def bulk_out(data, timeout_ms):
            events.append('out 0x%02x' % data[0])
            original_out(data, timeout_ms)
        transport.bulk_out = bulk_out  # type: ignore[assignment]
        controller = Controller(transport, t.PID_HS, sleep=lambda s: events.append('sleep %.1f' % s))
        controller.attach()
        controller.write(22, b'A', timeout_s=3.0)
        assert events == ['out 0x09', 'out 0x0f', 'out 0x09', 'out 0x01', 'sleep 0.1', 'out 0x0c', 'out 0x0d']

    def test_no_settle_when_not_system_controller(self):
        naps: List[float] = []
        script = attach_script()[:4]
        script[2] = ('out', p.register_write_message(t.register_init_writes(system_controller=False)))
        Controller(ScriptedTransport(script), t.PID_HS, sleep=naps.append).attach(system_controller=False)
        assert naps == []

    def test_public_interface_clear_settles_too(self):
        naps: List[float] = []
        transport = ScriptedTransport(attach_script() + [
            ('out', p.interface_clear_message()), ('in', status_reply(0x0F)),
        ])
        controller = Controller(transport, t.PID_HS, sleep=naps.append)
        controller.attach()
        controller.interface_clear()
        assert naps == [IFC_SETTLE_S, IFC_SETTLE_S]

    def test_hung_adapter_is_reported_with_the_replug_message(self):
        # Seen on the bench: control requests answered, init accepted, no bulk reply ever.
        script = attach_script()[:3] + [
            ('in', TransportTimeout('no reply'), 16),
            STOP,
            ('in', TransportTimeout('still no reply'), 16),
        ]
        transport = ScriptedTransport(script)
        controller = Controller(transport, t.PID_HS, sleep=lambda s: None)
        with pytest.raises(AdapterNotReady) as info:
            controller.attach()
        assert 'Unplug' in str(info.value)
        transport.assert_done()
        assert [tm for kind, _, tm in transport.timeouts if kind == 'in'] == [SHORT_MS, int(RECOVERY_WAIT_S * 1000)]

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

    def test_status_and_abort_after_close_are_refused_without_touching_the_transport(self):
        # pyusb reopens a handle it has released on the next request: a closed controller
        # must not make one.
        controller, transport = attached([('out', p.register_write_message(t.SHUTDOWN_WRITES)),
                                          ('in', regwrite_reply(2), 16)])
        controller.close()
        for operation in (controller.status, controller.abort):
            with pytest.raises(AdapterNotReady):
                operation()
        transport.assert_done()

    def test_close_without_attach_only_releases(self):
        transport = ScriptedTransport([])
        Controller(transport, t.PID_HS).close()
        assert transport.closed

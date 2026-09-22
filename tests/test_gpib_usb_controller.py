"""Controller and device_ops sequencing over a scripted transport.

The fake is a script: the exact bytes each ``bulk_out`` must carry and the
canned bytes each ``bulk_in`` / ``control_in`` returns, in order. Anything
off-script fails with a hex diff, so a wrong byte in an addressing sequence
reads as "expected 3f 40 36, got 3f 40 38 at offset 6", not as a hang. The
fake also records the timeout every bulk call was given, so the host-wait
rule of §7.2 is checked, not assumed.
"""
from typing import Any, List, Optional, Tuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from resistamet_gui.gpib_usb import device_ops as ops
from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t
from resistamet_gui.gpib_usb.controller import (DEFAULT_INFINITE_WAIT_S, DRAIN_WAIT_S, FRAMED_READ_MAX_BYTES,
                                                 IFC_SETTLE_S, RAW_READ_MIN_BYTES, BUS_MIN_RATE_BPS,
                                                 RAW_WRITE_MIN_BYTES, RECOVERY_WAIT_S, RAW_READ_SLICE_S,
                                                 RAW_REPLY_POLL_S, SRQ_WAIT_SLICE_S, Controller)
from resistamet_gui.gpib_usb.protocol import AdapterNotReady, GpibError, GpibTimeout, NoListener, ProtocolError
from resistamet_gui.gpib_usb.transport import TransportError, TransportStall, TransportTimeout
from tests.fakes.gpib_usb import (CLEAR_HALTS, DRAIN, DRAIN_LENGTH, NOT_READY, RAW_DRAIN, READY, SERIAL_REPLY,
                                  SHORT_MS, STATUS_8, STOP, T3S, WAIT_3S_MS, WAIT_10S_MS, WAIT_30S_MS,
                                  QueueingAdapter, ScriptedTransport, address_listener,
                                  address_talker, attach_script, attached, attached_ni, h, raw_read_reply,
                                  raw_wait_ms, raw_wait_slices, raw_write_reply, read_reply,
                                  reattach_after_usb_fault_script, reattach_script, regread_reply,
                                  regwrite_reply, status_reply, talking)


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
        assert controller.write_raw(b'', timeout_s=3.0) == 0
        transport.assert_done()

    def test_write_raw_sends_only_the_write_instruction(self):
        controller, transport = attached([
            ('out', h('0d fa ff fc 00 00 08 00 2a 49 44 4e 3f 0a 00 00 04 00 00 00')),
            ('in', status_reply(0x0D), 12),
            ('out', p.write_message(b'AB', T3S, send_eoi=False)), ('in', status_reply(0x0D)),
        ])
        assert controller.write_raw(b'*IDN?\n', timeout_s=3.0) == 6
        assert controller.write_raw(b'AB', send_eoi=False, timeout_s=3.0) == 2
        assert transport.in_timeouts_after(p.OP_WRITE) == [WAIT_3S_MS + 6, WAIT_3S_MS + 2]  # + 1 ms per byte
        transport.assert_done()

    def test_write_raw_error_8_raises_no_listener(self):
        controller, transport = attached([
            ('out', p.write_message(b'AB', T3S, send_eoi=True)),
            ('in', status_reply(0x0D, error=8, count=-2)),
        ])
        with pytest.raises(NoListener):
            controller.write_raw(b'AB', timeout_s=3.0)
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

    def test_framed_writes_split_at_0xffff_with_eoi_on_the_last(self):
        # A model without the alternate pair frames everything (§5.1 chunking).
        data = bytes(0xFFFF) + b'Z'
        script = [
            ('out', p.register_read_message(t.USB_B_SERIAL_REGISTERS)),
            ('in', regread_reply([0x78, 0x56, 0x34, 0x12]), 32),
        ] + attach_script()[2:] + address_listener() + [
            ('out', p.write_message(bytes(0xFFFF), T3S, send_eoi=False)), ('in', status_reply(0x0D)),
            ('out', p.write_message(b'Z', T3S, send_eoi=True)), ('in', status_reply(0x0D)),
        ]
        transport = ScriptedTransport(script)
        controller = Controller(transport, t.PID_USB_B, sleep=lambda s: None)
        controller.attach()
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


class TestRawWrite:
    """Writes of RAW_WRITE_MIN_BYTES and more: 0x0e, data on the alternate bulk OUT (§10.5.2)."""

    LONG = b'*CLS;' * 409 + b'*CL\r\n'  # 2050 bytes, as longwrite.pcap

    def test_2050_bytes_with_ni_bytes(self):
        # longwrite.pcap 1.8914 / 1.8916 / 2.0354: code 0xfe, termination character 0x0a, EOI.
        assert len(self.LONG) == 2050
        controller, transport = attached_ni(address_listener(pad=24, code=0xFE) + [
            ('out', h('0e 00 00 fe 00 0a 08 00 fe f7 ff ff 04 00 00 00')),
            ('raw_out', self.LONG),
            ('in', h('0e 00 28 00 00 00 00 00 04 00 00 00'), 512),
        ])
        assert controller.write(24, self.LONG, timeout_s=20.0, eos_char=0x0A) == 2050
        transport.assert_done()

    def test_the_threshold_is_ni_s_2048_2049(self):
        assert RAW_WRITE_MIN_BYTES == 2049  # §10.5.2: 2048 the last 0x0d, 2049 the first 0x0e
        under = bytes(RAW_WRITE_MIN_BYTES - 1)
        at = bytes(RAW_WRITE_MIN_BYTES)
        controller, transport = attached_ni(address_listener() + [
            ('out', p.write_message(under, T3S, True)), ('in', status_reply(0x0D)),
        ] + address_listener() + [
            ('out', p.write_raw_message(len(at), T3S, True)), ('raw_out', at),
            ('in', raw_write_reply(len(at), len(at)), 512),
        ])
        assert controller.write(22, under, timeout_s=3.0) == RAW_WRITE_MIN_BYTES - 1
        assert controller.write(22, at, timeout_s=3.0) == RAW_WRITE_MIN_BYTES
        transport.assert_done()

    def test_the_raw_transfer_and_the_reply_get_the_transfer_allowance(self):
        controller, transport = attached_ni([
            ('out', p.write_raw_message(3000, T3S, True)), ('raw_out', bytes(3000)),
            ('in', raw_write_reply(3000, 3000), 512),
        ])
        controller.write_raw(bytes(3000), timeout_s=3.0)
        assert transport.timeouts[-2:] == [('raw_out', 3000, raw_wait_ms(3000)), ('in', 512, raw_wait_ms(3000))]

    def test_the_framed_write_ignores_the_termination_character(self):
        # 0x0d keeps byte 5 at 0x00, the form the bench proved; NI's 0x0a there is untested.
        controller, transport = attached_ni([
            ('out', h('0d fa ff fc 00 00 08 00 2a 49 44 4e 3f 0a 00 00 04 00 00 00')), ('in', status_reply(0x0D)),
        ])
        controller.write_raw(b'*IDN?\n', timeout_s=3.0, eos_char=0x0A)
        transport.assert_done()

    def test_chunks_of_0xffff_with_eoi_on_the_last_and_a_short_tail_framed(self):
        # Per chunk, like the read loop: the 1-byte tail is below the threshold, so 0x0d.
        data = bytes(0xFFFF) + b'Z'
        controller, transport = attached_ni(address_listener() + [
            ('out', p.write_raw_message(0xFFFF, T3S, False)), ('raw_out', bytes(0xFFFF)),
            ('in', raw_write_reply(0xFFFF, 0xFFFF), 512),
            ('out', p.write_message(b'Z', T3S, True)), ('in', status_reply(0x0D)),
        ])
        assert controller.write(22, data, timeout_s=3.0) == 0x10000
        transport.assert_done()

    def test_a_long_tail_after_a_full_chunk_goes_raw_too(self):
        data = bytes(0xFFFF + 3000)
        controller, transport = attached_ni([
            ('out', p.write_raw_message(0xFFFF, T3S, False)), ('raw_out', bytes(0xFFFF)),
            ('in', raw_write_reply(0xFFFF, 0xFFFF), 512),
            ('out', p.write_raw_message(3000, T3S, True)), ('raw_out', bytes(3000)),
            ('in', raw_write_reply(3000, 3000), 512),
        ])
        assert controller.write_raw(data, timeout_s=3.0) == 0xFFFF + 3000
        transport.assert_done()

    def test_no_listener_reported_in_the_reply(self):
        controller, transport = attached_ni([
            ('out', p.write_raw_message(2100, T3S, True)), ('raw_out', bytes(2100)),
            ('in', raw_write_reply(2100, 0, error=8), 512),
        ])
        with pytest.raises(NoListener) as info:
            controller.write_raw(bytes(2100), timeout_s=3.0)
        assert info.value.code == 8
        transport.assert_done()

    #: raw_errors.pcap 5.6041-5.6069: 2502 bytes to address 5, where nothing listens.
    NOBODY = b'*CLS;' * 500 + b'\r\n'
    STALL = TransportStall('raw bulk write was refused with a STALL')

    def refused_write(self, tail: List[Tuple[Any, ...]]) -> List[Tuple[Any, ...]]:
        """NI's instruction and reply blocks for the refused 0x0e, in our bare message."""
        return address_listener(pad=5) + [
            ('out', h('0e 00 00 fc 00 0a 08 00 3a f6 ff ff 04 00 00 00')),
            ('raw_out', self.NOBODY, self.STALL),
            ('in', h('0e 00 28 08 3a f6 ff ff 04 00 00 00'), 512),
        ] + tail

    def test_refused_data_reads_the_reply_resets_both_out_pipes_and_carries_on(self):
        assert len(self.NOBODY) == 2502
        controller, transport = attached_ni(self.refused_write([
            ('clear_halt', 0x06), ('clear_halt', 0x02),
            # The next operation is ordinary: no stop request, no drain, no re-attach (§10.6.7).
        ]) + address_listener(pad=24) + [
            ('out', p.write_message(b'*IDN?\n', T3S, True)), ('in', status_reply(0x0D)),
        ])
        with pytest.raises(NoListener) as info:
            controller.write(5, self.NOBODY, timeout_s=3.0, eos_char=0x0A)
        assert info.value.code == 8
        assert controller.write(24, b'*IDN?\n', timeout_s=3.0) == 6
        transport.assert_done()

    def test_the_reply_to_refused_data_is_already_due(self):
        controller, transport = attached_ni(self.refused_write([('clear_halt', 0x06), ('clear_halt', 0x02)]))
        with pytest.raises(NoListener):
            controller.write(5, self.NOBODY, timeout_s=3.0, eos_char=0x0A)
        assert ('in', 512, SHORT_MS) in transport.timeouts[-1:]

    def test_refused_data_ended_by_our_own_stop_request_reattaches_next(self):
        # The refusal came but the reply did not, so the host stopped the instruction. What the
        # alternate OUT still holds is as unknown as after a stranded write: it would lead the
        # data of the next 0x0e.
        controller, transport = attached_ni(address_listener(pad=5) + [
            ('out', p.write_raw_message(2502, T3S, True, 0x0A)), ('raw_out', self.NOBODY, self.STALL),
            ('in', TransportTimeout('no reply yet'), 512), STOP,
            ('in', raw_write_reply(2502, 0, error=1), 512),
            ('clear_halt', 0x06), ('clear_halt', 0x02),
        ] + reattach_after_usb_fault_script(raw=True) + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(GpibTimeout):
            controller.write(5, self.NOBODY, timeout_s=3.0, eos_char=0x0A)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_refused_data_with_an_error_other_than_no_listener_reattaches_next(self):
        controller, transport = attached_ni(address_listener(pad=5) + [
            ('out', p.write_raw_message(2502, T3S, True, 0x0A)), ('raw_out', self.NOBODY, self.STALL),
            ('in', raw_write_reply(2502, 0, error=7), 512),
            ('clear_halt', 0x06), ('clear_halt', 0x02),
        ] + reattach_after_usb_fault_script(raw=True) + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(GpibError) as info:
            controller.write(5, self.NOBODY, timeout_s=3.0, eos_char=0x0A)
        assert info.value.code == 7
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_scripted_expectation_a_long_raw_write_follows_a_refused_one_without_a_reattach(self):
        # Not a hardware result. After the refusal NI's next operations were a raw read, a
        # serial poll and a framed query (§10.6.7): no capture has a second 0x0e after a refused
        # one, and the raw paths have not run on an adapter of ours. The script says what the
        # adapter is expected to do after the two pipe resets; what the test pins is what the
        # driver sends, an ordinary 0x0e with no stop request and no re-attach in between.
        controller, transport = attached_ni(self.refused_write([
            ('clear_halt', 0x06), ('clear_halt', 0x02),
        ]) + address_listener(pad=24) + [
            ('out', p.write_raw_message(2502, T3S, True, 0x0A)), ('raw_out', self.NOBODY),
            ('in', raw_write_reply(2502, 2502), 512),
        ])
        with pytest.raises(NoListener):
            controller.write(5, self.NOBODY, timeout_s=3.0, eos_char=0x0A)
        assert controller.write(24, self.NOBODY, timeout_s=3.0, eos_char=0x0A) == 2502
        transport.assert_done()

    def test_a_pipe_reset_that_fails_still_reports_no_listener_and_reattaches_next(self):
        controller, transport = attached_ni(self.refused_write([
            ('clear_halt', 0x06, TransportError('device gone')), ('clear_halt', 0x02),
        ]) + reattach_after_usb_fault_script(raw=True) + address_listener(pad=24) + [
            ('out', p.write_message(b'A', T3S, True)), ('in', status_reply(0x0D)),
        ])
        with pytest.raises(NoListener):
            controller.write(5, self.NOBODY, timeout_s=3.0, eos_char=0x0A)
        controller.write(24, b'A', timeout_s=3.0)
        transport.assert_done()

    def test_a_failure_that_is_not_recognised_as_a_stall_takes_the_same_path(self):
        # How libusb on macOS reports the adapter's STALL has not been seen. Whatever it is,
        # the reply is read and the pipes are reset, or the reply stays queued and 0x06 halted.
        odd = TransportError('raw bulk write failed: [Errno 5] Input/Output Error')
        controller, transport = attached_ni(address_listener(pad=5) + [
            ('out', p.write_raw_message(2502, T3S, True, 0x0A)), ('raw_out', self.NOBODY, odd),
            ('in', h('0e 00 28 08 3a f6 ff ff 04 00 00 00'), 512),
            ('clear_halt', 0x06), ('clear_halt', 0x02),
        ] + address_listener(pad=24) + [
            ('out', p.write_message(b'*IDN?\n', T3S, True)), ('in', status_reply(0x0D)),
        ])
        with pytest.raises(NoListener) as info:
            controller.write(5, self.NOBODY, timeout_s=3.0, eos_char=0x0A)
        assert info.value.code == 8
        assert ('in', 512, SHORT_MS) in transport.timeouts[-1:]
        assert controller.write(24, b'*IDN?\n', timeout_s=3.0) == 6   # no stop request, no re-attach
        transport.assert_done()

    def test_the_refusal_is_logged_with_its_class_errno_and_backend_code(self, caplog):
        stall = TransportStall('raw bulk write was refused with a STALL')
        stall.errno, stall.backend_code = 32, -9
        controller, transport = attached_ni(address_listener(pad=5) + [
            ('out', p.write_raw_message(2502, T3S, True, 0x0A)), ('raw_out', self.NOBODY, stall),
            ('in', h('0e 00 28 08 3a f6 ff ff 04 00 00 00'), 512),
            ('clear_halt', 0x06), ('clear_halt', 0x02),
        ])
        with caplog.at_level('WARNING', logger='resistamet_gui.gpib_usb.controller'):
            with pytest.raises(NoListener):
                controller.write(5, self.NOBODY, timeout_s=3.0, eos_char=0x0A)
        line = next(r.getMessage() for r in caplog.records if 'refused the data of a 0x0e' in r.getMessage())
        assert 'TransportStall' in line and 'errno 32' in line and 'backend code -9' in line

    def test_the_pipes_are_reset_and_the_refusal_raised_when_the_reply_never_comes(self):
        controller, transport = attached_ni(address_listener(pad=5) + [
            ('out', p.write_raw_message(2502, T3S, True, 0x0A)),
            ('raw_out', self.NOBODY, self.STALL),
            ('in', TransportTimeout('no reply'), 512), STOP, ('in', TransportTimeout('still none'), 512),
            STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH), RAW_DRAIN,   # no reply is a fault (§8.2)
            ('clear_halt', 0x06), ('clear_halt', 0x02),
        ] + reattach_script() + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(TransportStall):
            controller.write(5, self.NOBODY, timeout_s=3.0, eos_char=0x0A)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_refused_data_with_a_reply_that_reports_success_raises_the_refusal_and_reattaches(self):
        controller, transport = attached_ni(address_listener(pad=5) + [
            ('out', p.write_raw_message(2502, T3S, True, 0x0A)),
            ('raw_out', self.NOBODY, self.STALL),
            ('in', raw_write_reply(2502, 2502), 512),
            ('clear_halt', 0x06), ('clear_halt', 0x02),
        ] + reattach_after_usb_fault_script(raw=True) + [
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        with pytest.raises(TransportStall):
            controller.write(5, self.NOBODY, timeout_s=3.0, eos_char=0x0A)
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_a_reply_read_that_fails_too_still_resets_the_pipes_and_raises_the_first_error(self):
        gone = TransportError('raw bulk write failed: [Errno 19] No such device')
        controller, transport = attached_ni(address_listener(pad=5) + [
            ('out', p.write_raw_message(2502, T3S, True, 0x0A)), ('raw_out', self.NOBODY, gone),
            ('in', TransportError('bulk read failed: [Errno 19] No such device'), 512),
            ('clear_halt', 0x06, TransportError('no device')), ('clear_halt', 0x02, TransportError('no device')),
        ])
        with pytest.raises(TransportError) as info:
            controller.write(5, self.NOBODY, timeout_s=3.0, eos_char=0x0A)
        assert info.value is gone
        transport.assert_done()

    def test_a_transport_without_clear_halt_cannot_mask_the_error_being_reported(self):
        class NoClearHalt(ScriptedTransport):
            clear_halt = None  # type: ignore[assignment]

        transport = NoClearHalt(attach_script() + address_listener(pad=5) + [
            ('out', p.write_raw_message(2502, T3S, True, 0x0A)), ('raw_out', self.NOBODY, self.STALL),
            ('in', h('0e 00 28 08 3a f6 ff ff 04 00 00 00'), 512),
        ] + DRAIN + [RAW_DRAIN] + attach_script() + [             # the pipes cannot be trusted: re-attach
            ('out', p.command_message(b'\x14', T3S)), ('in', status_reply(0x0C)),
        ])
        controller = Controller(transport, t.PID_HS, ni_instructions=True, sleep=lambda s: None)
        controller.attach()
        with pytest.raises(NoListener) as info:
            controller.write(5, self.NOBODY, timeout_s=3.0, eos_char=0x0A)
        assert info.value.code == 8
        assert controller.command(b'\x14', timeout_s=3.0) == 1
        transport.assert_done()

    def test_partial_count_is_returned(self):
        controller, _ = attached_ni([
            ('out', p.write_raw_message(2100, T3S, True)), ('raw_out', bytes(2100)),
            ('in', raw_write_reply(2100, 1500), 512),
        ])
        assert controller.write_raw(bytes(2100), timeout_s=3.0) == 1500

    def test_instrument_not_accepting_times_out_the_transfer_and_stops_the_device(self):
        controller, transport = attached_ni([
            ('out', p.write_raw_message(2100, T3S, True)),
            ('raw_out', bytes(2100), TransportTimeout('instrument holds NRFD')),
            STOP,
            ('in', raw_write_reply(2100, 512, error=1), 512),
            ('clear_halt', 0x06), ('clear_halt', 0x02),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.write_raw(bytes(2100), timeout_s=3.0)
        assert info.value.code == 1
        transport.assert_done()
        assert transport.timeouts[-1] == ('in', 512, int(RECOVERY_WAIT_S * 1000))

    def test_transfer_that_stops_part_way_stops_the_device_and_reports_the_count_it_took(self):
        # pyusb returns the partial count, not a timeout, once some bytes moved; that is the
        # same situation as no byte accepted within the wait and takes the same path (§5.11).
        controller, transport = attached_ni([
            ('out', p.write_raw_message(2100, T3S, True)),
            ('raw_out', bytes(2100), 1024),                       # 1024 of 2100 accepted, then the wait expired
            STOP,
            ('in', raw_write_reply(2100, 900, error=1), 512),    # the device says 900 reached the bus
            ('clear_halt', 0x06), ('clear_halt', 0x02),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.write_raw(bytes(2100), timeout_s=3.0)
        assert info.value.code == 1
        transport.assert_done()
        assert transport.timeouts[-1] == ('in', 512, int(RECOVERY_WAIT_S * 1000))

    def test_a_raw_write_the_host_stopped_resets_the_out_pipes_and_reattaches_before_the_next_operation(self):
        # 124 of the 1024 accepted bytes never reached the bus and may sit in the alternate
        # OUT FIFO, where they would lead the data of the next 0x0e. No capture shows the case;
        # the driver treats the state as unknown. Scripted expectation, not a hardware result.
        controller, transport = attached_ni([
            ('out', p.write_raw_message(2100, T3S, True)),
            ('raw_out', bytes(2100), 1024),
            STOP,
            ('in', raw_write_reply(2100, 900, error=1), 512),
            ('clear_halt', 0x06), ('clear_halt', 0x02),
        ] + reattach_after_usb_fault_script(raw=True) + [
            ('out', p.write_raw_message(2100, T3S, True)), ('raw_out', bytes(2100)),
            ('in', raw_write_reply(2100, 2100), 512),
        ])
        with pytest.raises(GpibTimeout):
            controller.write_raw(bytes(2100), timeout_s=3.0)
        assert controller.write_raw(bytes(2100), timeout_s=3.0) == 2100
        transport.assert_done()

    def test_a_stopped_raw_write_with_no_reply_still_resets_the_out_pipes(self):
        controller, transport = attached_ni([
            ('out', p.write_raw_message(2100, T3S, True)),
            ('raw_out', bytes(2100), TransportTimeout('instrument holds NRFD')),
            STOP,
            ('in', TransportTimeout('no reply'), 512),
            ('clear_halt', 0x06), ('clear_halt', 0x02),
            STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH), RAW_DRAIN,   # no reply is a fault (§8.2)
        ])
        with pytest.raises(ProtocolError):
            controller.write_raw(bytes(2100), timeout_s=3.0)
        transport.assert_done()

    def test_missing_reply_takes_the_stop_path(self):
        controller, transport = attached_ni([
            ('out', p.write_raw_message(2100, T3S, True)), ('raw_out', bytes(2100)),
            ('in', TransportTimeout('no reply'), 512),
            STOP,
            ('in', raw_write_reply(2100, 2100, error=1), 512),
        ])
        with pytest.raises(GpibTimeout):
            controller.write_raw(bytes(2100), timeout_s=3.0)
        transport.assert_done()

    def test_wrong_reply_block_is_a_fault(self):
        controller, transport = attached_ni([
            ('out', p.write_raw_message(2100, T3S, True)), ('raw_out', bytes(2100)),
            ('in', status_reply(0x0D), 512),
            STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH), RAW_DRAIN,
        ])
        with pytest.raises(ProtocolError):
            controller.write_raw(bytes(2100), timeout_s=3.0)
        transport.assert_done()

    def test_a_model_without_the_alternate_pair_stays_framed(self):
        data = bytes(5000)
        script = [
            ('out', p.register_read_message(t.USB_B_SERIAL_REGISTERS)),
            ('in', regread_reply([0x78, 0x56, 0x34, 0x12]), 32),
        ] + attach_script()[2:] + [
            ('out', p.write_message(data, T3S, True)), ('in', status_reply(0x0D)),
        ]
        transport = ScriptedTransport(script)
        controller = Controller(transport, t.PID_USB_B, ni_instructions=True, sleep=lambda s: None)
        controller.attach()
        assert controller.write_raw(data, timeout_s=3.0) == 5000
        transport.assert_done()


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


IDN_2420 = b'KEITHLEY INSTRUMENTS INC.,MODEL 2420,1230523,C30   Mar 17 2006 09:29:29/A02  /H/L\n'


class TestRawRead:
    """Reads of RAW_READ_MIN_BYTES and more: 0x0b, data on the alternate bulk IN (§10.1)."""

    def test_idn_over_0x0b_with_ni_bytes(self):
        # counts.pcap 13.4323 / 13.4419 / 13.4424: the 0x0b of 4096 with the 3 s code. Our
        # message is the 0x0b block and the clear-END write; the reply is those two blocks.
        controller, transport = attached_ni(address_talker(pad=24) + [
            ('out', h('0b 00 00 fc 00 f0 ff ff 09 01 00 01 0a 55 00 00 04 00 00 00')),  # NI's e is 0a, ours 00
            ('raw_in', IDN_2420, 4608),
            ('in', h('0b 20 64 00 52 f0 ff ff e0 00 00 00 09 00 64 00 52 f0 ff ff 01 00 00 00 04 00 00 00'), 512),
        ])
        assert controller.read(24, max_bytes=4096, timeout_s=3.0) == (IDN_2420, True)
        transport.assert_done()

    def test_the_threshold_is_ni_s_1024_1025(self):
        # §10.1.1: counts.pcap 12.8188 is the last 0x0a (1024), read_thresholds.pcap 0.3160 the
        # first 0x0b (1025); the instruction blocks below are NI's bytes but for e, which NI
        # fills with the termination character and this driver leaves 0x00 with the compare off.
        assert RAW_READ_MIN_BYTES == 1025
        controller, transport = attached_ni(address_talker() + [
            ('out', h('0a 00 00 fc 00 fc 00 00') + p.read_message(1024, T3S)[8:]),
            ('in', read_reply(b'x', 1024), p.read_reply_buffer_size(1024, 512)),
        ] + address_talker() + [
            ('out', h('0b 00 00 fc ff fb ff ff') + p.read_raw_message(1025, T3S)[8:]),
            ('raw_in', b'x', p.raw_read_buffer_size(1025, 512)),
            ('in', raw_read_reply(1025, 1), 512),
        ])
        assert controller.read(22, max_bytes=1024, timeout_s=3.0) == (b'x', True)
        assert controller.read(22, max_bytes=1025, timeout_s=3.0) == (b'x', True)
        transport.assert_done()

    def test_data_is_read_before_the_reply_and_the_reply_wait_is_short(self):
        controller, transport = attached_ni(address_talker() + [
            ('out', p.read_raw_message(20480, T3S)),
            ('raw_in', IDN_2420, 20992),
            ('in', raw_read_reply(20480, 82), 512),
        ])
        controller.read(22, max_bytes=20480, timeout_s=3.0)
        kinds = [kind for kind, _, _ in transport.timeouts][-3:]
        assert kinds == ['out', 'raw_in', 'in']
        assert transport.timeouts[-2] == ('raw_in', 20992, int(RAW_READ_SLICE_S * 1000))  # the first slice
        assert transport.timeouts[-1] == ('in', 512, SHORT_MS)

    def test_full_chunk_has_end_clear(self):
        data = bytes(range(256)) * 80
        controller, transport = attached_ni(address_talker() + [
            ('out', p.read_raw_message(20480, T3S)),
            ('raw_in', data, 20992),
            ('in', h('0b 00 64 00 00 00 00 00 60 00 00 00 09 00 64 00 00 00 00 00 01 00 00 00 04 00 00 00'), 512),
        ])
        assert controller.read(22, max_bytes=20480, timeout_s=3.0) == (data, False)
        transport.assert_done()

    def test_request_above_one_instruction_loops_without_readdressing(self):
        first, second = bytes(0xFFFF), b'tail\n'
        controller, transport = attached_ni(address_talker() + [
            ('out', p.read_raw_message(0xFFFF, T3S)), ('raw_in', first, 66048),
            ('in', raw_read_reply(0xFFFF, 0xFFFF, end=False), 512),
            ('out', p.read_raw_message(70000 - 0xFFFF, T3S)), ('raw_in', second, 4608),
            ('in', raw_read_reply(70000 - 0xFFFF, len(second)), 512),
        ])
        assert controller.read(22, max_bytes=70000, timeout_s=3.0) == (first + second, True)
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
        controller, transport = attached_ni(address_talker(pad=5) + [
            ('out', p.read_raw_message(20480, T3S)),
            ('raw_in', b'', 20992),
            ('in', h('0b 00 64 0a 00 b0 ff ff 60 00 00 00 09 00 64 00 00 b0 ff ff 01 00 00 00 04 00 00 00'), 512),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.read(5, max_bytes=20480, timeout_s=3.0)
        assert info.value.partial == b'' and info.value.code == 0x0A
        transport.assert_done()

    def test_a_device_timeout_needs_no_stop_request_and_no_reattach(self):
        # raw_errors.pcap 6.1075-10.3034: the adapter ends the 0x88 transfer itself with a
        # zero-length packet at its timeout, the reply follows, and the next operation is
        # ordinary (§10.6.6, §10.6.7). NI's reply blocks, in our two-block message.
        controller, transport = attached_ni(address_talker(pad=5) + [
            ('out', h('0b 00 00 fc 00 b0 ff ff 09 01 00 01 0a 55 00 00 04 00 00 00')),
            ('raw_in', b'', 20992),
            ('in', h('0b 00 64 0a 00 b0 ff ff 60 00 00 00 09 00 64 00 00 b0 ff ff 01 00 00 00 04 00 00 00'), 512),
        ] + address_listener(pad=24) + [
            ('out', p.write_message(b'*IDN?\n', T3S, True)), ('in', status_reply(0x0D)),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.read(5, max_bytes=20480, timeout_s=3.0)
        assert info.value.code == 0x0A and info.value.partial == b''
        assert controller.write(24, b'*IDN?\n', timeout_s=3.0) == 6
        transport.assert_done()  # no ('ctrl', 0x20 ...) step anywhere in the script

    def test_partial_data_on_a_device_timeout(self):
        controller, transport = attached_ni(address_talker() + [
            ('out', p.read_raw_message(4096, T3S)),
            ('raw_in', b'PART', 4608),
            ('in', raw_read_reply(4096, 4, end=False, error=0x0A), 512),
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
        controller, transport = attached_ni(address_talker(pad=24) + [
            ('out', p.read_raw_message(20480, T3S)),
            ('raw_in', h('31 31 30 33 0a 00'), 20992),
            ('in', h('0b 20 64 00 05 b0 ff ff e0 00 00 00 09 00 64 00 05 b0 ff ff 01 00 00 00 04 00 00 00'), 512),
        ])
        assert controller.read(24, max_bytes=20480, timeout_s=3.0) == (b'1103\n', True)

    def test_fewer_bytes_than_the_count_is_a_fault_that_resyncs(self):
        controller, transport = attached_ni(address_talker() + [
            ('out', p.read_raw_message(4096, T3S)),
            ('raw_in', b'SHORT', 4608),
            ('in', raw_read_reply(4096, 82), 512),
            STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH), RAW_DRAIN,
        ])
        with pytest.raises(ProtocolError):
            controller.read(22, max_bytes=4096, timeout_s=3.0)
        transport.assert_done()

    def test_host_wait_expiry_on_the_data_stops_the_device_and_reports_a_timeout(self):
        controller, transport = attached_ni(address_talker(code=t.TIMEOUT_DISABLED_CODE) + [
            ('out', p.read_raw_message(4096, t.TIMEOUT_DISABLED_CODE)),
        ] + raw_wait_slices(4106, 4608) + [
            STOP,
            ('raw_in', b'', 4608),
            ('in', raw_read_reply(4096, 0, end=False, error=1), 512),
        ], infinite_wait_s=0.01)
        with pytest.raises(GpibTimeout) as info:
            controller.read(22, max_bytes=4096, timeout_s=None)
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


# ---------------------------------------------------------------------------
# §7.2 host waits
# ---------------------------------------------------------------------------

class TestHostWait:
    def test_five_second_request_uses_the_ten_second_row(self):
        controller, transport = attached(address_talker(code=0xFD) + [
            ('out', p.read_message(8, 0xFD)), ('in', read_reply(b'x', 8), 512),
        ])
        controller.read(22, max_bytes=8, timeout_s=5.0)
        # 5 s goes out as 0xfd, which one adapter runs for 16.78 s and the other for 20.0 s
        # (§7.3); the 15 s of nominal + 50 % would stop them 1.78 s and 5 s early, and the
        # 18.78 s of the first unit alone stopped the second 1.2 s early on the bench.
        assert transport.in_timeouts_after(0x0C) == [WAIT_10S_MS]
        assert transport.in_timeouts_after(0x06) == [SHORT_MS]
        assert transport.in_timeouts_after(0x0A) == [WAIT_10S_MS + 8]  # and 1 ms per byte asked for

    def test_three_second_request(self):
        controller, transport = attached(address_listener() + [
            ('out', p.write_message(b'A', T3S, True)), ('in', status_reply(0x0D)),
        ])
        controller.write(22, b'A', timeout_s=3.0)
        assert transport.in_timeouts_after(0x0C) == [WAIT_3S_MS]
        assert transport.in_timeouts_after(0x0D) == [WAIT_3S_MS + 1]  # and 1 ms for the byte

    @pytest.mark.parametrize('timeout_s, code, base_ms', [(1.0, 0xFB, 3250), (3.0, T3S, WAIT_3S_MS)])
    def test_the_reply_to_a_framed_read_allows_for_the_bytes_it_carries(self, timeout_s, code, base_ms):
        # The code bounds a handshake, not the transfer (§10.1.8): a 2420 took 4.0 s over a
        # 20480-byte chunk and finished with error 0. pyvisa asks for 20480 bytes every time;
        # the framed path asks the adapter for 1024 of them per 0x0a, and the wait for each
        # piece allows for that piece's bytes at BUS_MIN_RATE_BPS on top of the expiry.
        controller, transport = attached([
            ('out', p.read_message(1024, code)),
            ('in', read_reply(b'x', 1024), p.read_reply_buffer_size(1024, 512)),
        ])
        controller.read_raw(20480, timeout_s=timeout_s)
        assert transport.in_timeouts_after(0x0A) == [base_ms + 1024]

    def test_the_reply_to_a_framed_write_allows_for_what_the_adapter_still_holds(self):
        # The OUT completes once the adapter has the message; up to its buffer (about 4 KB,
        # §8.17) has then still to reach the instrument before the reply can come.
        for length, allowance_ms in ((1, 1), (2048, 2048), (4096, 4096), (30000, 4096)):
            controller, transport = attached([
                ('out', p.write_message(bytes(length), T3S, True)), ('in', status_reply(0x0D)),
            ])
            controller.write_raw(bytes(length), timeout_s=3.0)
            assert transport.in_timeouts_after(0x0D) == [WAIT_3S_MS + allowance_ms], length

    def test_disabled_timeout_uses_the_application_wait(self):
        controller, transport = attached(address_listener(code=0xF0) + [
            ('out', p.write_message(b'A', 0xF0, True)), ('in', status_reply(0x0D)),
        ], infinite_wait_s=42.0)
        controller.write(22, b'A', timeout_s=None)
        assert transport.in_timeouts_after(0x0D) == [42001]

    def test_only_a_message_that_carries_write_data_waits_longer_on_the_out(self):
        controller, transport = attached(address_listener() + [
            ('out', p.write_message(b'A', T3S, True)), ('in', status_reply(0x0D)),
        ])
        controller.write(22, b'A', timeout_s=3.0)
        outs = [(opcode, tm) for kind, opcode, tm in transport.timeouts if kind == 'out']
        assert {tm for opcode, tm in outs if opcode != 0x0D} == {SHORT_MS}
        assert [tm for opcode, tm in outs if opcode == 0x0D] == [raw_wait_ms(1)]

    @pytest.mark.parametrize('timeout_s, base_ms', [(3.0, WAIT_3S_MS), (20.0, WAIT_30S_MS), (0.3, 2375)])
    def test_the_out_of_a_framed_write_follows_the_device_timeout_and_the_length(self, timeout_s, base_ms):
        # §7.2, §10.5.2: the tail of NI's 2080-byte 0x0d message took 103 ms on 0x02 with a
        # fast listener; the 1 s of the §7.2 table is too short for a slow one.
        data = bytes(2048)  # the longest framed write on a model with the alternate pair
        code, _ = p.effective_timeout(timeout_s)
        controller, transport = attached([
            ('out', p.write_message(data, code, True)), ('in', status_reply(0x0D)),
        ])
        controller.write_raw(data, timeout_s=timeout_s)
        assert transport.timeouts[-2:] == [('out', 0x0D, base_ms + 2048), ('in', 12, base_ms + 2048)]

    def test_a_framed_write_of_a_full_instruction_on_a_model_without_the_pair(self):
        data = bytes(0xFFFF)
        script = [
            ('out', p.register_read_message(t.USB_B_SERIAL_REGISTERS)),
            ('in', regread_reply([0x78, 0x56, 0x34, 0x12]), 32),
        ] + attach_script()[2:] + [('out', p.write_message(data, T3S, True)), ('in', status_reply(0x0D))]
        transport = ScriptedTransport(script)
        controller = Controller(transport, t.PID_USB_B, sleep=lambda s: None)
        controller.attach()
        controller.write_raw(data, timeout_s=3.0)
        assert transport.timeouts[-2] == ('out', 0x0D, raw_wait_ms(0xFFFF))  # 6.2 s + 65.5 s

    def test_the_raw_out_and_its_reply_follow_the_device_timeout_and_the_length(self):
        # §10.5.2: 2049 bytes took 368 ms to complete on 0x06.
        controller, transport = attached_ni([
            ('out', p.write_raw_message(2049, 0xFE, True)), ('raw_out', bytes(2049)),
            ('in', raw_write_reply(2049, 2049), 512),
        ])
        controller.write_raw(bytes(2049), timeout_s=20.0)
        assert transport.timeouts[-3:] == [('out', 0x0E, SHORT_MS), ('raw_out', 2049, WAIT_30S_MS + 2049),
                                           ('in', 512, WAIT_30S_MS + 2049)]

    def test_disabled_timeout_waits_the_application_wait_on_the_out_too(self):
        controller, transport = attached([
            ('out', p.write_message(bytes(1000), 0xF0, True)), ('in', status_reply(0x0D)),
        ], infinite_wait_s=42.0)
        controller.write_raw(bytes(1000), timeout_s=None)
        assert transport.timeouts[-2:] == [('out', 0x0D, 43000), ('in', 12, 43000)]

    #: §7.3 as the specification prints it, (code, measured expiry in seconds), every timed
    #: case on GPIB-USB-HS 013CC9DF under NI's driver: written out here so the test does not
    #: read the figures it checks from the code.
    MEASURED = [(0xF9, 0.132272), (0xFA, 0.263541), (0xFB, 1.049837),
                (0xFC, 4.195609), (0xFC, 4.195640), (0xFC, 4.195316), (0xFC, 4.196156),
                (0xFC, 4.195943), (0xFC, 4.195767),
                (0xFD, 16.778423), (0xFE, 33.555345), (0xFE, 33.555258)]
    #: §7.3, "A second unit expires at other times": GPIB-USB-HS 01CEE482 under this driver's
    #: messages (bench 2026-09-21), (code, expiry in seconds): 1.25 times 0.1, 0.3, 1, 3, 16
    #: and 33 s.
    BENCH = [(0xF9, 0.127), (0xFA, 0.375), (0xFB, 1.250), (0xFC, 3.750), (0xFD, 20.000), (0xFE, 41.250)]
    #: §7.3's inference column, the larger candidate: (code, power of two in microseconds).
    INFERRED = [(0xF1, 4), (0xF2, 5), (0xF3, 7), (0xF4, 9), (0xF5, 10), (0xF6, 12), (0xF7, 14),
                (0xF8, 15), (0xFF, 27), (0x01, 29), (0x02, 30)]
    #: The same column for the six timed codes, to back-test the rule for the untimed ones.
    INFERRED_FOR_TIMED = [(0xF9, 17), (0xFA, 19), (0xFB, 20), (0xFC, 22), (0xFD, 24), (0xFE, 25)]

    @pytest.mark.parametrize('code, measured_s', MEASURED + BENCH)
    def test_the_host_outlasts_every_measured_expiry_of_either_unit(self, code, measured_s):
        # §7.2: the reply trailed the power of two by at most 1.9 ms; the wait must clear the
        # measured expiry by more than that, and the recommendation is 2 s. §7.3: until the
        # cause of the two units' difference is established the wait outlasts both tables.
        wait = p.host_wait_s(code, 600.0)
        assert wait > measured_s + 1.9e-3
        assert wait >= measured_s + 2.0
        assert t.TIMEOUT_EXPIRY_JITTER_S == 1.9e-3

    def test_the_waits_the_bench_needed(self):
        # On the bench under 0xfd the host, waiting 18.78 s, sent the stop request 1.2 s before
        # the adapter's own error 0x0a reply at 20.0 s, and a timeout was reported as an I/O
        # error (§7.3). The second unit's figure plus 2 s, or the first's where it is longer.
        assert p.host_wait_s(0xFD, 600.0) >= 22.0
        assert p.host_wait_s(0xFE, 600.0) >= 43.25
        assert p.host_wait_s(0xFB, 600.0) >= 3.25
        assert p.host_wait_s(0xFC, 600.0) >= 6.196
        assert p.host_wait_s(0xFA, 600.0) >= 2.375
        assert p.host_wait_s(0xF9, 600.0) >= 2.132

    def test_no_code_waits_less_than_it_did_from_the_first_unit_alone(self):
        # The wait before the second unit was timed: the first unit's figure, or the bare
        # power of two for a code nobody timed, plus 2 s. No code's wait may have got shorter.
        first_unit = dict(self.MEASURED)
        first_unit.update((code, 2 ** exponent / 1e6) for code, exponent in self.INFERRED)
        for _, code in t.TIMEOUT_TABLE:
            assert p.host_wait_s(code, 600.0) >= first_unit[code] + 2.0 - 1e-9, hex(code)

    @pytest.mark.parametrize('code, exponent', INFERRED)
    def test_an_untimed_code_waits_1_25_times_the_larger_of_nominal_and_the_power_of_two(self, code, exponent):
        nominal = dict((c, limit) for limit, c in t.TIMEOUT_TABLE)[code]
        expiry = 1.25 * max(nominal, 2 ** exponent / 1e6)
        assert t.timeout_expiry_s(code) == pytest.approx(expiry, rel=1e-12)
        assert p.host_wait_s(code, 600.0) == pytest.approx(expiry + 2.0, rel=1e-12)

    def test_the_untimed_code_rule_undershoots_neither_unit_on_any_timed_code(self):
        # Why that rule (§7.3): the second unit's round figure is the nominal limit up to 0xfc
        # and the first unit's power of two for 0xfd and 0xfe. The bare power of two, the rule
        # before, gives 16.78 s for 0xfd against a measured 20.0; 1.25 x nominal gives 12.5.
        nominal = dict((c, limit) for limit, c in t.TIMEOUT_TABLE)
        longest = {}
        for code, seconds in self.MEASURED + self.BENCH:
            longest[code] = max(longest.get(code, 0.0), seconds)
        for code, exponent in self.INFERRED_FOR_TIMED:
            power_of_two = 2 ** exponent / 1e6
            assert 1.25 * max(nominal[code], power_of_two) >= longest[code], hex(code)
        assert 2 ** 24 / 1e6 < longest[0xFD] and 1.25 * nominal[0xFD] < longest[0xFD]

    def test_every_row_of_the_timeout_table_has_an_expiry_the_host_outlasts(self):
        nominal = dict((code, limit) for limit, code in t.TIMEOUT_TABLE)
        assert set(nominal) == set(t.TIMEOUT_EXPIRY_MEASURED_S) | set(t.TIMEOUT_EXPIRY_INFERRED_S)
        assert not set(t.TIMEOUT_EXPIRY_MEASURED_S) & set(t.TIMEOUT_EXPIRY_INFERRED_S)
        assert set(t.TIMEOUT_EXPIRY_BENCH_S) == set(t.TIMEOUT_EXPIRY_MEASURED_S)
        assert t.TIMEOUT_EXPIRY_BENCH_S == dict(self.BENCH)
        for code, limit in nominal.items():
            expiry = t.timeout_expiry_s(code)
            assert p.host_wait_s(code, 600.0) > expiry + 1.9e-3
            assert expiry >= t.TIMEOUT_EXPIRY_MEASURED_S.get(code, 0.0), hex(code)
            assert expiry >= t.TIMEOUT_EXPIRY_BENCH_S.get(code, 0.0), hex(code)
            # The larger of the two units' figures is never below nominal (§7.3): 0xfa is the
            # one code the first unit ends early, 0.264 s for 300 ms, and the second runs 0.375.
            assert expiry >= limit, hex(code)
        assert t.TIMEOUT_EXPIRY_MEASURED_S[0xFA] < nominal[0xFA] < t.TIMEOUT_EXPIRY_BENCH_S[0xFA]

    def test_the_ten_second_code_is_waited_past_both_units_expiries(self):
        # The code the application's 5 s and 10 s timeouts go out as. Nominal + 50 % is 15 s;
        # the first unit runs it 16.78 s and the second 20.0 s.
        for asked in (5.0, 10.0):
            assert p.timeout_code(asked) == 0xFD
        assert p.host_wait_s(0xFD, 600.0) >= 22.0

    def test_the_disabled_code_has_no_expiry_and_an_unknown_code_is_refused(self):
        assert t.timeout_expiry_s(0xF0) is None
        assert p.host_wait_s(0xF0, 42.0) == 42.0
        with pytest.raises(ValueError):
            t.timeout_expiry_s(0x03)

    def test_the_raw_in_wait_is_never_shorter_than_the_reply_wait_of_a_framed_read(self):
        # §10.9: give the 0x88 read the same host wait as the reply rather than cancelling it
        # early; ours adds the transfer allowance on top, and spends it in slices.
        total_ms = WAIT_3S_MS + 20480
        controller, transport = attached_ni([
            ('out', p.read_raw_message(20480, T3S)),
        ] + raw_wait_slices(total_ms, 20992) + [
            STOP, ('raw_in', b'', 20992),
            ('in', raw_read_reply(20480, 0, end=False, error=1), 512),
        ])
        with pytest.raises(GpibTimeout):
            controller.read_raw(20480, timeout_s=3.0)
        transport.assert_done()
        slices = [tm for kind, _, tm in transport.timeouts if kind == 'raw_in'][:-1]
        assert sum(slices) == total_ms and total_ms > 4196 and max(slices) == int(RAW_READ_SLICE_S * 1000)


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
# §10.4 service request
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# §8.2 faults: resynchronise, then re-attach on the next operation
# ---------------------------------------------------------------------------

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
            # ren_device.pcap 1.7255: NI's go to local leads with the talk address.
            ('out', p.command_message(bytes((0x40, 0x3F, 0x38, 0x01)), 0xFA)), ('in', status_reply(0x0C)),
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

    def test_serial_poll_with_ni_instructions_on_is_one_0x10(self):
        controller, transport = attached_ni([
            ('out', p.serial_poll_message(22, T3S)),
            ('in', h('3a 16 00 40 39 00 74 00 00 00 ff ff 04 00 00 00'), 512),
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


class TestSerialPoll:
    """The 0x10 instruction (§10.5.4), not the §5.9 command sequence."""

    def test_poll_with_ni_bytes(self):
        # stb.pcap 0.5134 / 0.5151 (code 0xfe, status byte 0), minus the blocks NI batches around it.
        controller, transport = attached_ni([
            ('out', h('10 01 00 00 18 00 fe 00 04 00 00 00')),
            ('in', h('3a 18 00 00 39 00 74 00 00 00 ff ff 04 00 00 00'), 512),
        ])
        assert controller.serial_poll_instruction(24, timeout_s=20.0) == 0
        transport.assert_done()
        assert transport.timeouts[-1] == ('in', 512, WAIT_30S_MS)  # the 0xfe expiry + 2 s

    def test_status_byte_with_rqs(self):
        # srq_poll.pcap 2.5379 reported 0x20 after the adapter had polled RQS away; 0x60 is what
        # a device still requesting service would give.
        controller, _ = attached_ni([
            ('out', p.serial_poll_message(22, T3S)),
            ('in', h('3a 16 00 60 39 00 74 00 00 00 ff ff 04 00 00 00'), 512),
        ])
        assert controller.serial_poll_instruction(22) == 0x60

    def test_secondary_address(self):
        # sad_poll.pcap 0.5152 / 0.5170: S = 0x60 | 1, echoed in the 0x3a block; status byte 4.
        controller, transport = attached_ni([
            ('out', h('10 01 00 00 18 61 fc 00 04 00 00 00')),
            ('in', h('3a 18 61 04 39 00 74 00 00 00 ff ff 04 00 00 00'), 512),
        ])
        assert controller.serial_poll_instruction(24, sad=1) == 4
        transport.assert_done()

    def test_device_timeout_replies_without_the_0x3a_block(self):
        # raw_errors.pcap 10.8046 / 15.0003: nothing at address 5; the 0x39 block alone, error 0x0a.
        controller, transport = attached_ni([
            ('out', h('10 01 00 00 05 00 fc 00 04 00 00 00')),
            ('in', h('39 00 74 0a 00 00 00 00 04 00 00 00'), 512),
            # and the next operation goes out with no stop request and no re-attach (§10.6.7)
            ('out', p.serial_poll_message(24, T3S)),
            ('in', h('3a 18 00 00 39 00 74 00 00 00 00 00 04 00 00 00'), 512),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.serial_poll_instruction(5)
        assert info.value.code == 0x0A
        assert controller.serial_poll_instruction(24) == 0
        transport.assert_done()

    def test_device_timeout_with_a_0x3a_block_raises_too(self):
        controller, _ = attached_ni([
            ('out', p.serial_poll_message(22, T3S)),
            ('in', h('3a 16 00 00 39 00 74 0a ff ff ff ff 04 00 00 00'), 512),
        ])
        with pytest.raises(GpibTimeout):
            controller.serial_poll_instruction(22)

    def test_success_without_a_status_byte_is_a_fault(self):
        controller, transport = attached_ni([
            ('out', p.serial_poll_message(22, T3S)),
            ('in', h('39 00 74 00 00 00 ff ff 04 00 00 00'), 512),
            STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH), RAW_DRAIN,
        ])
        with pytest.raises(ProtocolError):
            controller.serial_poll_instruction(22)
        transport.assert_done()

    def test_answer_for_another_address_is_a_fault(self):
        controller, transport = attached_ni([
            ('out', p.serial_poll_message(22, T3S)),
            ('in', h('3a 18 00 00 39 00 74 00 00 00 ff ff 04 00 00 00'), 512),
            STOP, ('in', TransportTimeout('drained'), DRAIN_LENGTH), RAW_DRAIN,
        ])
        with pytest.raises(ProtocolError):
            controller.serial_poll_instruction(22)
        transport.assert_done()

    def test_poll_forgets_who_was_addressed(self):
        controller, transport = attached_ni(address_listener() + [
            ('out', p.write_message(b'A', T3S, True)), ('in', status_reply(0x0D)),
            ('out', p.serial_poll_message(22, T3S)),
            ('in', h('3a 16 00 00 39 00 74 00 00 00 ff ff 04 00 00 00'), 512),
        ] + address_listener() + [
            ('out', p.write_message(b'B', T3S, True)), ('in', status_reply(0x0D)),
        ])
        controller.write(22, b'A', timeout_s=3.0, readdress=False)
        controller.serial_poll_instruction(22)
        controller.write(22, b'B', timeout_s=3.0, readdress=False)
        transport.assert_done()

    def test_host_wait_expiry_takes_the_stop_path(self):
        controller, transport = attached_ni([
            ('out', p.serial_poll_message(22, T3S)), ('in', TransportTimeout('host wait'), 512),
            STOP, ('in', h('3a 16 00 00 39 00 74 01 ff ff ff ff 04 00 00 00'), 512),
        ])
        with pytest.raises(GpibTimeout):
            controller.serial_poll_instruction(22)
        transport.assert_done()


def probe_steps(pad: int, ndac: bool) -> List[Tuple[Any, ...]]:
    return [
        # UNT first: a talker still addressed would source its bytes into the probed listener.
        ('out', p.command_message(bytes((0x5F, 0x3F, 0x20 + pad)), T3S)), ('in', status_reply(0x0C)),
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
            ('out', p.command_message(bytes((0x5F, 0x3F, 0x21)), T3S)), ('in', status_reply(0x0C, error=5, count=-3)),
        ])
        assert ops.find_listeners(controller, [1, 2, 3]) == []
        transport.assert_done()

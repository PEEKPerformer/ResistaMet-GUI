"""Controller: raw writes, 0x0e with the data on the alternate bulk OUT (§10.5.2)."""
from typing import Any, List, Tuple

import pytest

from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t
from resistamet_gui.gpib_usb.controller import RAW_WRITE_MIN_BYTES, RECOVERY_WAIT_S, Controller
from resistamet_gui.gpib_usb.protocol import GpibError, GpibTimeout, NoListener, ProtocolError
from resistamet_gui.gpib_usb.transport import TransportError, TransportStall, TransportTimeout
from tests.fakes.gpib_usb import (DRAIN, DRAIN_LENGTH, RAW_DRAIN, SHORT_MS, STOP, T3S, ScriptedTransport,
                                  NI_SESSION_CLOSE, address_listener, attach_script, attached_ni, h,
                                  ni_raw_write_reply, ni_session,
                                  ni_write, raw_wait_ms, raw_write_reply, reattach_after_usb_fault_script,
                                  reattach_script, regread_reply, status_reply)


class TestRawWrite:
    """Writes of RAW_WRITE_MIN_BYTES and more: 0x0e, data on the alternate bulk OUT (§10.5.2)."""

    LONG = b'*CLS;' * 409 + b'*CL\r\n'  # 2050 bytes, as longwrite.pcap

    def test_2050_bytes_with_ni_bytes(self):
        # longwrite.pcap 1.8914 / 1.8916 / 2.0354: code 0xfe, termination character 0x0a, EOI,
        # in NI's 36-byte message (§10.5.2), byte for byte, after its bank-2 configuration.
        assert len(self.LONG) == 2050
        controller, transport = attached_ni(ni_session(pad=24, code=0xFE) + [
            ('out', h('03 00 00 00 0c fd 00 fd 40 3f 38 00 0e 00 00 fe 00 0a 08 00 fe f7 ff ff'
                      '09 01 00 02 03 01 00 00 04 00 00 00')),
            ('raw_out', self.LONG),
            ('in', ni_raw_write_reply(2050, 2050), 512),
        ])
        assert controller.write(24, self.LONG, timeout_s=20.0, eos_char=0x0A) == 2050
        transport.assert_done()

    def test_the_threshold_is_ni_s_2048_2049(self):
        assert RAW_WRITE_MIN_BYTES == 2049  # §10.5.2: 2048 the last 0x0d, 2049 the first 0x0e
        under = bytes(RAW_WRITE_MIN_BYTES - 1)
        at = bytes(RAW_WRITE_MIN_BYTES)
        controller, transport = attached_ni(address_listener() + [
            ('out', p.write_message(under, T3S, True)), ('in', status_reply(0x0D)),
        ] + ni_session() + [
            ('out', ni_write(len(at))), ('raw_out', at),
            ('in', ni_raw_write_reply(len(at), len(at)), 512),
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
        controller, transport = attached_ni(ni_session() + [
            ('out', ni_write(0xFFFF, eoi=False)), ('raw_out', bytes(0xFFFF)),
            ('in', ni_raw_write_reply(0xFFFF, 0xFFFF), 512),
            ('out', p.write_message(b'Z', T3S, True)), ('in', status_reply(0x0D)),   # still addressed
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
        """NI's message and reply for the refused 0x0e, byte for byte (raw_errors.pcap 5.6041, 5.6057)."""
        return ni_session(pad=5) + [
            ('out', h('03 00 00 00 0c fd 00 fd 40 3f 25 00 0e 00 00 fc 00 0a 08 00 3a f6 ff ff'
                      '09 01 00 02 03 01 00 00 04 00 00 00')),
            ('raw_out', self.NOBODY, self.STALL),
            ('in', h('03 00 30 00 00 00 ff ff 0c 00 38 00 00 00 ff ff 0e 00 28 08 3a f6 ff ff'
                     '09 00 28 00 3a f6 ff ff 01 00 00 00 04 00 00 00'), 512),
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
        controller, transport = attached_ni(ni_session(pad=5) + [
            ('out', ni_write(2502, pad=5, e=0x0A)), ('raw_out', self.NOBODY, self.STALL),
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
        controller, transport = attached_ni(ni_session(pad=5) + [
            ('out', ni_write(2502, pad=5, e=0x0A)), ('raw_out', self.NOBODY, self.STALL),
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
        ]) + NI_SESSION_CLOSE + ni_session(pad=24) + [
            ('out', ni_write(2502, pad=24, e=0x0A)), ('raw_out', self.NOBODY),
            ('in', ni_raw_write_reply(2502, 2502), 512),
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
        controller, transport = attached_ni(ni_session(pad=5) + [
            ('out', ni_write(2502, pad=5, e=0x0A)), ('raw_out', self.NOBODY, odd),
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
        controller, transport = attached_ni(ni_session(pad=5) + [
            ('out', ni_write(2502, pad=5, e=0x0A)), ('raw_out', self.NOBODY, stall),
            ('in', h('0e 00 28 08 3a f6 ff ff 04 00 00 00'), 512),
            ('clear_halt', 0x06), ('clear_halt', 0x02),
        ])
        with caplog.at_level('WARNING', logger='resistamet_gui.gpib_usb.controller'):
            with pytest.raises(NoListener):
                controller.write(5, self.NOBODY, timeout_s=3.0, eos_char=0x0A)
        line = next(r.getMessage() for r in caplog.records if 'refused the data of a 0x0e' in r.getMessage())
        assert 'TransportStall' in line and 'errno 32' in line and 'backend code -9' in line

    def test_the_pipes_are_reset_and_the_refusal_raised_when_the_reply_never_comes(self):
        controller, transport = attached_ni(ni_session(pad=5) + [
            ('out', ni_write(2502, pad=5, e=0x0A)),
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
        controller, transport = attached_ni(ni_session(pad=5) + [
            ('out', ni_write(2502, pad=5, e=0x0A)),
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
        controller, transport = attached_ni(ni_session(pad=5) + [
            ('out', ni_write(2502, pad=5, e=0x0A)), ('raw_out', self.NOBODY, gone),
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

        transport = NoClearHalt(attach_script() + ni_session(pad=5) + [
            ('out', ni_write(2502, pad=5, e=0x0A)), ('raw_out', self.NOBODY, self.STALL),
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

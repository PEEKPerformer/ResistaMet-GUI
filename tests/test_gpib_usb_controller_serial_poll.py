"""Controller: the 0x10 serial poll instruction (§10.5.4)."""
import pytest

from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb.protocol import GpibTimeout, ProtocolError
from resistamet_gui.gpib_usb.transport import TransportTimeout
from tests.fakes.gpib_usb import (DRAIN_LENGTH, RAW_DRAIN, STOP, T3S, WAIT_30S_MS, address_listener,
                                  attached_ni, h, status_reply)


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

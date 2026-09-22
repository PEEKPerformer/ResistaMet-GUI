"""Controller: the ATN rule, command chunking, bus control and the device_ops sequences."""
import pytest

from resistamet_gui.gpib_usb import device_ops as ops
from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t
from resistamet_gui.gpib_usb.protocol import GpibTimeout, NoListener
from resistamet_gui.gpib_usb.transport import TransportTimeout
from tests.fakes.gpib_usb import (STATUS_8, STOP, T3S, address_talker, attached, attached_ni, h, read_reply,
                                  regread_reply, regwrite_reply, status_reply)


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

"""Controller: framed writes (0x0d, §5.1) and the addressing before them."""
import pytest

from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t
from resistamet_gui.gpib_usb.controller import Controller
from resistamet_gui.gpib_usb.protocol import GpibTimeout, NoListener
from resistamet_gui.gpib_usb.transport import TransportTimeout
from tests.fakes.gpib_usb import (STOP, T3S, WAIT_3S_MS, ScriptedTransport, address_listener, attach_script,
                                  attached, h, regread_reply, status_reply)


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

    def test_every_write_addresses_again(self):
        # No record of who was addressed lets a repeat be skipped: the adapter serial-polls a
        # device that asserts SRQ by itself, readdressing the bus behind it (§10.4.2).
        controller, transport = attached(address_listener() + [
            ('out', p.write_message(b'A', T3S, True)), ('in', status_reply(0x0D)),
        ] + address_listener() + [
            ('out', p.write_message(b'B', T3S, True)), ('in', status_reply(0x0D)),
        ])
        controller.write(22, b'A', timeout_s=3.0)
        controller.write(22, b'B', timeout_s=3.0)
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

    def test_a_split_write_is_bounded_by_its_timeout_as_a_whole(self):
        # Like a read (§7.1, §10.10.2): the deadline is 0xfd's longest expiry, 20.0 s; the
        # second chunk still carries 0xfd, and none starts once the deadline has passed.
        script = [
            ('out', p.register_read_message(t.USB_B_SERIAL_REGISTERS)),
            ('in', regread_reply([0x78, 0x56, 0x34, 0x12]), 32),
        ] + attach_script()[2:] + address_listener(code=0xFD) + [
            ('out', p.write_message(bytes(0xFFFF), 0xFD, send_eoi=False)), ('in', status_reply(0x0D), None, 17.0),
            ('out', p.write_message(bytes(0xFFFF), 0xFD, send_eoi=False)), ('in', status_reply(0x0D), None, 3.5),
        ]
        transport = ScriptedTransport(script)
        controller = Controller(transport, t.PID_USB_B, sleep=lambda s: None, clock=transport.clock)
        controller.attach()
        with pytest.raises(GpibTimeout) as info:
            controller.write(22, bytes(3 * 0xFFFF), timeout_s=5.0)
        assert 'timeout ran out after 131070 of 196605 bytes' in str(info.value)
        transport.assert_done()   # no third chunk

    def test_host_wait_expiry_on_a_write_takes_the_stop_path(self):
        controller, transport = attached(address_listener() + [
            ('out', p.write_message(b'A', T3S, True)), ('in', TransportTimeout('host wait')),
            STOP, ('in', status_reply(0x0D, error=1, count=-1), 12),
        ])
        with pytest.raises(GpibTimeout) as info:
            controller.write(22, b'A', timeout_s=3.0)
        assert info.value.code == 1
        transport.assert_done()

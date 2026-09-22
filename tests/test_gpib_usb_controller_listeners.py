"""device_ops: finding listeners with the NDAC presence probe."""
from typing import Any, List, Tuple

from resistamet_gui.gpib_usb import device_ops as ops
from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t
from tests.fakes.gpib_usb import T3S, attached, regread_reply, status_reply


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

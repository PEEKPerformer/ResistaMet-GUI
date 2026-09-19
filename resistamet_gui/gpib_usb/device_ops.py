"""Device-level GPIB sequences composed from ``Controller`` primitives.

Selected device clear (§5.8), trigger, go-to-local and local lockout
(§5.18, §6) and the presence probe (§5.16) are each a few public
``Controller`` calls in a fixed order. They live here, as functions, so
``controller`` stays about the exchange rules and this module stays about
IEEE-488.1 procedure. Each holds the controller's lock for the whole
sequence so nothing interleaves with it. The serial poll is not here: the
adapter has an instruction for it (§10.5.4), so it is a ``Controller``
primitive.
"""
from typing import Iterable, List, Optional

from . import tables as t
from .controller import DEFAULT_TIMEOUT_S, Controller
from .protocol import NoListener


def device_clear(controller: Controller, pad: int, sad: Optional[int] = None,
                 timeout_s: Optional[float] = DEFAULT_TIMEOUT_S) -> None:
    """Selected device clear (§5.8): UNL, LAD N, SDC."""
    controller.command(t.addressed_command(pad, t.CMD_SDC, sad), timeout_s)


def trigger(controller: Controller, pad: int, sad: Optional[int] = None,
            timeout_s: Optional[float] = DEFAULT_TIMEOUT_S) -> None:
    """Group execute trigger to one device (§5.18)."""
    controller.command(t.addressed_command(pad, t.CMD_GET, sad), timeout_s)


def go_to_local(controller: Controller, pad: int, sad: Optional[int] = None,
                timeout_s: Optional[float] = DEFAULT_TIMEOUT_S) -> None:
    controller.command(t.addressed_command(pad, t.CMD_GTL, sad), timeout_s)


def local_lockout(controller: Controller, pad: Optional[int] = None, sad: Optional[int] = None,
                  timeout_s: Optional[float] = DEFAULT_TIMEOUT_S) -> None:
    """LLO to one addressed device, or to whoever is listening when ``pad`` is None."""
    if pad is None:
        controller.command(bytes((t.CMD_LLO,)), timeout_s)
    else:
        controller.command(t.addressed_command(pad, t.CMD_LLO, sad), timeout_s)


def find_listeners(controller: Controller, addresses: Iterable[int],
                   timeout_s: Optional[float] = DEFAULT_TIMEOUT_S) -> List[int]:
    """Which of ``addresses`` has a listener (§5.16 recommended probe).

    Addresses each candidate to listen, drops ATN, and reads the bus lines:
    a listener holds NDAC asserted while it waits for data; an empty
    address leaves it released. No data byte is sent to the instrument.
    """
    # Bench-verified on the GPIB-USB-HS with a Keithley 2400 at PAD 3: BSR reads
    # 0x01 (REN only) for empty addresses and has NDAC set for the instrument.
    with controller.lock:
        found: List[int] = []
        for pad in addresses:
            if pad == controller.own_address:
                continue
            try:
                controller.command(bytes((t.CMD_UNL, t.listen_address(pad))), timeout_s)
            except NoListener as exc:
                if exc.code == t.ERR_NO_ACCEPTOR:
                    break  # nothing on the bus accepts command bytes: it is empty
                raise
            controller.go_to_standby()
            if controller.bus_lines() & t.BSR_NDAC:
                found.append(pad)
            controller.take_control()
            controller.command(bytes((t.CMD_UNL,)), timeout_s)
        return found

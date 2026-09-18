"""Device-level GPIB sequences composed from ``Controller`` primitives.

Selected device clear (§5.8), trigger, go-to-local and local lockout
(§5.18, §6), the serial poll (§5.9) and the presence probe (§5.16) are each
a few public ``Controller`` calls in a fixed order. They live here, as
functions, so ``controller`` stays about the exchange rules and this module
stays about IEEE-488.1 procedure. Each holds the controller's lock for the
whole sequence so nothing interleaves with it.
"""
import logging
from typing import Iterable, List, Optional

from . import tables as t
from .controller import DEFAULT_TIMEOUT_S, Controller
from .protocol import GpibError, NoListener, ProtocolError
from .transport import TransportError

logger = logging.getLogger(__name__)


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


def serial_poll(controller: Controller, pad: int, sad: Optional[int] = None,
                timeout_s: Optional[float] = DEFAULT_TIMEOUT_S) -> int:
    """§5.9: SPE sequence, standby, one-byte read, SPD UNT. Returns the status byte."""
    with controller.lock:
        controller.command(t.serial_poll_enable_command(controller.own_address, pad, sad),
                           timeout_s)
        data = b''
        failure: Optional[Exception] = None
        try:
            controller.go_to_standby()
            data, _ = controller.read_raw(1, timeout_s)
        except (GpibError, TransportError) as exc:
            failure = exc
        # Leave serial-poll mode even when the read failed, without letting a
        # failure here hide the one that matters.
        try:
            controller.command(t.SERIAL_POLL_DISABLE_COMMAND, timeout_s)
        except (GpibError, TransportError) as exc:
            if failure is None:
                raise
            logger.warning('serial poll disable failed after %s: %s', failure, exc)
        if failure is not None:
            raise failure
        if not data:
            raise ProtocolError('serial poll returned no status byte')
        return data[0]


def find_listeners(controller: Controller, addresses: Iterable[int],
                   timeout_s: Optional[float] = DEFAULT_TIMEOUT_S) -> List[int]:
    """Which of ``addresses`` has a listener (§5.16 recommended probe).

    Addresses each candidate to listen, drops ATN, and reads the bus lines:
    a listener holds NDAC asserted while it waits for data; an empty
    address leaves it released. No data byte is sent to the instrument.
    """
    # spec gap: this NDAC probe follows IEEE-488.1 but is not bench-verified
    # with this adapter; §5.16 marks it uncertain.
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

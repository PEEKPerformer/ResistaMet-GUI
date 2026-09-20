"""Which adapter is ``GPIB<n>``, and one shared attached Controller per board.

Each adapter is identified by its USB serial (or, failing that, its bus and
address) and keeps its board number for the life of the process; new
adapters take the lowest free number. Numbering starts after any boards
linux-gpib reports so the two never collide; without linux-gpib it starts
at 0. Enumeration happens on first use and on an explicit ``refresh``, not
on every open, and every ``AdapterInfo`` the registry drops is disposed so
no libusb handle outlives its usefulness (see ``transport``).

A board's Controller is opened and attached on the first ``acquire`` and
closed on the ``release`` that brings its session count to zero. Nothing
here knows about pyvisa; ``visa_session`` sits on top.

Locks: the registry's lock guards the board table and the session counts,
and is held only for bookkeeping. Opening and closing an adapter happen
under that board's own lock instead, because a close waits for whatever
operation is in flight on the controller, which can be minutes; the other
boards, and ``owns`` / ``board_names``, must not wait with it. The
registry's lock is re-entrant because pyvisa closes a forgotten resource
from ``__del__``, which the garbage collector may run on a thread that is
inside ``acquire`` at that moment. Order: registry, then board, then
controller; the registry's lock is never waited for while a board's is held.
"""
import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import device_ops as ops
from . import transport
from .controller import Controller
from .protocol import GpibError
from .transport import AdapterInfo, Transport, TransportError

logger = logging.getLogger(__name__)

#: Every primary address; the controller skips its own.
PROBE_ADDRESSES = tuple(range(31))
#: Set to 1 / true / yes / on to use the instructions NI's driver was captured
#: sending: 0x0b / 0x0e with the data raw on the alternate endpoints for large
#: transfers, and 0x10 for the serial poll. Unset, 0 or anything else keeps
#: what ran on the bench: every transfer framed (0x0a / 0x0d) and the serial
#: poll as the §5.9 command sequence. NI's instructions have not run on an
#: adapter of ours, and every read the application makes is large enough to
#: take 0x0b. Read once per board open, here and nowhere else.
NI_INSTRUCTIONS_ENV = 'RESISTAMET_GPIB_NI_INSTRUCTIONS'
#: The switch's first name, from when it covered the transfers only. Still
#: read, with the same spellings, when ``NI_INSTRUCTIONS_ENV`` is not set.
RAW_TRANSFERS_ENV = 'RESISTAMET_GPIB_RAW_TRANSFERS'


def ni_instructions_enabled() -> bool:
    value = os.environ.get(NI_INSTRUCTIONS_ENV)
    if value is None:
        value = os.environ.get(RAW_TRANSFERS_ENV, '0')
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def _instructions_label(controller: Controller) -> str:
    """Which instructions this board uses, for the attach log line."""
    if not controller.ni_instructions:
        return 'framed transfers, command-sequence serial poll'
    return '%s transfers, 0x10 serial poll' % ('raw' if controller.raw_transfers else 'framed')


def _linux_gpib_board_count() -> int:
    """How many boards linux-gpib knows, so ours are numbered after them."""
    try:
        import gpib  # type: ignore
    except ImportError:
        return 0
    count = 0
    for board in range(16):
        try:
            gpib.ask(board, 1)
            count = board + 1
        except gpib.GpibError:
            continue
    return count


def _identity(info: AdapterInfo) -> Tuple[Any, ...]:
    """What makes an adapter the same one across enumerations."""
    if info.serial:
        return ('serial', info.serial)
    return ('location', info.bus, info.address)


class _Board:
    def __init__(self, info: AdapterInfo) -> None:
        self.info = info
        self.controller: Optional[Controller] = None
        self.sessions = 0
        #: Held while this board's adapter is being opened or closed.
        self.lock = threading.Lock()

    @property
    def busy(self) -> bool:
        """Sessions hold it, or its adapter is still being closed: its handle must stay."""
        return bool(self.sessions) or self.lock.locked()


class BoardRegistry:
    """Numbers adapters ``GPIB<n>`` and shares one attached Controller per board."""

    def __init__(self, open_transport: Callable[[AdapterInfo], Transport] = transport.open_transport,
                 first_board: Optional[int] = None) -> None:
        self._open_transport = open_transport
        self._first_board = first_board
        self._boards: Dict[str, _Board] = {}
        self._lock = threading.RLock()
        self._enumerated = False

    def refresh(self) -> None:
        """Re-enumerate. Known adapters keep their numbers; boards in use keep their handles."""
        with self._lock:
            self._refresh_locked()

    def _refresh_locked(self) -> None:
        if self._first_board is None:
            self._first_board = _linux_gpib_board_count()
        enumerated = transport.find_adapters()
        known = {_identity(board.info): name for name, board in self._boards.items()}
        boards: Dict[str, _Board] = {}
        surplus: List[AdapterInfo] = []
        for info in enumerated:
            name = known.get(_identity(info))
            if name is None:
                continue  # new adapter, numbered below
            current = self._boards[name]
            if current.busy:
                boards[name] = current       # in use: keep its handle, drop the duplicate
                surplus.append(info)
            else:
                boards[name] = _Board(info)  # same adapter, fresh handle
                surplus.append(current.info)
        for name, board in self._boards.items():
            if name in boards:
                continue
            if board.busy:
                boards[name] = board  # unplugged mid-session; its sessions still hold it
            else:
                surplus.append(board.info)
        number = self._first_board
        for info in enumerated:
            if _identity(info) in known:
                continue
            while str(number) in boards:
                number += 1
            boards[str(number)] = _Board(info)
        for info in surplus:
            transport.dispose_adapter(info)  # a string read during enumeration opened it
        self._boards = boards
        self._enumerated = True

    def owns(self, board: str) -> bool:
        """Whether ``board`` is one of ours; a miss looks at the USB bus once more.

        An adapter plugged in after the first enumeration must be reachable by
        ``open_resource`` without a ``list_resources`` first, and a miss that
        fell through to pyvisa-py would read as "install linux-gpib".
        """
        with self._lock:
            if not self._enumerated or board not in self._boards:
                self._refresh_locked()
            return board in self._boards

    def board_names(self) -> List[str]:
        with self._lock:
            if not self._enumerated:
                self._refresh_locked()
            return sorted(self._boards, key=int)

    def acquire(self, board: str) -> Controller:
        """The attached controller for ``board``; opened on first use. KeyError if unknown."""
        with self._lock:
            if not self._enumerated:
                self._refresh_locked()
            entry = self._boards[board]
            entry.sessions += 1  # counted before the open, so a refresh meanwhile keeps this handle
        try:
            with entry.lock:  # waits out a close of the same adapter that is still running
                if entry.controller is None:
                    entry.controller = self._open(entry.info)
                    logger.info('GPIB%s: %s attached (%s)', board, entry.info.label,
                                _instructions_label(entry.controller))
                return entry.controller
        except BaseException:
            with self._lock:
                entry.sessions -= 1
                self._enumerated = False  # the hardware may have changed; look again next time
            raise

    def _open(self, info: AdapterInfo) -> Controller:
        usb_transport: Optional[Transport] = None
        controller: Optional[Controller] = None
        try:
            usb_transport = self._open_transport(info)
            controller = Controller(usb_transport, info.product_id, ni_instructions=ni_instructions_enabled())
            controller.attach()
        except Exception:  # noqa: BLE001 - whatever failed, the claimed interface must not leak
            if controller is not None:
                controller.close()
            elif usb_transport is not None:
                usb_transport.close()
            raise
        return controller

    def release(self, board: str) -> None:
        with self._lock:
            entry = self._boards.get(board)
            if entry is None or entry.sessions == 0:
                return
            entry.sessions -= 1
            if entry.sessions:
                return
            # Nobody can be opening this board: an opener counts itself in
            # first. So this does not wait, and whoever opens the board from
            # now on waits behind it until the close below is done.
            entry.lock.acquire()
        try:
            controller, entry.controller = entry.controller, None
            if controller is not None:
                controller.close()  # may wait for an operation in flight; the registry's lock is free
                logger.info('GPIB%s: closed', board)
        finally:
            entry.lock.release()

    def list_interfaces(self) -> List[str]:
        """``GPIB<n>::INTFC`` for every board. Enumerates USB (which opens handles for
        descriptors, see ``transport``); attaches no adapter."""
        self.refresh()
        return ['GPIB%s::INTFC' % board for board in self.board_names()]

    def list_instruments(self) -> List[str]:
        """``GPIB<n>::<pad>::INSTR`` for every listener found on every board.

        Not side-effect free: attaches each idle adapter (IFC, REN, take
        control), probes every primary address, and shuts it down again.
        """
        self.refresh()
        names: List[str] = []
        for board in self.board_names():
            try:
                controller = self.acquire(board)
            except (GpibError, TransportError) as exc:
                logger.debug('GPIB%s: not listed, %s', board, exc)
                continue
            try:
                for pad in ops.find_listeners(controller, PROBE_ADDRESSES):
                    names.append('GPIB%s::%d::INSTR' % (board, pad))
            except (GpibError, TransportError) as exc:
                logger.debug('GPIB%s: listener probe failed, %s', board, exc)
            finally:
                self.release(board)
        return names

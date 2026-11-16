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

An adapter that leaves the USB bus comes back, when it is replugged, as a
new USB device with the same serial (spec §11.2, "Hot-unplug mid-run"). A
board whose adapter was found gone -- by an operation, by the close, or by
an open that could not claim the old device -- is marked stale, and its
next ``acquire`` enumerates first and opens the device found for it: with
its sessions closed the board is rebuilt from the enumeration, with
sessions still open on it (which keep failing on the old controller) it
takes the new device in place. An open that finds the device gone looks at
the bus once more and tries again, so an unplug and replug between sessions,
which nothing saw, costs no failed open either.

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
from .protocol import AdapterGone, GpibError
from .transport import AdapterInfo, Transport, TransportError, TransportGone

logger = logging.getLogger(__name__)

#: Every primary address; the controller skips its own.
PROBE_ADDRESSES = tuple(range(31))
#: Set to 1 / true / yes / on to use the instructions NI's driver was captured
#: sending: 0x0b / 0x0e with the data raw on the alternate endpoints for large
#: transfers, and 0x10 for the serial poll. Unset, 0 or anything else keeps
#: what ran on the bench: every transfer framed (0x0a / 0x0d) and the serial
#: poll as the §5.9 command sequence. On bench unit 01CEE482 (2026-09-21,
#: §11.2) an earlier form of the 0x0b read answers of up to 35 000 bytes
#: whole but, with nothing to read, ended at 20.0 s whatever its code, and
#: the 0x10 serial poll's status byte agreed with ``*STB?``. The raw read
#: and write were changed on 2026-09-22 to send NI's own messages, and in
#: that form ran on the same unit on 2026-09-23 (§10.11): the 0x0b ended at
#: its code's expiry, and the 0x0e wrote up to 6000 bytes and failed at
#: once at an empty address. With the switch on, pyvisa's reads, of 20480
#: bytes by default, all take 0x0b.
#: Read once per board open, here and nowhere else.
INSTRUCTIONS_ENV = 'NI_GPIB_USB_INSTRUCTIONS'
#: Aliases of ``INSTRUCTIONS_ENV``, with the same spellings: the name the
#: switch had inside ResistaMet, and its first name, from when it covered
#: the transfers only.
NI_INSTRUCTIONS_ENV = 'RESISTAMET_GPIB_NI_INSTRUCTIONS'
RAW_TRANSFERS_ENV = 'RESISTAMET_GPIB_RAW_TRANSFERS'
#: The names in the order they are looked up.
INSTRUCTIONS_ENVS = (INSTRUCTIONS_ENV, NI_INSTRUCTIONS_ENV, RAW_TRANSFERS_ENV)


def ni_instructions_enabled() -> bool:
    """Whether the switch is on.

    The first of ``INSTRUCTIONS_ENVS`` that is set decides, whatever its
    value, so the neutral name wins over both aliases and an explicit 0
    under it keeps the framed paths even when an alias says 1.
    """
    for name in INSTRUCTIONS_ENVS:
        value = os.environ.get(name)
        if value is not None:
            return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return False


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
        #: Its adapter was found gone from the USB bus: ``info`` names a device that no
        #: longer exists, until an enumeration hands the board the one that came back.
        self.stale = False

    @property
    def busy(self) -> bool:
        """Sessions hold it, or its adapter is still being closed: its handle must stay."""
        return bool(self.sessions) or self.lock.locked()

    @property
    def gone(self) -> bool:
        """Its adapter was found gone from the USB bus: its handle is dead."""
        return self.stale or (self.controller is not None and self.controller.adapter_gone)


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
            if current.busy and current.gone:
                # Replugged under open sessions: they keep counting on this board, and
                # the next acquire opens the new device. The old handle is released by
                # the old controller's close, not here.
                current.info = info
                current.stale = False
                boards[name] = current
            elif current.busy:
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
        """The attached controller for ``board``; opened on first use. KeyError if unknown.

        An open that finds the board's device gone marks the board stale and
        is tried once more after an enumeration, which hands the board the
        device that came back if the adapter was replugged.
        """
        for attempt in (1, 2):
            with self._lock:
                if not self._enumerated or (board in self._boards and self._boards[board].gone):
                    self._refresh_locked()
                entry = self._boards[board]
                entry.sessions += 1  # counted before the open, so a refresh meanwhile keeps this handle
            try:
                with entry.lock:  # waits out a close of the same adapter that is still running
                    if entry.controller is not None and entry.controller.adapter_gone:
                        # The sessions still open on it fail on the old controller; this one
                        # gets the device the refresh above found, if the adapter is back.
                        entry.controller.close()
                        entry.controller = None
                        entry.stale = True
                    if entry.controller is None:
                        entry.controller = self._open(entry.info)
                        logger.info('GPIB%s: %s attached (%s)', board, entry.info.label,
                                    _instructions_label(entry.controller))
                    return entry.controller
            except (TransportGone, AdapterGone) as exc:
                with self._lock:
                    entry.sessions -= 1
                    entry.stale = True
                    self._enumerated = False
                if attempt == 2:
                    raise
                logger.info('GPIB%s: %s is gone (%s); looking at the USB bus again', board, entry.info.label, exc)
            except BaseException:
                with self._lock:
                    entry.sessions -= 1
                    self._enumerated = False  # the hardware may have changed; look again next time
                raise
        raise AssertionError('unreachable')  # pragma: no cover

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
        gone = False
        try:
            controller, entry.controller = entry.controller, None
            if controller is not None:
                controller.close()  # may wait for an operation in flight; the registry's lock is free
                gone = controller.adapter_gone
                logger.info('GPIB%s: closed', board)
        finally:
            entry.lock.release()
        if gone:
            with self._lock:
                # Its info names a USB device that no longer exists; look at the bus
                # again before the board is next opened or owned.
                entry.stale = True
                self._enumerated = False

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

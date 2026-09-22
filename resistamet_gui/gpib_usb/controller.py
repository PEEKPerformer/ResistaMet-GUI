"""One NI GPIB-USB adapter, driven as controller-in-charge.

``protocol`` knows what the bytes look like; ``transport`` moves them. This
module owns the order things happen in: the attach sequence (§2.8), the
addressing before every transfer (§6), the go-to-standby between command
bytes and a read (the ATN rule at the top of §5), 16-byte command chunks
(§5.3), and turning a nonzero error code into an exception in exactly one
place. Everything is synchronous and the controller starts no thread of its
own. Every operation holds one re-entrant lock from start to end, with one
exception: ``wait_srq`` releases it while its interrupt read blocks, so a
caller with a second thread can run other operations during the wait (and
``close`` waits for that read to end). Sequences built from these primitives
(device clear, trigger, the presence probe) live in ``device_ops``.

Faults: a malformed reply or a USB error means the bulk pipes may be out of
step (§8.2). The offending operation raises, and the next operation resets
the bulk pipes and re-runs the attach sequence before doing anything else
(the first attach resets nothing). In between, the adapter is sent a stop
request and its pipe drained, once per fault: at once after a malformed
reply, and after a USB error -- a timeout of the stop request or of a
message the adapter did not take included -- behind the pipe resets of the
re-attach, since a halted pipe could not be drained before them. Without
the drain the reply the failed operation never read would be taken for the
reply to the next message. The adapter's own ways of ending an
instruction are not faults (§10.6.5-10.6.7): a STALL on the alternate OUT
for a 0x0e that cannot start, a zero-length transfer on the alternate IN
for a 0x0b that got nothing, a 0x10 reply without its result block. Each
comes with an ordinary reply carrying the error code, and the next
operation follows without a stop request or a re-attach.

The interrupt endpoint is not armed at attach (§2.5 calls it optional and
operation without it reliable), so attach skips the interrupt-monitor-mask
steps 4 and 6 of §2.8 and ``status()`` polls the control endpoint instead.
``wait_srq`` reads it on demand: the adapter pushes one 8-byte packet there
when an instrument asserts SRQ, having serial-polled it itself (§10.4.2).
That wait has no caller in the application and has not run on hardware; see
the note at the top of its class in ``srq``.

Transfers of every size take the framed 0x0a / 0x0d instructions unless the
controller is built with ``ni_instructions=True``: those are the paths that
have run on our bench, and the application's every read is a large one
(pyvisa asks for 20480 bytes at a time). A framed read asks for at most
``FRAMED_READ_MAX_BYTES`` (1024) in one 0x0a: a larger request is a sequence
of 0x0a instructions of that count or less, after one addressing, until END,
the count or an error. NI's driver sends no framed read above 1024 -- from
1025 up it uses 0x0b (§10.1.1, §10.1.8) -- so a bigger 0x0a is unobserved on
the wire, and on bench unit 01CEE482 a 20480-byte 0x0a was fatal once the
answer outgrew a ceiling: answers up to 4200 bytes came back whole in one
reply, a 7000-byte answer drew no reply at all and left the adapter
answering nothing until it was unplugged (§11.2). Switched on, large
transfers take the instructions NI's own driver uses (§10): a read of
``RAW_READ_MIN_BYTES`` or more is a 0x0b whose bytes arrive unframed on the
alternate bulk IN endpoint, and a write of ``RAW_WRITE_MIN_BYTES`` or more
is a 0x0e whose bytes go out unframed on the alternate bulk OUT; smaller
transfers stay framed, as does everything on a model without the alternate
pair. The raw paths were written from the captures alone, and what this
driver sends around them (a 0x0c, a 0x06 and the 0x0b as three messages,
under AUXRA 0x81, without NI's bank-2 session configuration) is a
composition no capture shows. They stay off until they have run against
an adapter of ours. The same switch selects the serial poll: off, it is the
IEEE-488.1 command sequence of §5.9 (``device_ops.serial_poll``); on, NI's
0x10 instruction (``serial_poll_instruction``).

Bench notes (GPIB-USB-HS 01CEE482, Keithley 2400 at PAD 3, 2026-09-18): the
attach sequence, addressing, the framed write and read and the presence
probe all work as written. The serial poll that ran that day was the
IEEE-488.1 command sequence of §5.9, which is still the default; the 0x10
instruction has not run on hardware. Instruments need a moment after IFC and REN before the
first addressed command (``IFC_SETTLE_S``); without it the 2400 silently
dropped the first query after a close-then-attach, and the adapter hung once
under the backend at exactly that point. The read reply's trailer is 16 bytes, not the 28 the
specification derived (see ``protocol``). The count field of a status reply
is only meaningful after 0x0a/0x0c/0x0d; other replies carry stale or marker
bytes there, which is why nothing here reads it elsewhere.

Layout: ``Controller`` owns an ``AdapterLink`` (``link``), which holds the
exchange primitives, the fault recovery and the state they share, and
inherits the transfer paths from ``transfers``, the wait for a service
request from ``srq`` and the model-specific attach steps from ``attach``.
"""
import logging
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, Optional, Tuple

from . import protocol as p
from . import tables as t
from .attach import _AttachMixin
# The constants of the mixins are re-exported: callers import them from here.
from .link import (BUS_MIN_RATE_BPS, DEFAULT_INFINITE_WAIT_S, DRAIN_WAIT_S,  # noqa: F401
                   RECOVERY_WAIT_S, SHORT_WAIT_S, AdapterLink)
from .protocol import AdapterNotReady, GpibError, NoReply, ProtocolError, StatusBlock
from .srq import SRQ_WAIT_SLICE_S, _SrqMixin
from .transfers import (ADAPTER_OUT_BUFFER_BYTES, FRAMED_READ_MAX_BYTES, RAW_READ_MIN_BYTES,  # noqa: F401
                        RAW_READ_SLICE_S, RAW_REPLY_POLL_S, RAW_WRITE_MIN_BYTES, _TransferMixin)
from .transport import Transport, TransportError

logger = logging.getLogger(__name__)

#: Device timeout for bus housekeeping that has no session timeout of its own.
DEFAULT_TIMEOUT_S = 3.0
#: Pause after an IFC pulse / REN change before the first addressed command.
#: Observed on the bench: a Keithley 2400 that was in remote state, then saw
#: REN drop at shutdown and IFC + REN at the next attach, handshakes command
#: and data bytes arriving within ~1 ms in hardware but never parses them
#: (it later reports -420 Query UNTERMINATED); 20 ms was already enough.
#: This is five times that.
IFC_SETTLE_S = 0.1

_LISTEN = 'listen'
_TALK = 'talk'


class Controller(_AttachMixin, _SrqMixin, _TransferMixin):
    """Sequencing rules for one adapter over one ``Transport``."""

    def __init__(self, transport: Transport, product_id: int, *,
                 own_address: int = 0, t1_ns: int = 2000,
                 infinite_wait_s: float = DEFAULT_INFINITE_WAIT_S,
                 ni_instructions: bool = False,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        """``ni_instructions`` True uses the instructions NI's driver was captured
        sending and our bench has not run: 0x0b / 0x0e for large transfers, on a
        model with the alternate pair, and 0x10 for the serial poll (see the
        module docstring). The default keeps every transfer on the framed 0x0a /
        0x0d paths and the serial poll on the §5.9 command sequence, the ones
        proven on the bench. The SRQ wait is unaffected."""
        model = t.MODELS.get(product_id)
        if model is None:
            raise ValueError('unsupported product id 0x%04x' % product_id)
        if model.needs_firmware:
            raise AdapterNotReady('%s needs its firmware uploaded before it can be driven'
                                  % model.name)
        if not 0 <= own_address <= 30:
            raise ValueError('own address %d outside 0..30' % own_address)
        self._ni_instructions = bool(ni_instructions)
        #: The pipes to the adapter and the exchange state over them. 0x0b /
        #: 0x0e are used only when the caller has switched them on and the
        #: model has the alternate pair.
        self._link = AdapterLink(transport, model, raw=self._ni_instructions and model.raw_endpoints,
                                 infinite_wait_s=infinite_wait_s)
        self._own_address = own_address
        self._t1_ns = t1_ns
        self._sleep = sleep
        self._lock = threading.RLock()
        self._attached = False
        self._closed = False
        self._system_controller = True
        #: (direction, pad, sad) of the last successful addressing command,
        #: so a caller that disables re-addressing can skip a repeat.
        self._addressed: Optional[Tuple[str, int, Optional[int]]] = None
        #: Clear while a ``wait_srq`` has an interrupt read in flight; ``close``
        #: waits for it so the transport is not released under a pending transfer.
        self._srq_idle = threading.Event()
        self._srq_idle.set()
        #: From the serial-number query (or the USB-B register read).
        self.serial_number: Optional[int] = None

    @property
    def model(self) -> t.Model:
        return self._link.model

    @property
    def own_address(self) -> int:
        return self._own_address

    @property
    def ni_instructions(self) -> bool:
        """Whether the caller asked for NI's instructions; decides the serial poll (``device_ops``)."""
        return self._ni_instructions

    @property
    def raw_transfers(self) -> bool:
        """Whether large transfers take the 0x0b / 0x0e instructions (§10)."""
        return self._link.raw

    @property
    def system_controller(self) -> bool:
        """Whether attach set the adapter up as system controller (§2.6 row 16)."""
        return self._system_controller

    @property
    def lock(self) -> threading.RLock:
        """Hold this to make a sequence of operations atomic (see ``device_ops``)."""
        return self._lock

    # ------------------------------------------------------------------
    # §2.8 attach, §2.9 shutdown
    # ------------------------------------------------------------------

    def attach(self, system_controller: bool = True) -> None:
        """§2.8 in order. Step 1 (claiming the interface) is the transport's."""
        with self._guard():
            if self._closed:
                raise AdapterNotReady('controller is closed')
            if self._attached:
                return
            self._system_controller = system_controller
            self._link.drained = False
            self._addressed = None
            if self._link.model.readiness_poll:
                self._readiness_poll()                              # step 2
            if self._link.model.hs_plus_extras:
                self._hs_plus_extras()                              # step 3 (HS+)
            if not self._link.model.readiness_poll:
                self.serial_number = self._usb_b_serial()           # step 3 (USB-B)
            # Step 4 (monitor mask 0x0000) skipped: the interrupt endpoint is not used.
            # Own secondary addressing stays disabled (rows 20-22 of §2.6).
            try:
                self._link.register_write(                          # step 5
                    t.register_init_writes(self._own_address, system_controller, self._t1_ns),
                    'register initialisation')
            except NoReply as exc:
                # Seen on the bench: a hung adapter answers every control request,
                # swallows bulk messages until its FIFO fills and never replies. A
                # USB reset does not clear it; only a power cycle does.
                raise AdapterNotReady(
                    '%s accepted the initialisation message but never replied: the '
                    'adapter is hung. Unplug it and plug it back in.' % self._link.model.name) from exc
            # Step 6 (monitor mask 0x10ff) skipped for the same reason.
            if system_controller:                                   # step 7
                self._interface_clear()
                self._remote_enable(True)
                # Error 5 here just means nothing is on the bus yet (§8.12).
                self._link.status_exchange(p.take_control_message(True), SHORT_WAIT_S,
                                           'take control', tolerate=(t.ERR_NO_ACCEPTOR,))
                self._sleep(IFC_SETTLE_S)  # let the instruments finish reacting to IFC/REN
            self._attached = True
            self._link.resync_pending = False

    def close(self) -> None:
        """§2.9: chip reset, the device-level register, release the interface.

        A ``wait_srq`` in progress sees ``_closed`` at its next slice and
        leaves; this waits for that before touching the transport, since
        releasing the interface under a pending transfer is undefined.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if not self._srq_idle.wait(SRQ_WAIT_SLICE_S + SHORT_WAIT_S):
            logger.warning('%s: a wait for a service request did not end; releasing the adapter anyway',
                           self._link.model.name)
        with self._lock:
            try:
                if self._attached and not self._link.resync_pending:
                    self._link.register_write(t.SHUTDOWN_WRITES, 'shutdown')
            except (GpibError, TransportError) as exc:
                logger.warning('shutdown register write failed: %s', exc)
            finally:
                self._attached = False
                self._link.transport.close()

    # ------------------------------------------------------------------
    # bus control
    # ------------------------------------------------------------------

    def interface_clear(self) -> None:
        with self._guard():
            self._ensure_attached()
            self._interface_clear()
            self._sleep(IFC_SETTLE_S)

    def remote_enable(self, on: bool) -> None:
        with self._guard():
            self._ensure_attached()
            self._remote_enable(on)

    def take_control(self, synchronous: bool = True) -> StatusBlock:
        with self._guard():
            self._ensure_attached()
            return self._link.status_exchange(p.take_control_message(synchronous), SHORT_WAIT_S,
                                              'take control')

    def go_to_standby(self) -> StatusBlock:
        with self._guard():
            self._ensure_attached()
            return self._go_to_standby()

    def _interface_clear(self) -> None:
        self._addressed = None
        self._link.status_exchange(p.interface_clear_message(), SHORT_WAIT_S, 'interface clear')

    def _remote_enable(self, on: bool) -> None:
        self._link.register_write((t.REN_ON_WRITE if on else t.REN_OFF_WRITE,), 'remote enable')

    def _go_to_standby(self) -> StatusBlock:
        return self._link.status_exchange(p.go_to_standby_message(), SHORT_WAIT_S, 'go to standby')

    # ------------------------------------------------------------------
    # data transfer
    # ------------------------------------------------------------------

    def write(self, pad: int, data: bytes, *, sad: Optional[int] = None,
              send_eoi: bool = True, timeout_s: Optional[float],
              eos_char: Optional[int] = None, readdress: bool = True) -> int:
        """Address ``pad`` to listen, then write ``data`` (§5.1, §10.5).

        Writes of ``RAW_WRITE_MIN_BYTES`` and more go as 0x0e instructions
        with the bytes on the alternate bulk OUT; shorter ones as framed
        0x0d. ``eos_char`` fills the 0x0e header's ``e`` byte, which NI sets
        to the session's termination character (§10.5.2); the framed 0x0d
        keeps its bench-proven 0x00 there.
        """
        with self._guard():
            self._ensure_attached()
            if not data:
                return 0
            code = p.timeout_code(timeout_s)
            self._address(_LISTEN, pad, sad, code, self._link.reply_wait_s(code), readdress)
            return self._write_bytes(data, code, send_eoi, eos_char)

    def write_raw(self, data: bytes, *, send_eoi: bool = True,
                  timeout_s: Optional[float], eos_char: Optional[int] = None) -> int:
        """Write with the bus as it stands: no addressing.

        For callers that addressed the bus themselves with command bytes. The
        adapter reports error 3 or 8 when nothing is addressed to listen.
        """
        with self._guard():
            self._ensure_attached()
            if not data:
                return 0
            return self._write_bytes(data, p.timeout_code(timeout_s), send_eoi, eos_char)

    def read(self, pad: int, *, sad: Optional[int] = None, max_bytes: int,
             timeout_s: Optional[float], eos: Optional[int] = None,
             eos_8bit: bool = False, readdress: bool = True) -> Tuple[bytes, bool]:
        """Address ``pad`` to talk, go to standby, then read up to ``max_bytes`` (§5.2, §10.1).

        Returns the data and whether END (EOI, or the EOS character when
        ``eos`` is given) ended it. False means the count was reached. A
        device-side timeout raises ``GpibTimeout`` carrying the partial data.
        On the framed path no single 0x0a asks for more than
        ``FRAMED_READ_MAX_BYTES``; a larger request is read in pieces after
        the one addressing (``_read_bytes``).
        Without ``eos`` the instruction's ``m e`` bytes are the bench-proven
        ``00 00``. NI puts the session's termination character into ``e``
        even then (§10.1.6), under its own AUXRA value; the codec can build
        that form, this controller does not send it.
        """
        with self._guard():
            self._ensure_attached()
            if max_bytes < 1:
                return b'', False
            code = p.timeout_code(timeout_s)
            self._address(_TALK, pad, sad, code, self._link.reply_wait_s(code), readdress)
            # ATN rule (§5): a 0x06 between the addressing 0x0c and the read.
            self._go_to_standby()
            return self._read_bytes(max_bytes, code, eos, eos_8bit, 'read')

    def read_raw(self, max_bytes: int, timeout_s: Optional[float],
                 eos: Optional[int] = None, eos_8bit: bool = False) -> Tuple[bytes, bool]:
        """Read with the bus as it stands: no addressing, no standby.

        For callers that addressed the bus themselves. ATN must already be
        false, else error 2.
        """
        with self._guard():
            self._ensure_attached()
            if max_bytes < 1:
                return b'', False
            return self._read_bytes(max_bytes, p.timeout_code(timeout_s), eos, eos_8bit, 'read')

    def command(self, command_bytes: bytes,
                timeout_s: Optional[float] = DEFAULT_TIMEOUT_S) -> int:
        """Raw command bytes, 16 per instruction (§5.3). Returns bytes accepted."""
        with self._guard():
            self._ensure_attached()
            # Arbitrary command bytes may change who is addressed.
            self._addressed = None
            code = p.timeout_code(timeout_s)
            wait = self._link.reply_wait_s(code)
            accepted = 0
            for start in range(0, len(command_bytes), p.MAX_COMMAND_BYTES):
                chunk = command_bytes[start:start + p.MAX_COMMAND_BYTES]
                status = self._link.status_exchange(p.command_message(chunk, code), wait, 'command')
                accepted += status.transferred(len(chunk))
            return accepted

    def _address(self, direction: str, pad: int, sad: Optional[int], code: int,
                 wait_s: float, readdress: bool) -> None:
        target = (direction, pad, sad)
        if not readdress and self._addressed == target:
            return
        self._addressed = None
        if direction == _LISTEN:
            command = t.address_listener_command(self._own_address, pad, sad)
        else:
            command = t.address_talker_command(self._own_address, pad, sad)
        self._link.status_exchange(p.command_message(command, code), wait_s, 'address to %s' % direction)
        self._addressed = target

    def serial_poll_instruction(self, pad: int, sad: Optional[int] = None,
                                timeout_s: Optional[float] = DEFAULT_TIMEOUT_S) -> int:
        """The status byte of device ``pad`` through the 0x10 instruction (§10.5.4).

        NI's driver polls this way rather than with the IEEE-488.1 command
        sequence of §5.9. The adapter addresses the bus itself for the poll,
        so whoever was addressed before is forgotten here. Not run on
        hardware yet, and NI's captures all had its bank-2 session
        configuration written first, which this driver does not write;
        ``device_ops.serial_poll`` sends this only when the controller was
        built with ``ni_instructions``, and the §5.9 sequence otherwise.
        """
        with self._guard():
            self._ensure_attached()
            self._addressed = None
            code = p.timeout_code(timeout_s)
            wait = self._link.reply_wait_s(code)
            reply = self._link.transact(p.serial_poll_message(pad, code, sad), p.SMALL_REPLY_BUFFER, wait)
            parsed = p.parse_serial_poll_reply(reply)
            # A failed poll carries no 0x3a block to compare (§10.6.6): the error first.
            self._link.raise_for_error(parsed.status, 'serial poll')
            if parsed.pad != pad or parsed.status_byte is None:
                raise ProtocolError('serial poll answered for address %r, asked %d: %s'
                                    % (parsed.pad, pad, reply.hex()))
            return parsed.status_byte

    # ------------------------------------------------------------------
    # adapter state
    # ------------------------------------------------------------------

    def abort(self) -> StatusBlock:
        """§5.11 stop request. Sequential recovery only: the lock serialises it."""
        with self._guard():
            self._refuse_when_closed()
            return p.parse_status_block(self._link.control(t.STOP_REQUEST))

    def status(self) -> StatusBlock:
        """§5.12 status query: current ibsta without touching the bus."""
        with self._guard():
            self._refuse_when_closed()
            return p.parse_status_block(self._link.control(t.STATUS_QUERY))

    def _refuse_when_closed(self) -> None:
        """For the control requests that need no attach: pyusb would reopen a released handle for them."""
        if self._closed:
            raise AdapterNotReady('controller is closed')

    def bus_lines(self) -> int:
        """§5.13 BSR: REN, IFC, SRQ, EOI, NRFD, NDAC, DAV, ATN as bits."""
        with self._guard():
            self._ensure_attached()
            return self._link.register_read((t.BSR_REGISTER,))[0]

    # ------------------------------------------------------------------
    # the fault rule
    # ------------------------------------------------------------------

    @contextmanager
    def _guard(self) -> Iterator[None]:
        """The lock, plus the §8.2 fault rule around every public operation."""
        with self._lock:
            try:
                yield
            except Exception as exc:
                # Whatever was addressed cannot be trusted after a failure.
                self._addressed = None
                self._link.note_fault(exc)
                raise

    def _ensure_attached(self) -> None:
        if self._closed:
            raise AdapterNotReady('controller is closed')
        if self._link.resync_pending:
            logger.warning('%s: re-running the attach sequence after a fault', self._link.model.name)
            self._attached = False
            self._link.clear_halts_after_fault()
            if not self._link.drained:
                # A USB fault drained nothing when it happened. The reply the
                # failed operation did not read would answer the first message
                # of the attach, and the next operation -- in a run, the one
                # that switches the output off -- would fail in its place.
                self._link.resync()
            self.attach(self._system_controller)
        if not self._attached:
            raise AdapterNotReady('adapter is not attached')

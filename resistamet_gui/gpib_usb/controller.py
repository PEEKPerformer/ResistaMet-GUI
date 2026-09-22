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
reply, and after a USB error -- a timeout of the stop request, or of a
write whose data the bus did not take, included -- behind the pipe resets
of the re-attach, since a halted pipe could not be drained before them. A
message with no data that the adapter does not take is the hung adapter
of §8.17, reported as ``AdapterNotReady`` with the advice to replug it,
and the next operation re-attaches the same way. Without
the drain the reply the failed operation never read would be taken for the
reply to the next message. The adapter's own ways of ending an
instruction are not faults (§10.6.5-10.6.7): a STALL on the alternate OUT
for a 0x0e that cannot start, a zero-length transfer on the alternate IN
for a 0x0b that got nothing, a 0x10 reply without its result block. Each
comes with an ordinary reply carrying the error code, and the next
operation follows without a stop request or a re-attach.

An adapter that has left the USB bus is not a fault either: there is no
pipe to bring back into step. The first USB call that finds the device
gone (libusb's "no such device", errno ENODEV) ends the operation with
``AdapterGone`` at once, with no pipe reset, stop request, drain or
re-attach, and every later operation on the controller raises the same
without touching USB. A replugged adapter is a new USB device, which the
board registry opens afresh (spec §11.2, "Hot-unplug mid-run").

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
the count, an error or the timeout (below). NI's driver sends no framed read above 1024 -- from
1025 up it uses 0x0b (§10.1.1, §10.1.8) -- so a bigger 0x0a is unobserved on
the wire, and on bench unit 01CEE482 a 20480-byte 0x0a was fatal once the
answer outgrew a ceiling: answers up to 4200 bytes came back whole in one
reply, a 7000-byte answer drew no reply at all and left the adapter
answering nothing until it was unplugged (§11.2). Switched on, large
transfers take the instructions NI's own driver uses (§10): a read of
``RAW_READ_MIN_BYTES`` or more is a 0x0b whose bytes arrive unframed on the
alternate bulk IN endpoint, and a write of ``RAW_WRITE_MIN_BYTES`` or more
is a 0x0e whose bytes go out unframed on the alternate bulk OUT; smaller
transfers stay framed. The switch is honoured on the GPIB-USB-HS alone, the
one model NI's driver was captured on; any other model stays framed. The
raw paths were written from the captures alone. On an
instrument session they send NI's messages byte for byte -- the snapshot,
the addressing 0x0c under 0xfd, the 0x0b or 0x0e, the register writes,
all in one message, after NI's bank-2 session configuration (§10.1.2,
§10.2.4, §10.5.2) -- so that the bench can tell whether this driver's
earlier composition (a 0x0c, a 0x06 and the 0x0b as three messages, with
no bank-2 configuration) is why unit 01CEE482 ended a 0x0b at 20.0 s
whatever its code (§11.2). What still differs from NI is the
initialisation (AUXRA 0x81 against NI's 0x99, §10.3.1). They stay off
until they have run against an adapter of ours. The same switch selects
the serial poll: off, it is the IEEE-488.1 command sequence of §5.9
(``device_ops.serial_poll``); on, NI's 0x10 instruction
(``serial_poll_instruction``).

Timeouts. Every operation takes a timeout in seconds, and it means what a
VISA timeout means: the least time to wait before reporting one.

- The code. A timeout becomes the smallest device code under which no
  adapter timed in §7.3 ends an instruction sooner (``protocol.
  timeout_code``). That is NI's code -- the smallest nominal limit not
  below the timeout (§7.1) -- except where NI's would end early: 264 to
  300 ms go out as 0xfb, not 0xfa, which ends at 0.2635 s on the captured
  unit, and 300 s as 0x02, not 0x01, which nobody timed. 2 ms and 5 ms
  take 0xf5 and 0xf6, which still cover them.
- What the adapter then waits, measured (§7.3), captured unit 013CC9DF
  under NI's driver / bench unit 01CEE482 under this one, by the timeout
  asked: up to 2.285 ms, 0xf5, 2.3 ms / not timed; to 5.348 ms, 0xf6,
  5.3-5.5 ms / not timed; to 17.719 ms, 0xf7, 17.7-17.8 ms / not timed; to
  34.088 ms, 0xf8, 34.1 ms / not timed; to 127 ms, 0xf9, 0.132 s / 0.127 s;
  to 263.4 ms, 0xfa, 0.2635 s / 0.375 s; to 1.0498 s, 0xfb, 1.050 s / 1.250
  s; to 3.75 s, 0xfc, 4.196 s / 3.750 s; to 16.778 s, 0xfd, 16.778 s / 20.0
  s; to 33.555 s, 0xfe, 33.556 s / 41.25 s. The longer codes, 0xff (to 100
  s), 0x01 (to 268 s) and 0x02 (to 1000 s), were timed on neither. The
  application's 5 s goes out as 0xfd.
- The host waits for the adapter to say so: the longer of the two units'
  figures plus 2 s (§7.2), plus a second per 1000 bytes of a transfer, and
  on NI's raw messages the 20 s of their addressing block's 0xfd as well.
  Only when that runs out is the stop request sent (§5.11).
- A read is bounded as a whole, from the call on, as NI's one instruction
  is (§7.1, §10.10.2), and by what NI's is bounded by: not the timeout
  asked but the expiry of the code it goes out as, the longer of the two
  units' (5 s: 0xfd, 20.0 s), so that it is never cut off before NI's
  single instruction would have ended on either. A framed read in pieces
  of 1024 gives every piece the timeout's own code and starts none after
  that, so its last piece may run up to one piece past it; a read that
  runs out raises ``GpibTimeout`` with the bytes read so far. A write
  split into several instructions likewise.
- The raw path on unit 01CEE482: a 0x0b of this driver's earlier
  composition ended there at 20.0 s whatever its code (0xf9, 0xfb, 0xfc)
  with nothing to read, so a timeout on it came after 20 s whatever was
  asked (§11.2); reads that returned data were not affected. Whether NI's
  message, now sent, changes that is not yet known.
- 0 and None mean no timeout here: code 0xf0, under which the adapter
  never ends an instruction; the host waits ``infinite_wait_s`` (600 s by
  default), then stops the instruction and reports a timeout. The VISA
  session sends VI_TMO_INFINITE this way; VI_TMO_IMMEDIATE it sends as
  100 ms, 0xf9, since no instruction completes under the shortest codes.

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
from .protocol import AdapterGone, AdapterNotReady, GpibError, NoReply, ProtocolError, StatusBlock
from .srq import SRQ_WAIT_SLICE_S, _SrqMixin
from .transfers import (ADAPTER_OUT_BUFFER_BYTES, FRAMED_READ_MAX_BYTES, RAW_READ_MIN_BYTES,  # noqa: F401
                        RAW_READ_SLICE_S, RAW_REPLY_POLL_S, RAW_WRITE_MIN_BYTES, _TransferMixin)
from .transport import Transport, TransportError, TransportGone

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
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        """``ni_instructions`` True uses the instructions NI's driver was captured
        sending and our bench has not run: 0x0b / 0x0e for large transfers and
        0x10 for the serial poll (see the module docstring), on the one model
        NI's driver was captured on, the GPIB-USB-HS; on any other it is logged
        and ignored. The default keeps every transfer on the framed 0x0a /
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
        if ni_instructions and not model.ni_captured:
            logger.warning('%s: NI\'s instructions were asked for and are not used: only the GPIB-USB-HS '
                           'was captured under NI\'s driver, and what this model\'s alternate endpoints '
                           'carry is not established (spec §1.2, §11.4). Framed transfers and the '
                           '§5.9 serial poll instead.', model.name)
            ni_instructions = False
        self._ni_instructions = bool(ni_instructions)
        #: The pipes to the adapter and the exchange state over them. 0x0b /
        #: 0x0e are used only when the caller has switched them on and the
        #: model has the alternate pair.
        self._link = AdapterLink(transport, model, raw=self._ni_instructions and model.raw_endpoints,
                                 infinite_wait_s=infinite_wait_s)
        self._own_address = own_address
        self._t1_ns = t1_ns
        self._sleep = sleep
        #: Seconds, monotonic: what a transfer's deadline is counted on.
        self._clock = clock
        self._lock = threading.RLock()
        self._attached = False
        self._closed = False
        self._system_controller = True
        #: Clear while a ``wait_srq`` has an interrupt read in flight; ``close``
        #: waits for it so the transport is not released under a pending transfer.
        self._srq_idle = threading.Event()
        self._srq_idle.set()
        #: From the serial-number query (or the USB-B register read).
        self.serial_number: Optional[int] = None
        #: The USB error that showed the adapter gone from the bus; set once, never cleared.
        self._gone: Optional[TransportGone] = None
        #: (pad, sad, code) of NI's bank-2 session configuration last written (§10.2.4),
        #: None when none has been since the attach. Only the raw paths write it.
        self._ni_session_state: Optional[Tuple[int, Optional[int], int]] = None

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
    def adapter_gone(self) -> bool:
        """Whether the adapter has left the USB bus; every operation now raises ``AdapterGone``."""
        return self._gone is not None

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
            self._refuse_when_gone()
            if self._closed:
                raise AdapterNotReady('controller is closed')
            if self._attached:
                return
            self._system_controller = system_controller
            self._link.drained = False
            self._ni_session_state = None
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
                # Seen on the bench (§8.17): a hung adapter answers every control
                # request, swallows bulk messages until its FIFO fills and never
                # replies. A USB reset does not clear it; only a power cycle does.
                # Once its FIFO is full the message is not taken at all, which
                # ``AdapterLink.send`` reports the same way.
                raise self._link.hung('accepted the initialisation message but never replied') from exc
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
                if self._attached and not self._link.resync_pending and self._gone is None:
                    if self._ni_session_state is not None:
                        # NI's close of the last session on the address (§10.3.3).
                        reply = self._link.transact(p.ni_session_close_message(), p.SMALL_REPLY_BUFFER,
                                                    SHORT_WAIT_S)
                        self._check_ni_reply(reply, 'bank-2 session close', (1, 1), strict=False)
                        self._ni_session_state = None
                    self._link.register_write(t.SHUTDOWN_WRITES, 'shutdown')
            except TransportGone as gone:
                # Found only now: recorded, so that the board registry knows the
                # handle is dead and looks for the device that comes back.
                self._adapter_gone(gone)
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
              eos_char: Optional[int] = None) -> int:
        """Address ``pad`` to listen, then write ``data`` (§5.1, §10.5).

        Writes of ``RAW_WRITE_MIN_BYTES`` and more go as 0x0e instructions
        with the bytes on the alternate bulk OUT; shorter ones as framed
        0x0d. ``eos_char`` fills the 0x0e header's ``e`` byte, which NI sets
        to the session's termination character whether or not the compare
        is on (§10.5.1, §10.5.2); the framed 0x0d keeps its bench-proven
        0x00 there. A raw write is NI's message, which addresses the
        instrument itself (``_raw_write_instruction``). ``timeout_s`` bounds
        the write as a whole, from this call on (``_write_bytes``).
        """
        with self._guard():
            deadline = self._deadline(timeout_s)
            self._ensure_attached()
            if not data:
                return 0
            code = p.timeout_code(timeout_s)
            if self._link.raw and len(data) >= RAW_WRITE_MIN_BYTES:
                return self._write_bytes(data, code, send_eoi, eos_char, deadline, address=(pad, sad))
            self._address(_LISTEN, pad, sad, code, self._link.reply_wait_s(code))
            return self._write_bytes(data, code, send_eoi, eos_char, deadline)

    def write_raw(self, data: bytes, *, send_eoi: bool = True,
                  timeout_s: Optional[float], eos_char: Optional[int] = None) -> int:
        """Write with the bus as it stands: no addressing.

        For callers that addressed the bus themselves with command bytes. The
        adapter reports error 3 or 8 when nothing is addressed to listen.
        """
        with self._guard():
            deadline = self._deadline(timeout_s)
            self._ensure_attached()
            if not data:
                return 0
            return self._write_bytes(data, p.timeout_code(timeout_s), send_eoi, eos_char, deadline)

    def read(self, pad: int, *, sad: Optional[int] = None, max_bytes: int,
             timeout_s: Optional[float], eos: Optional[int] = None,
             eos_8bit: bool = False, termchar: Optional[int] = None) -> Tuple[bytes, bool]:
        """Address ``pad`` to talk, go to standby, then read up to ``max_bytes`` (§5.2, §10.1).

        Returns the data and whether END (EOI, or the EOS character when
        ``eos`` is given) ended it. False means the count was reached. A
        timeout raises ``GpibTimeout`` carrying the partial data; it bounds
        the read as a whole, from this call on, as NI's code bounds its one
        instruction (§7.1, §10.10.2). On the framed path no single 0x0a asks
        for more than ``FRAMED_READ_MAX_BYTES``; a larger request is read in
        pieces after the one addressing (``_read_bytes``).
        Without ``eos`` a framed read's ``m e`` bytes are the bench-proven
        ``00 00``. NI puts the session's termination character into ``e``
        even then (§10.1.6), under its own AUXRA value; a raw read, which is
        NI's message (``_raw_read_instruction``), does so too, with
        ``termchar``, and addresses the instrument itself.
        """
        with self._guard():
            deadline = self._deadline(timeout_s)
            self._ensure_attached()
            if max_bytes < 1:
                return b'', False
            code = p.timeout_code(timeout_s)
            if self._reads_raw(max_bytes):
                return self._read_bytes(max_bytes, code, eos, eos_8bit, 'read', deadline,
                                        address=(pad, sad), termchar=termchar)
            self._address(_TALK, pad, sad, code, self._link.reply_wait_s(code))
            # ATN rule (§5): a 0x06 between the addressing 0x0c and the read.
            self._go_to_standby()
            return self._read_bytes(max_bytes, code, eos, eos_8bit, 'read', deadline)

    def read_raw(self, max_bytes: int, timeout_s: Optional[float],
                 eos: Optional[int] = None, eos_8bit: bool = False) -> Tuple[bytes, bool]:
        """Read with the bus as it stands: no addressing, no standby.

        For callers that addressed the bus themselves. ATN must already be
        false, else error 2.
        """
        with self._guard():
            deadline = self._deadline(timeout_s)
            self._ensure_attached()
            if max_bytes < 1:
                return b'', False
            return self._read_bytes(max_bytes, p.timeout_code(timeout_s), eos, eos_8bit, 'read', deadline)

    def command(self, command_bytes: bytes,
                timeout_s: Optional[float] = DEFAULT_TIMEOUT_S) -> int:
        """Raw command bytes, 16 per instruction (§5.3). Returns bytes accepted."""
        with self._guard():
            self._ensure_attached()
            code = p.timeout_code(timeout_s)
            wait = self._link.reply_wait_s(code)
            accepted = 0
            for start in range(0, len(command_bytes), p.MAX_COMMAND_BYTES):
                chunk = command_bytes[start:start + p.MAX_COMMAND_BYTES]
                status = self._link.status_exchange(p.command_message(chunk, code), wait, 'command')
                accepted += status.transferred(len(chunk))
            return accepted

    def _deadline(self, timeout_s: Optional[float]) -> Optional[float]:
        """When a transfer asked for with ``timeout_s`` must be over, on ``_clock``; None for no timeout.

        Not ``timeout_s`` from now but the expiry of the code it goes out as:
        NI bounds its one instruction by the adapter's expiry under that code,
        not by the timeout asked for (§7.1, §10.10.2), and the expiry can be
        several times the timeout (5 s -> 0xfd -> 16.78 s). It is the
        longest expiry either timed unit showed under the code
        (``tables.timeout_expiry_s``, the figure the host wait is built
        on; for a code not timed on both, the same estimate), so no piece
        is refused before NI's single instruction would have ended on
        either unit: under 0xfc NI read 20480-byte chunks in 3.93-4.00 s
        with error 0 (§10.1.8), inside the captured unit's 4.196 s and past
        the bench unit's 3.750 s. It is never below the timeout asked, so
        VISA's least wait holds too. With the bound at the timeout, an
        answer whose first byte came late filled one piece and was cut off
        where NI's read would have taken all of it.
        """
        code = p.timeout_code(timeout_s)
        if code == t.TIMEOUT_DISABLED_CODE:
            return None
        return self._clock() + t.timeout_expiry_s(code)

    def _address(self, direction: str, pad: int, sad: Optional[int], code: int, wait_s: float) -> None:
        """Address ``pad`` for a transfer, every time (§6).

        No record of who was addressed last lets a repeat be skipped: the
        adapter serial-polls a device that asserts SRQ by itself (§10.4.2),
        which readdresses the bus behind any such record, and NI's own
        driver addresses before every transfer (§10.2.3). VI_ATTR_GPIB_READDR_EN
        is therefore accepted and has no effect.
        """
        if direction == _LISTEN:
            command = t.address_listener_command(self._own_address, pad, sad)
        else:
            command = t.address_talker_command(self._own_address, pad, sad)
        self._link.status_exchange(p.command_message(command, code), wait_s, 'address to %s' % direction)

    def serial_poll_instruction(self, pad: int, sad: Optional[int] = None,
                                timeout_s: Optional[float] = DEFAULT_TIMEOUT_S) -> int:
        """The status byte of device ``pad`` through the 0x10 instruction (§10.5.4).

        NI's driver polls this way rather than with the IEEE-488.1 command
        sequence of §5.9. The adapter addresses the bus itself for the poll.
        Not run on hardware yet, and NI's captures all had its bank-2 session
        configuration written first, which this driver does not write;
        ``device_ops.serial_poll`` sends this only when the controller was
        built with ``ni_instructions``, and the §5.9 sequence otherwise.
        """
        with self._guard():
            self._ensure_attached()
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

    def status(self) -> StatusBlock:
        """§5.12 status query: current ibsta without touching the bus."""
        with self._guard():
            self._refuse_when_closed()
            return p.parse_status_block(self._link.control(t.STATUS_QUERY))

    def _refuse_when_closed(self) -> None:
        """For the control requests that need no attach: pyusb would reopen a released handle for them."""
        self._refuse_when_gone()
        if self._closed:
            raise AdapterNotReady('controller is closed')

    def _refuse_when_gone(self) -> None:
        """Before anything else, closed or not: a controller whose adapter left says so."""
        if self._gone is not None:
            raise self._gone_error() from self._gone

    def _gone_error(self) -> AdapterGone:
        return AdapterGone('%s is no longer on the USB bus (unplugged, or it lost power): the '
                           'operation cannot reach it. Plug it back in and open the instrument again.'
                           % self._link.model.name)

    def _adapter_gone(self, cause: TransportGone) -> AdapterGone:
        """Record that the adapter has left the bus (§11.2); the ``AdapterGone`` to raise for it."""
        if self._gone is None:
            self._gone = cause
            self._attached = False
            logger.warning('%s: the adapter has left the USB bus (%s); no recovery is attempted and '
                           'every later operation fails until it is replugged and opened again',
                           self._link.model.name, cause)
        return self._gone_error()

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
        """The lock, plus the §8.2 fault rule around every public operation.

        An adapter gone from the bus is the exception: it is reported as
        ``AdapterGone``, whether the operation or the recovery after it found
        it, and nothing is recovered.
        """
        with self._lock:
            try:
                yield
            except TransportGone as gone:
                raise self._adapter_gone(gone) from gone
            except Exception as exc:
                try:
                    self._link.note_fault(exc)
                except TransportGone as gone:
                    raise self._adapter_gone(gone) from gone
                raise

    def _ensure_attached(self) -> None:
        self._refuse_when_gone()
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

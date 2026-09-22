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
the note at the top of its section.

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

Size: this module is well past the 400-line guideline. It holds the attach
sequence, the framed and raw transfer paths, the serial poll, the SRQ wait
and the exchange/fault machinery they all share; the fault rule is only
correct if it wraps every exchange, which is why the transfers have not
been moved out yet. Once the raw paths have run on hardware, the data
transfers (``write*``, ``read*`` and their ``_raw_*`` helpers) are the
natural piece to lift into a module of their own.
"""
import logging
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, List, NoReturn, Optional, Tuple

from . import protocol as p
from . import tables as t
# The constants of the mixins are re-exported: callers import them from here.
from .link import (BUS_MIN_RATE_BPS, DEFAULT_INFINITE_WAIT_S, DRAIN_WAIT_S,  # noqa: F401
                   RECOVERY_WAIT_S, SHORT_WAIT_S, _ExchangeMixin)
from .protocol import AdapterNotReady, GpibError, GpibTimeout, NoReply, ProtocolError, StatusBlock
from .transport import Transport, TransportError, TransportTimeout

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
#: Reads of at least this many bytes use the 0x0b instruction with the data
#: on the alternate bulk IN. NI's rule (§10.1.1, read_thresholds.pcap): a
#: requested count of 1024 or less goes as 0x0a, 1025 or more as 0x0b. Only
#: the requested count decides, not how much the instrument then sends, and
#: nothing changes at 2048 or 4096.
RAW_READ_MIN_BYTES = 1025
#: A framed 0x0a never asks for more than this many bytes; a larger framed
#: read is a sequence of 0x0a instructions of at most this count (see
#: ``Controller._read_bytes``). 1024 is the last count NI sends as 0x0a
#: (``RAW_READ_MIN_BYTES`` - 1, §10.1.1), so a framed read above it is
#: unobserved on the wire (§10.1.8), and on bench unit 01CEE482 it was fatal
#: past a ceiling: under a 20480-byte 0x0a, answers of up to 4200 bytes (a
#: 4496-byte reply) came back whole and a 7000-byte answer drew no reply at
#: all and wedged the adapter until it was unplugged (§11.2). A full piece of
#: 1024 is a 1136-byte reply, a quarter of the largest that arrived.
FRAMED_READ_MAX_BYTES = 1024
#: Writes of at least this many bytes use the 0x0e instruction with the data
#: on the alternate bulk OUT. NI's rule (§10.5.2, write_thresholds.pcap):
#: every length up to 2048 goes as one framed 0x0d, 2049 and more as 0x0e.
#: The write boundary is not the read boundary.
RAW_WRITE_MIN_BYTES = 2049
#: The interrupt read of ``wait_srq`` is issued in slices of this length so a
#: ``close`` is noticed between them; the only cost is one extra interrupt
#: read per slice while nothing is pending.
SRQ_WAIT_SLICE_S = 1.0
#: The alternate-IN read of a 0x0b is issued in slices of this length, with a
#: look at the primary IN between two slices (``RAW_REPLY_POLL_S``): a reply
#: that is already there means the instruction is over, whatever the data
#: transfer is doing. §10.6.7 leaves open whether the adapter completes that
#: transfer for read errors other than the timeout; if it does not, one long
#: read would sit out the whole transfer wait with the error reply queued.
RAW_READ_SLICE_S = 1.0
RAW_REPLY_POLL_S = 0.01
#: What the adapter buffers of a framed 0x0d message: the hung adapter of
#: §8.17 took about 4 KB on the primary OUT before it stopped accepting. The
#: OUT of a framed write completes once the adapter holds the message, so up
#: to this much has still to reach the instrument before the reply can come.
ADAPTER_OUT_BUFFER_BYTES = 4096

_LISTEN = 'listen'
_TALK = 'talk'


class Controller(_ExchangeMixin):
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
        self._transport = transport
        self._model = model
        self._ni_instructions = bool(ni_instructions)
        #: Whether 0x0b / 0x0e are used: the caller must have switched them on and
        #: the model must have the alternate pair.
        self._raw = self._ni_instructions and model.raw_endpoints
        self._own_address = own_address
        self._t1_ns = t1_ns
        self._infinite_wait_s = infinite_wait_s
        self._sleep = sleep
        self._lock = threading.RLock()
        self._attached = False
        self._closed = False
        self._system_controller = True
        #: Set after a fault; the next operation re-runs attach first. Cleared
        #: only by an attach that succeeds, so a failed re-attach is retried.
        self._resync_pending = False
        #: The stop-and-drain of §8.2 has run since the last fault. Reset when
        #: a re-attach starts, so a fault during it drains again, once.
        self._drained = False
        #: (direction, pad, sad) of the last successful addressing command,
        #: so a caller that disables re-addressing can skip a repeat.
        self._addressed: Optional[Tuple[str, int, Optional[int]]] = None
        #: Set by the last exchange when the host had to stop the device (§5.11).
        self._host_stopped = False
        #: Clear while a ``wait_srq`` has an interrupt read in flight; ``close``
        #: waits for it so the transport is not released under a pending transfer.
        self._srq_idle = threading.Event()
        self._srq_idle.set()
        #: From the serial-number query (or the USB-B register read).
        self.serial_number: Optional[int] = None

    @property
    def model(self) -> t.Model:
        return self._model

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
        return self._raw

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
            self._drained = False
            self._addressed = None
            if self._model.readiness_poll:
                self._readiness_poll()                              # step 2
            if self._model.hs_plus_extras:
                self._hs_plus_extras()                              # step 3 (HS+)
            if not self._model.readiness_poll:
                self.serial_number = self._usb_b_serial()           # step 3 (USB-B)
            # Step 4 (monitor mask 0x0000) skipped: the interrupt endpoint is not used.
            # Own secondary addressing stays disabled (rows 20-22 of §2.6).
            try:
                self._register_write(                               # step 5
                    t.register_init_writes(self._own_address, system_controller, self._t1_ns),
                    'register initialisation')
            except NoReply as exc:
                # Seen on the bench: a hung adapter answers every control request,
                # swallows bulk messages until its FIFO fills and never replies. A
                # USB reset does not clear it; only a power cycle does.
                raise AdapterNotReady(
                    '%s accepted the initialisation message but never replied: the '
                    'adapter is hung. Unplug it and plug it back in.' % self._model.name) from exc
            # Step 6 (monitor mask 0x10ff) skipped for the same reason.
            if system_controller:                                   # step 7
                self._interface_clear()
                self._remote_enable(True)
                # Error 5 here just means nothing is on the bus yet (§8.12).
                self._status_exchange(p.take_control_message(True), SHORT_WAIT_S,
                                      'take control', tolerate=(t.ERR_NO_ACCEPTOR,))
                self._sleep(IFC_SETTLE_S)  # let the instruments finish reacting to IFC/REN
            self._attached = True
            self._resync_pending = False

    def _readiness_poll(self) -> None:
        reply = self._control(t.SERIAL_NUMBER_QUERY)
        try:
            self.serial_number = p.parse_serial_number(reply)
        except ProtocolError as exc:
            raise AdapterNotReady(str(exc)) from exc
        for attempt in range(t.READINESS_ATTEMPTS):
            try:
                reply = self._control(t.READINESS_QUERY, timeout_ms=t.READINESS_USB_TIMEOUT_MS)
            except TransportTimeout:
                reply = b''
            if reply and p.readiness_reported(reply):
                return
            if attempt + 1 < t.READINESS_ATTEMPTS:
                self._sleep(t.READINESS_INTERVAL_S)
        raise AdapterNotReady('%s did not report ready after %d polls'
                              % (self._model.name, t.READINESS_ATTEMPTS))

    def _hs_plus_extras(self) -> None:
        for request, expected in t.HS_PLUS_INIT_REQUESTS:
            reply = self._control(request)
            if reply != expected:
                # spec gap: only the LED effect of these requests is known, so an
                # unexpected reply is recorded rather than treated as fatal.
                logger.warning('HS+ init request 0x%02x answered %s, expected %s',
                               request.request, reply.hex(), expected.hex())

    def _usb_b_serial(self) -> int:
        values = self._register_read(t.USB_B_SERIAL_REGISTERS)
        return int.from_bytes(bytes(values), 'little')

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
                           self._model.name)
        with self._lock:
            try:
                if self._attached and not self._resync_pending:
                    self._register_write(t.SHUTDOWN_WRITES, 'shutdown')
            except (GpibError, TransportError) as exc:
                logger.warning('shutdown register write failed: %s', exc)
            finally:
                self._attached = False
                self._transport.close()

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
            return self._status_exchange(p.take_control_message(synchronous), SHORT_WAIT_S,
                                         'take control')

    def go_to_standby(self) -> StatusBlock:
        with self._guard():
            self._ensure_attached()
            return self._go_to_standby()

    def _interface_clear(self) -> None:
        self._addressed = None
        self._status_exchange(p.interface_clear_message(), SHORT_WAIT_S, 'interface clear')

    def _remote_enable(self, on: bool) -> None:
        self._register_write((t.REN_ON_WRITE if on else t.REN_OFF_WRITE,), 'remote enable')

    def _go_to_standby(self) -> StatusBlock:
        return self._status_exchange(p.go_to_standby_message(), SHORT_WAIT_S, 'go to standby')

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
            self._address(_LISTEN, pad, sad, code, self._reply_wait_s(code), readdress)
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

    def _write_bytes(self, data: bytes, code: int, send_eoi: bool, eos_char: Optional[int]) -> int:
        """Write instructions of at most 0xffff bytes each, EOI only with the last (§5.1).

        Framed or raw is decided per chunk, so a short tail after a raw
        chunk goes framed: each write instruction is complete in itself, its
        data with it. (The read loop decides once per call instead.)
        """
        # Both instructions carry at most 0xffff bytes, so one chunk size serves.
        step = min(p.MAX_TRANSFER_BYTES, p.MAX_RAW_TRANSFER_BYTES)
        written = 0
        for start in range(0, len(data), step):
            chunk = data[start:start + step]
            eoi = send_eoi and start + len(chunk) == len(data)
            if self._raw and len(chunk) >= RAW_WRITE_MIN_BYTES:
                written += self._raw_write_instruction(chunk, code, eoi, eos_char)
            else:
                # The data rides inside the message, and the adapter takes the
                # message only as fast as the instrument takes the data: the
                # tail of NI's 2080-byte message needed 103 ms (§10.5.2, §7.2).
                # So the OUT follows the device timeout and the byte count, not
                # the short wait. Once it completes all but the adapter's own
                # buffer is on the bus, and the reply waits for that remainder.
                buffered = min(len(chunk), ADAPTER_OUT_BUFFER_BYTES)
                status, _ = self._exchange(p.write_message(chunk, code, eoi), p.STATUS_REPLY_LENGTH,
                                           self._transfer_wait_s(code, buffered), 'write',
                                           out_wait_s=self._transfer_wait_s(code, len(chunk)))
                written += status.transferred(len(chunk))
        return written

    def _raw_write_instruction(self, chunk: bytes, code: int, send_eoi: bool,
                               eos_char: Optional[int]) -> int:
        """One 0x0e (§10.5.2): the header on the primary OUT, the bytes raw on the alternate OUT.

        The device consumes the raw transfer as it writes to the bus (NI's
        2050 bytes took 0.1 s to be accepted), so that transfer, not only the
        reply, gets the host wait. What remains buffered when the transfer
        completes still has to reach the instrument, so the reply gets the
        same wait.

        A write the adapter cannot start -- nothing listens -- is refused at
        the USB level: the raw transfer fails with a STALL a millisecond after
        it was submitted, and the reply arrives by itself with error 8 and
        the count (§10.6.5). NI sends no stop request there; it reads the
        reply and resets the two OUT pipes, and so does this
        (``_refused_raw_write``). Every failure of the raw transfer but a
        timeout is taken for that refusal until the reply says otherwise:
        how libusb on macOS reports the STALL has not been seen, and if an
        unrecognised error skipped this path the reply would stay queued
        and the alternate OUT halted for every later 0x0e.
        """
        message = p.write_raw_message(len(chunk), code, send_eoi, eos_char)
        wait_s = self._transfer_wait_s(code, len(chunk))
        self._host_stopped = False
        self._transport.bulk_out(message, int(SHORT_WAIT_S * 1000))
        try:
            accepted = self._transport.bulk_out_raw(chunk, int(wait_s * 1000))
        except TransportTimeout:
            accepted = 0
        except TransportError as refusal:
            self._refused_raw_write(refusal)
        stranded = accepted < len(chunk)
        if stranded:
            # The host wait ran out with the instrument not accepting: the
            # transport reports bytes moved as a short count, none as a
            # timeout. The device is still mid-instruction, and §5.11 makes it
            # finish so the reply can say how much reached the bus. NI was not
            # observed using the stop request (§10.8); none of its captures
            # has a host wait expiring, which is the one case it is kept for.
            self._host_stopped = True
            self._control(t.STOP_REQUEST)
            reply_wait = RECOVERY_WAIT_S
        else:
            reply_wait = wait_s
        try:
            reply = self._reply_or_stop(p.SMALL_REPLY_BUFFER, reply_wait)
        finally:
            if stranded:
                self._abandon_raw_out()
        parsed = p.parse_raw_write_reply(reply)
        self._raise_for_error(parsed.status, 'write')
        return parsed.transferred(len(chunk))

    def _abandon_raw_out(self) -> None:
        """After a 0x0e the host wait ended: reset the OUT pipes and have the next operation re-attach.

        The adapter took the bytes the transport counted as accepted, and
        the stop request ended the instruction before all of them reached
        the bus. Whatever is left in the alternate OUT FIFO would come out
        in front of the data of the next 0x0e. No capture shows this case
        -- NI's host wait never ran out (§10.6.7, §10.8) -- so whether the
        stop request empties the FIFO, and whether a pipe reset does, is
        not established. The OUT pipes are reset as after a refusal, which
        is the one recovery NI was seen to use on them, and the state is
        treated as unknown: the next operation re-attaches first.
        """
        self._reset_out_pipes()
        self._resync_pending = True

    def _refused_raw_write(self, refusal: TransportError) -> NoReturn:
        """The raw OUT of a 0x0e failed: read the reply, reset the OUT pipes, raise (§10.6.5).

        The failure is the adapter saying the instruction is over, so the
        reply is already due (NI's came 0.3 ms after the STALL) and gets the
        short wait. What is raised is the adapter's own error when the reply
        carries one. For error 8, nobody listens, with the instruction ended
        by the adapter itself, the next operation then follows with nothing
        else done, as in the capture; for any other error, or when the reply
        had to be forced with a stop request, it re-attaches first. Otherwise it
        is the transport error itself, which the fault rule answers with a
        re-attach; a reply that is missing or malformed is drained first.
        The log line names the error as the transport delivered it, which
        is how the bench learns what a STALL looks like under macOS.
        """
        logger.warning('%s: the alternate OUT refused the data of a 0x0e: %s (cause %s, errno %r, '
                       'backend code %r): %s; reading the reply for the reason',
                       self._model.name, type(refusal).__name__, type(refusal.__cause__).__name__,
                       getattr(refusal, 'errno', None), getattr(refusal, 'backend_code', None), refusal)
        status: Optional[StatusBlock] = None
        try:
            status = p.parse_raw_write_reply(self._reply_or_stop(p.SMALL_REPLY_BUFFER, SHORT_WAIT_S)).status
        except ProtocolError as exc:
            logger.warning('%s: no usable reply after the refused data: %s', self._model.name, exc)
            self._resync()
        except TransportError as exc:
            logger.warning('%s: reading the reply after the refused data failed: %s', self._model.name, exc)
        self._reset_out_pipes()
        if status is not None:
            if self._host_stopped or status.error != t.ERR_NO_LISTENER:
                # Only the refusal NI's captures show -- error 8, ended by the
                # adapter -- is known to leave the adapter ready for the next
                # operation. Ended by our stop request, or for another reason,
                # the alternate OUT may hold bytes that would lead the data of
                # the next 0x0e, as after a stranded write.
                self._resync_pending = True
            self._raise_for_error(status, 'write')
        raise refusal

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
            self._address(_TALK, pad, sad, code, self._reply_wait_s(code), readdress)
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

    def _read_bytes(self, max_bytes: int, code: int, eos: Optional[int],
                    eos_8bit: bool, operation: str) -> Tuple[bytes, bool]:
        """Read instructions until END, the count, or a short result; framed or raw by size.

        One instruction carries at most ``FRAMED_READ_MAX_BYTES`` on the
        framed path and 0xffff on the raw one, so a larger request loops.
        The instrument stays addressed between pieces: addressing changes
        only by command bytes (§6), the 0x0a reply reports ATN still false
        (§10.1.5), and an instrument that gave up fewer bytes than it holds
        keeps the rest for the next read (§10.1.7, where NI's second
        ``viRead`` re-addressed first and that was harmless, not needed).
        So the 0x0c and the 0x06 go once per call and each piece costs one
        round trip. Every piece is a whole instruction with its own device
        timeout code and its own host wait (``_transfer_wait_s`` of one
        piece, never of the request), so a long answer under a short code
        completes as long as each piece keeps moving. A timeout mid-loop
        raises with everything read so far as its partial (§5.2: the
        partial data of a timed-out read is valid).

        The requested count decides the instruction, once, as it does for NI
        (§10.1.1): a request that starts as 0x0b stays 0x0b to its last
        chunk. Choosing per chunk switched such a read to a framed 0x0a for
        a tail under the threshold, a change of instruction in the middle
        of one instrument message that NI's rule never produces. The price
        is a last 0x0b with a count below 1025, which NI was not captured
        sending. Whether the count field is 32 bits wide, or 16 followed by
        ``ff ff``, §10.1.2 leaves open; one instruction is capped at 0xffff
        bytes, below which the two readings are the same bytes.
        """
        chunks: List[bytes] = []
        remaining = max_bytes
        raw = self._raw and max_bytes >= RAW_READ_MIN_BYTES
        step = p.MAX_TRANSFER_BYTES if raw else FRAMED_READ_MAX_BYTES
        while remaining > 0:
            count = min(remaining, step)
            try:
                if raw:
                    data, end = self._raw_read_instruction(count, code, eos, eos_8bit, operation)
                else:
                    data, end = self._read_instruction(count, code, self._transfer_wait_s(code, count), eos,
                                                       eos_8bit, operation)
            except GpibTimeout as exc:
                exc.partial = b''.join(chunks) + exc.partial
                raise
            chunks.append(data)
            remaining -= len(data)
            if end or len(data) < count:
                return b''.join(chunks), end
        return b''.join(chunks), False

    def _read_instruction(self, count: int, code: int, wait_s: float, eos: Optional[int],
                          eos_8bit: bool, operation: str) -> Tuple[bytes, bool]:
        """One framed 0x0a (§5.2): the data comes back in blocks on the primary bulk IN."""
        buffer = p.read_reply_buffer_size(count, self._transport.max_packet_size)
        _, reply = self._exchange(p.read_message(count, code, eos, eos_8bit), buffer, wait_s,
                                  operation, tolerate=(t.ERR_TIMEOUT, t.ERR_STOPPED))
        parsed = p.parse_read_reply(reply, count)
        self._raise_for_error(parsed.status, operation, partial=parsed.data)
        return parsed.data, parsed.end

    def _raw_read_instruction(self, count: int, code: int, eos: Optional[int],
                              eos_8bit: bool, operation: str) -> Tuple[bytes, bool]:
        """One 0x0b (§10.1.2-10.1.3): the data arrives raw on the alternate bulk IN, the status on the primary."""
        message = p.read_raw_message(count, code, eos, eos_8bit)
        buffer = p.raw_read_buffer_size(count, self._transport.max_packet_size_raw)
        wait_s = self._transfer_wait_s(code, count)
        data, reply = self._raw_read_transact(message, buffer, wait_s)
        parsed = p.parse_raw_read_reply(reply, count, data)
        self._raise_for_error(parsed.status, operation, partial=parsed.data)
        return parsed.data, parsed.end

    def _raw_read_transact(self, message: bytes, data_buffer: int, wait_s: float) -> Tuple[bytes, bytes]:
        """Send a 0x0b message; collect its data on the alternate IN, then its reply on the primary IN.

        The data is read first because the device sends it first: in every
        capture the alternate-endpoint transfer completed 0.4-0.5 ms before
        the 0x84 reply, and for a read that timed out it completed with zero
        bytes (§10.1.3, §7.2). NI keeps both IN transfers posted before the
        OUT completes; with a synchronous transport the same order of events
        is obtained by issuing the two reads in the order the device fills
        them. The device holds its data in the endpoint until the host reads,
        so the microseconds between the OUT and the first IN cost nothing,
        and the reply cannot arrive before the data on the wire.

        The data read is issued in slices of ``RAW_READ_SLICE_S`` that add
        up to ``wait_s``, with a short look at the primary IN between two
        of them, so a reply that comes without the data transfer ending is
        seen within a slice instead of after the whole wait.
        """
        self._host_stopped = False
        self._transport.bulk_out(message, int(SHORT_WAIT_S * 1000))
        data = b''
        remaining_ms = max(1, int(wait_s * 1000))
        slice_limit_ms = max(1, int(RAW_READ_SLICE_S * 1000))
        while remaining_ms > 0:
            slice_ms = min(slice_limit_ms, remaining_ms)
            try:
                data += self._transport.bulk_in_raw(data_buffer - len(data), slice_ms)
            except TransportTimeout as expired:
                # What the transport had received before the slice ran out (it
                # reports a partial transfer this way) stays; the next read
                # continues the same transfer.
                data += expired.partial
            else:
                # The transfer ended by itself; the reply follows within a millisecond.
                return data, self._reply_or_stop(p.SMALL_REPLY_BUFFER, SHORT_WAIT_S)
            remaining_ms -= slice_ms
            if remaining_ms <= 0:
                break
            try:
                reply = self._transport.bulk_in(p.SMALL_REPLY_BUFFER, max(1, int(RAW_REPLY_POLL_S * 1000)))
            except TransportTimeout:
                continue
            # The instruction is over though its data transfer has not ended:
            # an error the adapter reports on the primary IN alone, or a
            # transfer that ended at the very edge of a slice, which the
            # transport cannot tell from a timeout. No stop request: nothing
            # is in flight. One short read collects what the alternate IN
            # still holds, so it is not left for the next 0x0b; the reply's
            # count then decides whether anything is missing.
            try:
                data += self._transport.bulk_in_raw(data_buffer - len(data), int(DRAIN_WAIT_S * 1000))
            except TransportTimeout as nothing_more:
                data += nothing_more.partial
            return data, reply
        # §5.11: the host wait is over and the device still owes both transfers; make it finish now.
        self._host_stopped = True
        self._control(t.STOP_REQUEST)
        try:
            data += self._transport.bulk_in_raw(data_buffer - len(data), int(RECOVERY_WAIT_S * 1000))
        except TransportTimeout as still:
            # Whether a stopped 0x0b completes its data transfer is not
            # established (a timed-out one does, with zero bytes). The
            # reply's count decides whether anything was lost.
            data += still.partial
        return data, self._reply_or_stop(p.SMALL_REPLY_BUFFER, RECOVERY_WAIT_S)

    def command(self, command_bytes: bytes,
                timeout_s: Optional[float] = DEFAULT_TIMEOUT_S) -> int:
        """Raw command bytes, 16 per instruction (§5.3). Returns bytes accepted."""
        with self._guard():
            self._ensure_attached()
            # Arbitrary command bytes may change who is addressed.
            self._addressed = None
            code = p.timeout_code(timeout_s)
            wait = self._reply_wait_s(code)
            accepted = 0
            for start in range(0, len(command_bytes), p.MAX_COMMAND_BYTES):
                chunk = command_bytes[start:start + p.MAX_COMMAND_BYTES]
                status = self._status_exchange(p.command_message(chunk, code), wait, 'command')
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
        self._status_exchange(p.command_message(command, code), wait_s, 'address to %s' % direction)
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
            wait = self._reply_wait_s(code)
            reply = self._transact(p.serial_poll_message(pad, code, sad), p.SMALL_REPLY_BUFFER, wait)
            parsed = p.parse_serial_poll_reply(reply)
            # A failed poll carries no 0x3a block to compare (§10.6.6): the error first.
            self._raise_for_error(parsed.status, 'serial poll')
            if parsed.pad != pad or parsed.status_byte is None:
                raise ProtocolError('serial poll answered for address %r, asked %d: %s'
                                    % (parsed.pad, pad, reply.hex()))
            return parsed.status_byte

    # ------------------------------------------------------------------
    # service request
    #
    # Status of this section: kept, not proven, and not reachable from the
    # application. pyvisa-py 0.8.1 has no enable_event / wait_on_event, so
    # nothing above the controller calls wait_srq; only tests do. It has never
    # run on hardware. And it rests on a premise the captures do not
    # establish: every capture with a push began after NI's driver already
    # owned the adapter, and in each NI had written its bank-2 session
    # configuration (0x04, 0x05 := PAD, 0x06 := SAD, §10.2.4) before the push
    # was seen -- the only way shown for the adapter to know which device to
    # poll. This driver writes neither those registers nor the monitor mask
    # §2.5 calls for, so whether the adapter pushes anything for it is open.
    # ------------------------------------------------------------------

    def wait_srq(self, timeout_s: Optional[float]) -> int:
        """Block until an instrument requests service; return its status byte (§10.4.2).

        Unproven: no production caller (pyvisa-py 0.8.1 offers no way to
        reach it), never run on hardware, and the pushes NI's driver got
        were all preceded by its bank-2 session configuration, which this
        driver does not write. Treat a timeout here as "no push", not as
        "no service request".

        The adapter answers an SRQ by polling the requesting device itself
        and pushing ``30 18 00 sb ..`` on the interrupt endpoint, ``sb``
        being the status byte with RQS set; a later explicit poll finds RQS
        clear (§10.4.2, §10.9). NI's driver then sends control request 0x3b
        and re-arms its interrupt read; this does the same, the re-arm being
        the next call.

        The interrupt read is issued only here, and the controller lock is
        released while it blocks: a permanently pending read would need a
        thread of its own, and every other operation would have to wait for
        this one otherwise. The read runs in slices of ``SRQ_WAIT_SLICE_S``
        so a ``close`` from another thread is noticed between them. The cost
        is that a push arriving while nobody waits sits in the adapter until
        the next call (which then returns at once); whether the adapter
        keeps more than one is not established. One wait at a time.
        ``timeout_s`` None waits the controller's infinite wait; it must
        otherwise be positive (``ValueError``): a zero wait would mean "is
        a push already queued?", which the interrupt endpoint cannot be
        asked without blocking -- libusb reads a timeout of 0 as no timeout
        at all -- so pretending to answer it would be wrong either way. A
        ``GpibTimeout`` means no request arrived in time.
        """
        if timeout_s is not None and not timeout_s > 0:
            raise ValueError('wait_srq needs a positive timeout or None, not %r' % (timeout_s,))
        with self._lock:
            self._ensure_attached()
            if not self._srq_idle.is_set():
                raise GpibError('a wait for a service request is already in progress')
            self._srq_idle.clear()
            transport = self._transport
        try:
            push = self._interrupt_read_in_slices(transport, timeout_s)
        finally:
            self._srq_idle.set()
        parsed = p.parse_srq_push(push)  # a malformed push does not put the bulk pipes out of step
        with self._guard():
            if self._closed:
                raise AdapterNotReady('controller is closed')
            transport.control_out(t.SRQ_ACKNOWLEDGE.request, t.SRQ_ACKNOWLEDGE.value,
                                  t.SRQ_ACKNOWLEDGE.index, b'', t.CONTROL_TIMEOUT_MS,
                                  request_type=t.SRQ_ACKNOWLEDGE.request_type)
        return parsed.status_byte

    def _interrupt_read_in_slices(self, transport: Transport, timeout_s: Optional[float]) -> bytes:
        """The next interrupt push, waited for in slices; called with the lock released.

        Counted in whole milliseconds, the unit the transport takes, and no
        slice is ever 0 ms: libusb reads that as no timeout at all, the read
        would never return, and ``close`` would give up waiting for it and
        release the transport under it. A remainder under a millisecond ends
        the wait instead.
        """
        total_s = self._infinite_wait_s if timeout_s is None else timeout_s
        remaining_ms = max(1, int(round(total_s * 1000)))
        slice_limit_ms = max(1, int(round(SRQ_WAIT_SLICE_S * 1000)))
        while True:
            if self._closed:
                raise AdapterNotReady('controller is closed')
            slice_ms = min(slice_limit_ms, remaining_ms)
            try:
                return transport.interrupt_in(t.INTERRUPT_READ_LENGTH, slice_ms)
            except TransportTimeout as exc:
                remaining_ms -= slice_ms
                if remaining_ms <= 0:
                    raise GpibTimeout('no service request within %.3g s' % total_s) from exc
            except TransportError:
                with self._lock:
                    self._resync_pending = True
                raise

    # ------------------------------------------------------------------
    # adapter state
    # ------------------------------------------------------------------

    def abort(self) -> StatusBlock:
        """§5.11 stop request. Sequential recovery only: the lock serialises it."""
        with self._guard():
            self._refuse_when_closed()
            return p.parse_status_block(self._control(t.STOP_REQUEST))

    def status(self) -> StatusBlock:
        """§5.12 status query: current ibsta without touching the bus."""
        with self._guard():
            self._refuse_when_closed()
            return p.parse_status_block(self._control(t.STATUS_QUERY))

    def _refuse_when_closed(self) -> None:
        """For the control requests that need no attach: pyusb would reopen a released handle for them."""
        if self._closed:
            raise AdapterNotReady('controller is closed')

    def bus_lines(self) -> int:
        """§5.13 BSR: REN, IFC, SRQ, EOI, NRFD, NDAC, DAV, ATN as bits."""
        with self._guard():
            self._ensure_attached()
            return self._register_read((t.BSR_REGISTER,))[0]

    # ------------------------------------------------------------------
    # the exchange primitives
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
                if isinstance(exc, ProtocolError):
                    self._resync()
                elif isinstance(exc, TransportError):
                    # A USB timeout that gets this far was not a reply the host
                    # gave up on (that becomes ``NoReply``): the stop request
                    # itself failed, or the adapter did not take a message.
                    # Either way a reply may be queued that nobody will read;
                    # the re-attach drains it (``_ensure_attached``).
                    self._resync_pending = True
                raise

    def _ensure_attached(self) -> None:
        if self._closed:
            raise AdapterNotReady('controller is closed')
        if self._resync_pending:
            logger.warning('%s: re-running the attach sequence after a fault', self._model.name)
            self._attached = False
            self._clear_halts_after_fault()
            if not self._drained:
                # A USB fault drained nothing when it happened. The reply the
                # failed operation did not read would answer the first message
                # of the attach, and the next operation -- in a run, the one
                # that switches the output off -- would fail in its place.
                self._resync()
            self.attach(self._system_controller)
        if not self._attached:
            raise AdapterNotReady('adapter is not attached')

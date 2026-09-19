"""One NI GPIB-USB adapter, driven as controller-in-charge.

``protocol`` knows what the bytes look like; ``transport`` moves them. This
module owns the order things happen in: the attach sequence (§2.8), the
addressing before every transfer (§6), the go-to-standby between command
bytes and a read (the ATN rule at the top of §5), 16-byte command chunks
(§5.3), and turning a nonzero error code into an exception in exactly one
place. Everything is synchronous and serialised behind one re-entrant lock;
there are no background threads. Sequences built from these primitives
(device clear, trigger, the presence probe) live in ``device_ops``.

Faults: a malformed reply or a USB error means the bulk pipes may be out of
step (§8.2). The offending operation raises, the adapter is sent a stop
request and its pipe drained, and the next operation re-runs the attach
sequence before doing anything else. The adapter's own ways of ending an
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

Large transfers take the instructions NI's own driver uses (§10): a read of
``RAW_READ_MIN_BYTES`` or more is a 0x0b whose bytes arrive unframed on the
alternate bulk IN endpoint, and a write of ``RAW_WRITE_MIN_BYTES`` or more
is a 0x0e whose bytes go out unframed on the alternate bulk OUT. Smaller
transfers keep the bench-proven framed 0x0a / 0x0d paths, as do models
without the alternate pair. The raw paths were written from the captures
alone and have not run against an adapter of ours yet.

Bench notes (GPIB-USB-HS 01CEE482, Keithley 2400 at PAD 3, 2026-09-18): the
attach sequence, addressing, write, read, serial poll and the presence probe
all work as written. Instruments need a moment after IFC and REN before the
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
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

from . import protocol as p
from . import tables as t
from .protocol import AdapterNotReady, GpibError, GpibTimeout, NoReply, ProtocolError, StatusBlock
from .transport import Transport, TransportError, TransportStall, TransportTimeout

logger = logging.getLogger(__name__)

#: Host wait for the bulk replies §7.2 puts at "1 s minimum" (0x01, 0x06,
#: 0x08, 0x09, 0x0f), and for every bulk OUT.
SHORT_WAIT_S = 2.0
#: Wait for the reply the device owes after a stop request (§5.11).
RECOVERY_WAIT_S = 2.0
#: Wait when draining a stale reply after a malformed one (§8.2).
DRAIN_WAIT_S = 0.2
#: Device timeout for bus housekeeping that has no session timeout of its own.
DEFAULT_TIMEOUT_S = 3.0
#: Pause after an IFC pulse / REN change before the first addressed command.
#: Observed on the bench: a Keithley 2400 that was in remote state, then saw
#: REN drop at shutdown and IFC + REN at the next attach, handshakes command
#: and data bytes arriving within ~1 ms in hardware but never parses them
#: (it later reports -420 Query UNTERMINATED); 20 ms was already enough.
#: This is five times that.
IFC_SETTLE_S = 0.1
#: spec gap: with the device timeout disabled (code 0xf0) §7.2 leaves the
#: host wait to the application. Ten minutes; on expiry the operation is
#: stopped (§5.11) and reported as a timeout.
DEFAULT_INFINITE_WAIT_S = 600.0
#: Reads of at least this many bytes use the 0x0b instruction with the data
#: on the alternate bulk IN. NI's rule (§10.1.1, read_thresholds.pcap): a
#: requested count of 1024 or less goes as 0x0a, 1025 or more as 0x0b. Only
#: the requested count decides, not how much the instrument then sends, and
#: nothing changes at 2048 or 4096.
RAW_READ_MIN_BYTES = 1025
#: Writes of at least this many bytes use the 0x0e instruction with the data
#: on the alternate bulk OUT. NI's rule (§10.5.2, write_thresholds.pcap):
#: every length up to 2048 goes as one framed 0x0d, 2049 and more as 0x0e.
#: The write boundary is not the read boundary.
RAW_WRITE_MIN_BYTES = 2049
#: The interrupt read of ``wait_srq`` is issued in slices of this length so a
#: ``close`` is noticed between them; the only cost is one extra interrupt
#: read per slice while nothing is pending.
SRQ_WAIT_SLICE_S = 1.0
#: The device timeout code bounds a handshake interval, not the whole
#: instruction (§10.1.8: 20480-byte chunks took 4.0 s each under the 3 s code
#: and completed with error 0), so the host wait for a raw transfer must also
#: cover the transfer itself. This is the slowest instrument pace assumed: the
#: host wait grows by one second per this many bytes. The 2420 formats at
#: about 5000 bytes per second (§10.1.4). This is a driver choice, not a
#: specification value.
RAW_TRANSFER_MIN_RATE_BPS = 1000

_LISTEN = 'listen'
_TALK = 'talk'


class Controller:
    """Sequencing rules for one adapter over one ``Transport``."""

    def __init__(self, transport: Transport, product_id: int, *,
                 own_address: int = 0, t1_ns: int = 2000,
                 infinite_wait_s: float = DEFAULT_INFINITE_WAIT_S,
                 raw_transfers: bool = True,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        """``raw_transfers`` False keeps every transfer on the framed 0x0a / 0x0d
        paths even on a model with the alternate pair, for a like-for-like
        comparison on the bench; the serial poll and the SRQ wait are unaffected."""
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
        #: Whether 0x0b / 0x0e are used: the model must have the alternate pair and
        #: the caller must not have switched them off.
        self._raw = bool(raw_transfers) and model.raw_endpoints
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
            code, limit = p.effective_timeout(timeout_s)
            wait = p.host_wait_s(limit, self._infinite_wait_s)
            self._address(_LISTEN, pad, sad, code, wait, readdress)
            return self._write_bytes(data, code, limit, send_eoi, eos_char)

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
            code, limit = p.effective_timeout(timeout_s)
            return self._write_bytes(data, code, limit, send_eoi, eos_char)

    def _write_bytes(self, data: bytes, code: int, limit: Optional[float], send_eoi: bool,
                     eos_char: Optional[int]) -> int:
        """Write instructions of at most 0xffff bytes each, EOI only with the last (§5.1).

        Framed or raw is decided per chunk, like the read loop, so a short
        tail after a raw chunk goes framed.
        """
        # Both instructions carry at most 0xffff bytes, so one chunk size serves.
        step = min(p.MAX_TRANSFER_BYTES, p.MAX_RAW_TRANSFER_BYTES)
        written = 0
        for start in range(0, len(data), step):
            chunk = data[start:start + step]
            eoi = send_eoi and start + len(chunk) == len(data)
            if self._raw and len(chunk) >= RAW_WRITE_MIN_BYTES:
                written += self._raw_write_instruction(chunk, code, limit, eoi, eos_char)
            else:
                wait = p.host_wait_s(limit, self._infinite_wait_s)
                status, _ = self._exchange(p.write_message(chunk, code, eoi), p.STATUS_REPLY_LENGTH,
                                           wait, 'write')
                written += status.transferred(len(chunk))
        return written

    def _raw_write_instruction(self, chunk: bytes, code: int, limit: Optional[float], send_eoi: bool,
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
        reply and resets the two OUT pipes, and so does this.
        """
        message = p.write_raw_message(len(chunk), code, send_eoi, eos_char)
        wait_s = p.host_wait_s(limit, self._infinite_wait_s) + len(chunk) / RAW_TRANSFER_MIN_RATE_BPS
        self._host_stopped = False
        self._transport.bulk_out(message, int(SHORT_WAIT_S * 1000))
        refused = False
        try:
            accepted = self._transport.bulk_out_raw(chunk, int(wait_s * 1000))
        except TransportStall:
            refused, accepted = True, 0
        except TransportTimeout:
            accepted = 0
        if refused:
            # The STALL is the adapter saying the instruction is over, so the
            # reply is already due (NI's came 0.3 ms later).
            reply_wait = SHORT_WAIT_S
        elif accepted < len(chunk):
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
            if refused:
                self._reset_out_pipes()
        parsed = p.parse_raw_write_reply(reply)
        self._raise_for_error(parsed.status, 'write')
        if refused:
            raise ProtocolError('0x0e data was refused with a STALL but the reply reports no error: %s'
                                % reply.hex())
        return parsed.transferred(len(chunk))

    def _reset_out_pipes(self) -> None:
        """Clear the halt a refused 0x0e leaves on the alternate OUT, then reset the primary OUT.

        NI's order (§10.6.5). The primary OUT had reported no error and why
        NI resets it is not established; it is followed because the next
        operation was then seen to work without anything else. If a reset
        fails the pipes cannot be trusted, and the next operation re-attaches.
        """
        for endpoint in (self._model.endpoint_out_raw, self._model.endpoint_out):
            if endpoint is None:
                continue
            try:
                self._transport.clear_halt(endpoint)
            except TransportError as exc:
                logger.warning('%s: clearing the halt on endpoint 0x%02x failed: %s',
                               self._model.name, endpoint, exc)
                self._resync_pending = True

    def read(self, pad: int, *, sad: Optional[int] = None, max_bytes: int,
             timeout_s: Optional[float], eos: Optional[int] = None,
             eos_8bit: bool = False, termchar: Optional[int] = None,
             readdress: bool = True) -> Tuple[bytes, bool]:
        """Address ``pad`` to talk, go to standby, then read up to ``max_bytes`` (§5.2, §10.1).

        Returns the data and whether END (EOI, or the EOS character when
        ``eos`` is given) ended it. False means the count was reached. A
        device-side timeout raises ``GpibTimeout`` carrying the partial data.
        ``termchar`` fills the instruction's ``e`` byte when ``eos`` is None,
        as NI does (§10.1.6); None sends the bench-proven 0x00.
        """
        with self._guard():
            self._ensure_attached()
            if max_bytes < 1:
                return b'', False
            code, limit = p.effective_timeout(timeout_s)
            wait = p.host_wait_s(limit, self._infinite_wait_s)
            self._address(_TALK, pad, sad, code, wait, readdress)
            # ATN rule (§5): a 0x06 between the addressing 0x0c and the read.
            self._go_to_standby()
            return self._read_bytes(max_bytes, code, limit, eos, eos_8bit, termchar, 'read')

    def read_raw(self, max_bytes: int, timeout_s: Optional[float],
                 eos: Optional[int] = None, eos_8bit: bool = False,
                 termchar: Optional[int] = None) -> Tuple[bytes, bool]:
        """Read with the bus as it stands: no addressing, no standby.

        For callers that addressed the bus themselves. ATN must already be
        false, else error 2.
        """
        with self._guard():
            self._ensure_attached()
            if max_bytes < 1:
                return b'', False
            code, limit = p.effective_timeout(timeout_s)
            return self._read_bytes(max_bytes, code, limit, eos, eos_8bit, termchar, 'read')

    def _read_bytes(self, max_bytes: int, code: int, limit: Optional[float], eos: Optional[int],
                    eos_8bit: bool, termchar: Optional[int], operation: str) -> Tuple[bytes, bool]:
        """Read instructions until END, the count, or a short result; framed or raw by size.

        One instruction carries at most 0xffff bytes on either path, so a
        larger request loops; the instrument stays addressed between chunks
        (§10.1.7 shows re-addressing is harmless, and none is needed). A
        timeout mid-loop raises with everything read so far as its partial.
        """
        chunks: List[bytes] = []
        remaining = max_bytes
        while remaining > 0:
            count = min(remaining, p.MAX_TRANSFER_BYTES)
            try:
                if self._raw and count >= RAW_READ_MIN_BYTES:
                    data, end = self._raw_read_instruction(count, code, limit, eos, eos_8bit, termchar,
                                                           operation)
                else:
                    wait = p.host_wait_s(limit, self._infinite_wait_s)
                    data, end = self._read_instruction(count, code, wait, eos, eos_8bit, termchar, operation)
            except GpibTimeout as exc:
                exc.partial = b''.join(chunks) + exc.partial
                raise
            chunks.append(data)
            remaining -= len(data)
            if end or len(data) < count:
                return b''.join(chunks), end
        return b''.join(chunks), False

    def _read_instruction(self, count: int, code: int, wait_s: float, eos: Optional[int],
                          eos_8bit: bool, termchar: Optional[int], operation: str) -> Tuple[bytes, bool]:
        """One framed 0x0a (§5.2): the data comes back in blocks on the primary bulk IN."""
        buffer = p.read_reply_buffer_size(count, self._transport.max_packet_size)
        _, reply = self._exchange(p.read_message(count, code, eos, eos_8bit, termchar), buffer, wait_s,
                                  operation, tolerate=(t.ERR_TIMEOUT, t.ERR_STOPPED))
        parsed = p.parse_read_reply(reply, count)
        self._raise_for_error(parsed.status, operation, partial=parsed.data)
        return parsed.data, parsed.end

    def _raw_read_instruction(self, count: int, code: int, limit: Optional[float], eos: Optional[int],
                              eos_8bit: bool, termchar: Optional[int], operation: str) -> Tuple[bytes, bool]:
        """One 0x0b (§10.1.2-10.1.3): the data arrives raw on the alternate bulk IN, the status on the primary."""
        message = p.read_raw_message(count, code, eos, eos_8bit, termchar)
        buffer = p.raw_read_buffer_size(count, self._transport.max_packet_size_raw)
        wait_s = p.host_wait_s(limit, self._infinite_wait_s) + count / RAW_TRANSFER_MIN_RATE_BPS
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
        """
        self._host_stopped = False
        self._transport.bulk_out(message, int(SHORT_WAIT_S * 1000))
        try:
            data = self._transport.bulk_in_raw(data_buffer, int(wait_s * 1000))
            reply_wait = SHORT_WAIT_S  # the reply follows the data within a millisecond
        except TransportTimeout as expired:
            # §5.11: the device still owes both transfers; make it finish now.
            # What the transport had received before its wait ran out (the
            # transport reports a partial transfer this way) stays.
            self._host_stopped = True
            self._control(t.STOP_REQUEST)
            reply_wait = RECOVERY_WAIT_S
            try:
                data = expired.partial + self._transport.bulk_in_raw(data_buffer - len(expired.partial),
                                                                     int(RECOVERY_WAIT_S * 1000))
            except TransportTimeout as still:
                # Whether a stopped 0x0b completes its data transfer is not
                # established (a timed-out one does, with zero bytes). The
                # reply's count decides whether anything was lost.
                data = expired.partial + still.partial
        return data, self._reply_or_stop(p.SMALL_REPLY_BUFFER, reply_wait)

    def command(self, command_bytes: bytes,
                timeout_s: Optional[float] = DEFAULT_TIMEOUT_S) -> int:
        """Raw command bytes, 16 per instruction (§5.3). Returns bytes accepted."""
        with self._guard():
            self._ensure_attached()
            # Arbitrary command bytes may change who is addressed.
            self._addressed = None
            code, limit = p.effective_timeout(timeout_s)
            wait = p.host_wait_s(limit, self._infinite_wait_s)
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

    def serial_poll(self, pad: int, sad: Optional[int] = None,
                    timeout_s: Optional[float] = DEFAULT_TIMEOUT_S) -> int:
        """The status byte of device ``pad`` through the 0x10 instruction (§10.5.4).

        NI's driver polls this way rather than with the IEEE-488.1 command
        sequence of §5.9. The adapter addresses the bus itself for the poll,
        so whoever was addressed before is forgotten here.
        """
        with self._guard():
            self._ensure_attached()
            self._addressed = None
            code, limit = p.effective_timeout(timeout_s)
            wait = p.host_wait_s(limit, self._infinite_wait_s)
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
    # ------------------------------------------------------------------

    def wait_srq(self, timeout_s: Optional[float]) -> int:
        """Block until an instrument requests service; return its status byte (§10.4.2).

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
        """The next interrupt push, waited for in slices; called with the lock released."""
        remaining = self._infinite_wait_s if timeout_s is None else timeout_s
        while True:
            if self._closed:
                raise AdapterNotReady('controller is closed')
            slice_s = min(SRQ_WAIT_SLICE_S, remaining)
            try:
                return transport.interrupt_in(t.INTERRUPT_READ_LENGTH, int(slice_s * 1000))
            except TransportTimeout as exc:
                remaining -= slice_s
                if remaining <= 0:
                    raise GpibTimeout('no service request within %.3g s'
                                      % (self._infinite_wait_s if timeout_s is None else timeout_s)) from exc
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
            return p.parse_status_block(self._control(t.STOP_REQUEST))

    def status(self) -> StatusBlock:
        """§5.12 status query: current ibsta without touching the bus."""
        with self._guard():
            return p.parse_status_block(self._control(t.STATUS_QUERY))

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
                elif isinstance(exc, TransportError) and not isinstance(exc, TransportTimeout):
                    self._resync_pending = True
                raise

    def _resync(self) -> None:
        """§8.2: stop whatever is in flight, drain one stale reply, re-attach later."""
        self._resync_pending = True
        if self._drained:
            return  # nested guards report the same fault; one drain per fault
        self._drained = True
        logger.warning('%s: reply out of step; stopping and draining the bulk pipe',
                       self._model.name)
        try:
            self._control(t.STOP_REQUEST)
            self._transport.bulk_in(
                p.read_reply_buffer_size(p.MAX_TRANSFER_BYTES, self._transport.max_packet_size),
                int(DRAIN_WAIT_S * 1000))
        except TransportTimeout:
            logger.debug('nothing to drain')
        except TransportError as exc:
            logger.debug('drain after a malformed reply failed: %s', exc)
        if self._raw:
            # Data of an interrupted 0x0b, or the zero-length packet that ends
            # a full-length one, may still sit on the alternate IN (§10.1.4).
            try:
                self._transport.bulk_in_raw(
                    p.raw_read_buffer_size(p.MAX_RAW_TRANSFER_BYTES, self._transport.max_packet_size_raw),
                    int(DRAIN_WAIT_S * 1000))
            except TransportTimeout:
                logger.debug('nothing to drain on the alternate endpoint')
            except TransportError as exc:
                logger.debug('alternate-endpoint drain failed: %s', exc)

    def _ensure_attached(self) -> None:
        if self._closed:
            raise AdapterNotReady('controller is closed')
        if self._resync_pending:
            logger.warning('%s: re-running the attach sequence after a fault', self._model.name)
            self._attached = False
            self.attach(self._system_controller)
        if not self._attached:
            raise AdapterNotReady('adapter is not attached')

    def _control(self, request: t.ControlRequest,
                 timeout_ms: int = t.CONTROL_TIMEOUT_MS) -> bytes:
        return self._transport.control_in(request.request, request.value, request.index,
                                          request.length, timeout_ms,
                                          request_type=request.request_type)

    def _transact(self, message: bytes, reply_length: int, wait_s: float) -> bytes:
        """One message out, its one reply in (§3.1); stop and collect on a host timeout."""
        self._host_stopped = False
        self._transport.bulk_out(message, int(SHORT_WAIT_S * 1000))
        return self._reply_or_stop(reply_length, wait_s)

    def _reply_or_stop(self, reply_length: int, wait_s: float) -> bytes:
        """The reply on the primary IN; on a host timeout, §5.11: stop the device and collect.

        After a stop request already sent for this exchange the wait is the
        recovery wait, and a second miss is ``NoReply``.
        """
        try:
            return self._transport.bulk_in(reply_length, int(wait_s * 1000))
        except TransportTimeout as exc:
            if self._host_stopped:
                raise NoReply('adapter did not answer after a stop request') from exc
            self._host_stopped = True
            self._control(t.STOP_REQUEST)
            try:
                return self._transport.bulk_in(reply_length, int(RECOVERY_WAIT_S * 1000))
            except TransportTimeout as exc2:
                raise NoReply('adapter did not answer after a stop request') from exc2

    def _exchange(self, message: bytes, reply_length: int, wait_s: float, operation: str,
                  tolerate: Sequence[int] = ()) -> Tuple[StatusBlock, bytes]:
        """Send, receive, check the echoed id, and raise for a nonzero error code.

        Every bulk instruction with a status block goes through here, so the
        error-code mapping of §4.3 lives in ``_raise_for_error`` alone. The
        register read (0x08) has no status block and uses ``_transact`` directly.
        """
        reply = self._transact(message, reply_length, wait_s)
        opcode = message[0]
        if opcode == p.OP_READ:
            offset, expected_id = p.read_status_offset(reply), p.BLOCK_READ_STATUS
        else:
            offset, expected_id = 0, opcode
        status = p.parse_status_block(reply, offset)
        if status.id != expected_id:
            raise ProtocolError('%s: reply id 0x%02x, expected 0x%02x: %s'
                                % (operation, status.id, expected_id, reply.hex()))
        self._raise_for_error(status, operation, tolerate=tolerate)
        return status, reply

    def _status_exchange(self, message: bytes, wait_s: float, operation: str,
                         tolerate: Sequence[int] = ()) -> StatusBlock:
        """An instruction whose reply is the exact 12-byte status reply (§3.5)."""
        status, reply = self._exchange(message, p.STATUS_REPLY_LENGTH, wait_s, operation, tolerate)
        p.parse_status_reply(reply, message[0])  # asserts the exact length
        return status

    def _register_write(self, writes: Sequence[Tuple[int, int, int]], operation: str) -> StatusBlock:
        status, reply = self._exchange(p.register_write_message(writes),
                                       p.REGISTER_WRITE_REPLY_LENGTH, SHORT_WAIT_S, operation)
        _, completed = p.parse_register_write_reply(reply)
        if completed != len(writes):
            # §8.14: the device stopped at a bad (bank, addr) pair.
            raise ProtocolError('%s: %d of %d register writes completed'
                                % (operation, completed, len(writes)))
        return status

    def _register_read(self, reads: Sequence[Tuple[int, int]]) -> List[int]:
        # The only reply without a status block (§3.5).
        reply = self._transact(p.register_read_message(reads), p.REGISTER_READ_REPLY_LENGTH,
                               SHORT_WAIT_S)
        return p.parse_register_read_reply(reply, len(reads))

    def _raise_for_error(self, status: StatusBlock, operation: str,
                         tolerate: Sequence[int] = (), partial: bytes = b'') -> None:
        code = status.error
        if code == t.ERR_SUCCESS or code in tolerate:
            return
        if code == t.ERR_STOPPED and self._host_stopped:
            # Our own stop request ended it: from the caller's side, a timeout.
            raise GpibTimeout('%s: no reply within the host wait' % operation, partial, code)
        exc = p.error_for_code(code, operation)
        if isinstance(exc, GpibTimeout):
            exc.partial = partial
        raise exc

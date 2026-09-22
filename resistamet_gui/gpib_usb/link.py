"""The exchange with the adapter, and the recovery when it goes wrong.

One message out and its one reply in (§3.1), the host wait for that reply
(§7.2), the stop request when the wait runs out (§5.11), the one place a
nonzero error code becomes an exception (§4.3), and the stop-and-drain and
pipe resets that bring the bulk pipes back into step after a fault (§8.2,
§10.6.5). ``Controller`` owns one ``AdapterLink``, which holds the state
these share: the transport and model, whether the raw transfers are on,
the infinite wait, and the fault and stop flags. The constants are
re-exported by ``controller``.
"""
import logging
from typing import List, Optional, Sequence, Tuple

from . import protocol as p
from . import tables as t
from .protocol import AdapterNotReady, GpibTimeout, NoReply, ProtocolError, StatusBlock
from .transport import Transport, TransportError, TransportGone, TransportTimeout

#: The controller's logger: these are its methods, and they log as it.
logger = logging.getLogger(__name__.rpartition('.')[0] + '.controller')

#: Host wait for the bulk replies §7.2 puts at "1 s minimum" (0x01, 0x06,
#: 0x08, 0x09, 0x0f), and for the bulk OUT of every message that carries no
#: data. A message or transfer that carries write data is paced by the bus
#: and gets ``AdapterLink.transfer_wait_s`` instead.
SHORT_WAIT_S = 2.0
#: Wait for the reply the device owes after a stop request (§5.11).
RECOVERY_WAIT_S = 2.0
#: Wait when draining a stale reply after a malformed one (§8.2).
DRAIN_WAIT_S = 0.2
#: spec gap: with the device timeout disabled (code 0xf0) §7.2 leaves the
#: host wait to the application. Ten minutes; on expiry the operation is
#: stopped (§5.11) and reported as a timeout.
DEFAULT_INFINITE_WAIT_S = 600.0
#: The host wait for a transfer grows by one second per this many bytes, on
#: top of the expiry of its code. Under NI's messages the code bounds the
#: whole instruction, data moving or not (§7.1, §10.10.2), and expiry + 2 s
#: covers it (§7.2); the allowance stays as margin for what no capture shows:
#: a framed 0x0a cut off while data arrives, the expiry of a 0x0d or 0x0e
#: (§7.3), and unit 01CEE482, whose 0x0b of this driver's ran to 20.0 s
#: whatever its code (§11.2). The host wait is only the backstop for an
#: instruction the adapter does not end itself; a longer one costs nothing
#: when it does. 1000 bytes a second is below the 2420's pace (about 5000
#: formatting, §10.1.4; about 5600 taking write data, §10.5.2). A driver
#: choice, not a specification value.
BUS_MIN_RATE_BPS = 1000


class AdapterLink:
    """The exchange primitives and fault recovery of one ``Controller`` (see the module docstring)."""

    def __init__(self, transport: Transport, model: t.Model, *, raw: bool, infinite_wait_s: float) -> None:
        self.transport = transport
        self.model = model
        #: Whether 0x0b / 0x0e are used (see ``Controller.raw_transfers``).
        self.raw = raw
        self.infinite_wait_s = infinite_wait_s
        #: Set after a fault; the next operation re-runs attach first. Cleared
        #: only by an attach that succeeds, so a failed re-attach is retried.
        self.resync_pending = False
        #: The stop-and-drain of §8.2 has run since the last fault. Reset when
        #: a re-attach starts, so a fault during it drains again, once.
        self.drained = False
        #: Set by the last exchange when the host had to stop the device (§5.11).
        self.host_stopped = False

    def note_fault(self, exc: BaseException) -> None:
        """The §8.2 fault rule for an exception that is leaving an operation."""
        if isinstance(exc, ProtocolError):
            self.resync()
        elif isinstance(exc, TransportError):
            # A USB timeout that gets this far was not a reply the host
            # gave up on (that becomes ``NoReply``): the stop request
            # itself failed, or the bus did not take a write's data.
            # Either way a reply may be queued that nobody will read;
            # the re-attach drains it (``_ensure_attached``).
            self.resync_pending = True

    def control(self, request: t.ControlRequest,
                timeout_ms: int = t.CONTROL_TIMEOUT_MS) -> bytes:
        return self.transport.control_in(request.request, request.value, request.index,
                                         request.length, timeout_ms,
                                         request_type=request.request_type)

    def hung(self, what: str) -> AdapterNotReady:
        """The error for the hung adapter of §8.17, which only a power cycle clears."""
        return AdapterNotReady('%s %s: the adapter is hung. Unplug it and plug it back in.'
                               % (self.model.name, what))

    def send(self, message: bytes, paced_wait_s: Optional[float] = None) -> None:
        """Put one message on the primary OUT (§3.1).

        ``paced_wait_s`` is for the one message the bus paces, the 0x0d with
        its data inline, which the adapter takes only as fast as the
        instrument takes the data (§10.5.2); a timeout of that is the
        transfer's. Every other message is taken at once by an adapter that
        works, since the reply to the one before it has been read (§8.2):
        not taken within ``SHORT_WAIT_S``, the adapter is in the state of
        §8.17 -- it had taken about 4 KB, then NAKed every packet and never
        answered -- and is reported so, with the advice to replug it, which
        is all that cleared it on the bench. The next operation re-attaches
        first, which finds the state again if it persists.
        """
        if paced_wait_s is not None:
            self.transport.bulk_out(message, int(paced_wait_s * 1000))
            return
        try:
            self.transport.bulk_out(message, int(SHORT_WAIT_S * 1000))
        except TransportTimeout as exc:
            self.resync_pending = True
            raise self.hung('did not take a %d-byte message within %.0f s' % (len(message), SHORT_WAIT_S)) from exc

    def transact(self, message: bytes, reply_length: int, wait_s: float,
                 paced_wait_s: Optional[float] = None) -> bytes:
        """One message out (``send``), its one reply in (§3.1); stop and collect on a host timeout."""
        self.host_stopped = False
        self.send(message, paced_wait_s)
        return self.reply_or_stop(reply_length, wait_s)

    def reply_or_stop(self, reply_length: int, wait_s: float) -> bytes:
        """The reply on the primary IN; on a host timeout, §5.11: stop the device and collect.

        The stop request is from §5.11 alone: it is in none of NI's
        captures, whose failed and timed-out instructions all ended by
        themselves with a normal reply (§10.6.7, §10.8). With the host wait
        outlasting the device timeout it is reached only when the adapter
        does not answer at all, or when the device timeout is disabled.
        """
        try:
            return self.transport.bulk_in(reply_length, int(wait_s * 1000))
        except TransportTimeout:
            self.stop_device()
            return self.reply_after_stop(reply_length)

    def stop_device(self) -> None:
        """§5.11: the host wait is over; make the device end the instruction now."""
        self.host_stopped = True
        self.control(t.STOP_REQUEST)

    def reply_after_stop(self, reply_length: int) -> bytes:
        """The reply the device owes after ``stop_device``, in the recovery wait; else ``NoReply``."""
        try:
            return self.transport.bulk_in(reply_length, int(RECOVERY_WAIT_S * 1000))
        except TransportTimeout as exc:
            raise NoReply('adapter did not answer after a stop request') from exc

    def exchange(self, message: bytes, reply_length: int, wait_s: float, operation: str,
                 tolerate: Sequence[int] = (), paced_wait_s: Optional[float] = None) -> Tuple[StatusBlock, bytes]:
        """Send, receive, check the echoed id, and raise for a nonzero error code.

        Every bulk instruction with a status block goes through here, so the
        error-code mapping of §4.3 lives in ``raise_for_error`` alone. The
        register read (0x08) has no status block and uses ``transact`` directly.
        """
        reply = self.transact(message, reply_length, wait_s, paced_wait_s)
        opcode = message[0]
        if opcode == p.OP_READ:
            offset, expected_id = p.read_status_offset(reply), p.BLOCK_READ_STATUS
        else:
            offset, expected_id = 0, opcode
        status = p.parse_status_block(reply, offset)
        if status.id != expected_id:
            raise ProtocolError('%s: reply id 0x%02x, expected 0x%02x: %s'
                                % (operation, status.id, expected_id, reply.hex()))
        self.raise_for_error(status, operation, tolerate=tolerate)
        return status, reply

    def status_exchange(self, message: bytes, wait_s: float, operation: str,
                        tolerate: Sequence[int] = ()) -> StatusBlock:
        """An instruction whose reply is the exact 12-byte status reply (§3.5)."""
        status, reply = self.exchange(message, p.STATUS_REPLY_LENGTH, wait_s, operation, tolerate)
        p.parse_status_reply(reply, message[0])  # asserts the exact length
        return status

    def register_write(self, writes: Sequence[Tuple[int, int, int]], operation: str) -> StatusBlock:
        status, reply = self.exchange(p.register_write_message(writes),
                                      p.REGISTER_WRITE_REPLY_LENGTH, SHORT_WAIT_S, operation)
        _, completed = p.parse_register_write_reply(reply)
        if completed != len(writes):
            # §8.14: the device stopped at a bad (bank, addr) pair.
            raise ProtocolError('%s: %d of %d register writes completed'
                                % (operation, completed, len(writes)))
        return status

    def register_read(self, reads: Sequence[Tuple[int, int]]) -> List[int]:
        # The only reply without a status block (§3.5).
        reply = self.transact(p.register_read_message(reads), p.REGISTER_READ_REPLY_LENGTH,
                              SHORT_WAIT_S)
        return p.parse_register_read_reply(reply, len(reads))

    def raise_for_error(self, status: StatusBlock, operation: str,
                        tolerate: Sequence[int] = (), partial: bytes = b'') -> None:
        code = status.error
        if code == t.ERR_SUCCESS or code in tolerate:
            return
        if code == t.ERR_STOPPED and self.host_stopped:
            # Our own stop request ended it: from the caller's side, a timeout.
            raise GpibTimeout('%s: no reply within the host wait' % operation, partial, code)
        exc = p.error_for_code(code, operation)
        if isinstance(exc, GpibTimeout):
            exc.partial = partial
        raise exc

    def reply_wait_s(self, code: int) -> float:
        """The host wait for the reply to one instruction sent with timeout ``code`` (§7.2).

        The longest expiry either timed adapter showed under that code plus
        two seconds (``protocol.host_wait_s``, §7.3), so the adapter always
        gives up first and says so in its reply; this controller's infinite
        wait for the disabled code. Every message sent here carries one
        timed instruction, so no expiries are summed.
        """
        return p.host_wait_s(code, self.infinite_wait_s)

    def transfer_wait_s(self, code: int, byte_count: int) -> float:
        """The host wait for a transfer of ``byte_count`` bytes that the bus paces.

        The reply wait for ``code``, plus the time the bytes themselves take
        at ``BUS_MIN_RATE_BPS`` (see there for why, now that the code is
        known to bound the whole instruction). Used for the reply to one framed 0x0a, which comes
        only when that instruction is over; the OUT of a 0x0d message, and its
        reply for the part the adapter buffers; the raw IN of a 0x0b; the
        raw OUT of a 0x0e and its reply.
        """
        return self.reply_wait_s(code) + byte_count / BUS_MIN_RATE_BPS

    def resync(self) -> None:
        """§8.2: stop whatever is in flight, drain one stale reply, re-attach later.

        Raises nothing but ``TransportGone``: an adapter that has left the
        bus has no pipe to bring back into step, and the controller must
        hear of it at once.
        """
        self.resync_pending = True
        if self.drained:
            return  # nested guards report the same fault; one drain per fault
        self.drained = True
        logger.warning('%s: reply out of step; stopping and draining the bulk pipe',
                       self.model.name)
        try:
            self.control(t.STOP_REQUEST)
            self.transport.bulk_in(
                p.read_reply_buffer_size(p.MAX_TRANSFER_BYTES, self.transport.max_packet_size),
                int(DRAIN_WAIT_S * 1000))
        except TransportGone:
            raise
        except TransportTimeout:
            logger.debug('nothing to drain')
        except TransportError as exc:
            logger.debug('drain after a malformed reply failed: %s', exc)
        if self.raw:
            # Data of an interrupted 0x0b, or the zero-length packet that ends
            # a full-length one, may still sit on the alternate IN (§10.1.4).
            try:
                self.transport.bulk_in_raw(
                    p.raw_read_buffer_size(p.MAX_RAW_TRANSFER_BYTES, self.transport.max_packet_size_raw),
                    int(DRAIN_WAIT_S * 1000))
            except TransportGone:
                raise
            except TransportTimeout:
                logger.debug('nothing to drain on the alternate endpoint')
            except TransportError as exc:
                logger.debug('alternate-endpoint drain failed: %s', exc)

    def clear_halts_after_fault(self) -> None:
        """Reset the bulk pipes before a re-attach, so a halt does not outlive the fault.

        A halted alternate OUT fails every later 0x0e until it is cleared,
        and the attach sequence does not touch it. The OUT pair goes first
        in NI's order (§10.6.5), then the IN pair; no capture shows a reset
        of an IN pipe, or any reset outside the refused write. Only here:
        the first attach of a healthy adapter is bench-proven as it is and
        stays without. A reset that cannot be done is logged and the attach
        goes ahead; if the pipe is still halted the attach fails and is
        retried by the next operation.
        """
        model = self.model
        alternate = model.raw_endpoints
        for endpoint in (model.endpoint_out_raw if alternate else None, model.endpoint_out,
                         model.endpoint_in, model.endpoint_in_raw if alternate else None):
            if endpoint is not None:
                self._clear_halt(endpoint)

    def _clear_halt(self, endpoint: int) -> bool:
        """Reset one pipe; False, with a log line, when that could not be done.

        Raises nothing but ``TransportGone``: every caller is already
        reporting or recovering from another failure, which a transport
        without ``clear_halt``, or one whose reset fails, must not replace;
        an adapter that has left the bus ends the recovery instead.
        """
        try:
            self.transport.clear_halt(endpoint)
        except TransportGone:
            raise
        except Exception as exc:  # noqa: BLE001 -- see the docstring
            logger.warning('%s: clearing the halt on endpoint 0x%02x failed: %s: %s',
                           self.model.name, endpoint, type(exc).__name__, exc)
            return False
        return True

    def reset_out_pipes(self) -> None:
        """Clear the halt a refused 0x0e leaves on the alternate OUT, then reset the primary OUT.

        NI's order (§10.6.5). The primary OUT had reported no error and why
        NI resets it is not established; it is followed because the next
        operation was then seen to work without anything else. If a reset
        fails the pipes cannot be trusted, and the next operation re-attaches.
        """
        for endpoint in (self.model.endpoint_out_raw, self.model.endpoint_out):
            if endpoint is not None and not self._clear_halt(endpoint):
                self.resync_pending = True

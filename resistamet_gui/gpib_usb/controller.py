"""One NI GPIB-USB adapter, driven as controller-in-charge.

``protocol`` knows what the bytes look like; ``transport`` moves them. This
module owns the order things happen in: the attach sequence (§2.8), the
addressing before every transfer (§6), the go-to-standby between command
bytes and a read (the ATN rule at the top of §5), 16-byte command chunks
(§5.3), and turning a nonzero error code into an exception in exactly one
place. Everything is synchronous and serialised behind one re-entrant lock;
there are no background threads. Sequences built from these primitives
(device clear, serial poll, the presence probe) live in ``device_ops``.

Faults: a malformed reply or a USB error means the bulk pipes may be out of
step (§8.2). The offending operation raises, the adapter is sent a stop
request and its pipe drained, and the next operation re-runs the attach
sequence before doing anything else.

The interrupt endpoint is not used (§2.5 calls it optional and operation
without it reliable), so attach skips the interrupt-monitor-mask steps 4 and
6 of §2.8 and ``status()`` polls the control endpoint instead.

Size: this module runs a little over the 400-line guideline even with the
device-level sequences moved out. What remains is the attach sequence, the
three transfer primitives and the exchange/fault machinery they share; the
fault rule is only correct if it wraps every exchange, so it stays with them.
"""
import logging
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

from . import protocol as p
from . import tables as t
from .protocol import AdapterNotReady, GpibError, GpibTimeout, ProtocolError, StatusBlock
from .transport import Transport, TransportError, TransportTimeout

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
#: spec gap: with the device timeout disabled (code 0xf0) §7.2 leaves the
#: host wait to the application. Ten minutes; on expiry the operation is
#: stopped (§5.11) and reported as a timeout.
DEFAULT_INFINITE_WAIT_S = 600.0

_LISTEN = 'listen'
_TALK = 'talk'


class Controller:
    """Sequencing rules for one adapter over one ``Transport``."""

    def __init__(self, transport: Transport, product_id: int, *,
                 own_address: int = 0, t1_ns: int = 2000,
                 infinite_wait_s: float = DEFAULT_INFINITE_WAIT_S,
                 sleep: Callable[[float], None] = time.sleep) -> None:
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
        #: From the serial-number query (or the USB-B register read).
        self.serial_number: Optional[int] = None

    @property
    def model(self) -> t.Model:
        return self._model

    @property
    def own_address(self) -> int:
        return self._own_address

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
            self._register_write(                                   # step 5
                t.register_init_writes(self._own_address, system_controller, self._t1_ns),
                'register initialisation')
            # Step 6 (monitor mask 0x10ff) skipped for the same reason.
            if system_controller:                                   # step 7
                self._interface_clear()
                self._remote_enable(True)
                # Error 5 here just means nothing is on the bus yet (§8.12).
                self._status_exchange(p.take_control_message(True), SHORT_WAIT_S,
                                      'take control', tolerate=(t.ERR_NO_ACCEPTOR,))
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
        """§2.9: chip reset, the device-level register, release the interface."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
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
              readdress: bool = True) -> int:
        """Address ``pad`` to listen, then 0x0d in chunks of at most 0xffff (§5.1)."""
        with self._guard():
            self._ensure_attached()
            if not data:
                return 0
            code, limit = p.effective_timeout(timeout_s)
            wait = p.host_wait_s(limit, self._infinite_wait_s)
            self._address(_LISTEN, pad, sad, code, wait, readdress)
            written = 0
            for start in range(0, len(data), p.MAX_TRANSFER_BYTES):
                chunk = data[start:start + p.MAX_TRANSFER_BYTES]
                last = start + len(chunk) == len(data)
                status, _ = self._exchange(p.write_message(chunk, code, send_eoi and last),
                                           p.STATUS_REPLY_LENGTH, wait, 'write')
                written += status.transferred(len(chunk))
            return written

    def read(self, pad: int, *, sad: Optional[int] = None, max_bytes: int,
             timeout_s: Optional[float], eos: Optional[int] = None,
             eos_8bit: bool = False, readdress: bool = True) -> Tuple[bytes, bool]:
        """Address ``pad`` to talk, go to standby, then one 0x0a (§5.2).

        Returns the data and whether END (EOI, or the EOS character when
        ``eos`` is given) ended it. False means the count was reached. A
        device-side timeout raises ``GpibTimeout`` carrying the partial data.
        """
        with self._guard():
            self._ensure_attached()
            if max_bytes < 1:
                return b'', False
            code, limit = p.effective_timeout(timeout_s)
            wait = p.host_wait_s(limit, self._infinite_wait_s)
            self._address(_TALK, pad, sad, code, wait, readdress)
            # ATN rule (§5): a 0x06 between the addressing 0x0c and the 0x0a.
            self._go_to_standby()
            return self._read_instruction(min(max_bytes, p.MAX_TRANSFER_BYTES), code, wait,
                                          eos, eos_8bit, 'read')

    def read_raw(self, max_bytes: int, timeout_s: Optional[float],
                 eos: Optional[int] = None, eos_8bit: bool = False) -> Tuple[bytes, bool]:
        """One 0x0a with the bus as it stands: no addressing, no standby.

        For callers that addressed the bus themselves (a serial poll sends its
        own SPE sequence and standby). ATN must already be false, else error 2.
        """
        with self._guard():
            self._ensure_attached()
            if max_bytes < 1:
                return b'', False
            code, limit = p.effective_timeout(timeout_s)
            wait = p.host_wait_s(limit, self._infinite_wait_s)
            return self._read_instruction(min(max_bytes, p.MAX_TRANSFER_BYTES), code, wait,
                                          eos, eos_8bit, 'read')

    def _read_instruction(self, count: int, code: int, wait_s: float, eos: Optional[int],
                          eos_8bit: bool, operation: str) -> Tuple[bytes, bool]:
        buffer = p.read_reply_buffer_size(count, self._transport.max_packet_size)
        _, reply = self._exchange(p.read_message(count, code, eos, eos_8bit), buffer, wait_s,
                                  operation, tolerate=(t.ERR_TIMEOUT, t.ERR_STOPPED))
        parsed = p.parse_read_reply(reply, count)
        self._raise_for_error(parsed.status, operation, partial=parsed.data)
        return parsed.data, parsed.end

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
        try:
            return self._transport.bulk_in(reply_length, int(wait_s * 1000))
        except TransportTimeout:
            # §5.11: the device still owes the reply; make it finish now.
            self._host_stopped = True
            self._control(t.STOP_REQUEST)
            try:
                return self._transport.bulk_in(reply_length, int(RECOVERY_WAIT_S * 1000))
            except TransportTimeout as exc:
                raise ProtocolError('adapter did not answer after a stop request') from exc

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

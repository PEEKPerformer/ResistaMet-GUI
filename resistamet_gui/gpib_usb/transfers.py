"""The data transfers behind ``Controller.write`` and ``Controller.read``.

Framed writes and reads (0x0d, 0x0a: §5.1, §5.2) and the raw ones (0x0e,
0x0b: §10.1, §10.5), chunked to what one instruction carries, with their
host waits and the recovery particular to each. Which instruction a transfer
takes, and why the raw ones are off unless asked for, is in the
``controller`` module docstring. ``Controller`` inherits these from
``_TransferMixin``, which has no state of its own; the exchange primitives
it calls are in ``link``. The constants are re-exported by ``controller``.
"""
import logging
from typing import List, NoReturn, Optional, Sequence, Tuple

from . import protocol as p
from . import tables as t
from .link import DRAIN_WAIT_S, RECOVERY_WAIT_S, SHORT_WAIT_S
from .protocol import GpibTimeout, ProtocolError, StatusBlock
from .transport import TransportError, TransportGone, TransportTimeout

#: The controller's logger: these are its methods, and they log as it.
logger = logging.getLogger(__name__.rpartition('.')[0] + '.controller')

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
#: The alternate-IN read of a 0x0b is issued in slices of this length, with a
#: look at the primary IN between two slices (``RAW_REPLY_POLL_S``): a reply
#: that is already there means the instruction is over, whatever the data
#: transfer is doing. §10.6.7 leaves open whether the adapter completes that
#: transfer for read errors other than the timeout; if it does not, one long
#: read would sit out the whole transfer wait with the error reply queued.
RAW_READ_SLICE_S = 1.0
RAW_REPLY_POLL_S = 0.01
#: A device address, (primary, secondary or None), for NI's messages that
#: address the instrument themselves.
Address = Tuple[int, Optional[int]]
#: What the adapter buffers of a framed 0x0d message: the hung adapter of
#: §8.17 took about 4 KB on the primary OUT before it stopped accepting. The
#: OUT of a framed write completes once the adapter holds the message, so up
#: to this much has still to reach the instrument before the reply can come.
ADAPTER_OUT_BUFFER_BYTES = 4096


class _TransferMixin:
    """The write and read paths of ``Controller`` (see the module docstring)."""

    def _refuse_after_deadline(self, deadline: Optional[float], operation: str, done: int, asked: int,
                               partial: bytes = b'') -> None:
        """Before a later piece of a transfer: once ``deadline`` has passed, end with a timeout.

        The piece is not started, and the transfer ends with ``GpibTimeout``
        carrying what it has, as NI's one instruction ends at its code's
        expiry (§7.1, §10.10.2). A piece that does start carries the
        transfer's own code, never a shorter one for the time left: a framed
        0x0a cut off by its code while data is arriving has never been seen
        (§7.3, §10.10.2), 0xf5-0xf8 were never timed on unit 01CEE482, and
        that unit has wedged under another framed condition nobody had seen
        (§11.2). The last piece may therefore run up to one piece past the
        deadline. None (no timeout) never refuses.
        """
        if deadline is not None and self._clock() >= deadline:
            raise GpibTimeout('%s: the timeout ran out after %d of %d bytes' % (operation, done, asked), partial)

    def _ni_session(self, address: Address, code: int) -> None:
        """NI's bank-2 session configuration before a raw instruction to ``address`` (§10.2.4).

        NI writes it at a session's open -- 0x04 := 1, 0x05 := PAD, 0x06 :=
        the secondary byte, 0x07 := the timeout code -- and again whenever
        the code changes; every capture of 0x0b and 0x0e had it written
        first, and bench unit 01CEE482, sent a 0x0b without it, ended the
        instruction at 20.0 s whatever its code (§11.2). The sequence is
        NI's: the first raw instruction to an address sends the 32-byte
        open form; a later one with another code the 12-byte bank-2 0x03
        write and then the 28-byte update (§10.10.1); one to another address
        first NI's close of the session on the old one (0x04 := 0, §10.3.3),
        as NI closed GPIB0::5 before opening GPIB0::24 in the same process
        (§10.6.7), then the open form; one that repeats both, nothing.
        Whether any of it is needed is not established (§8.15).
        """
        pad, sad = address
        current = self._ni_session_state
        if current == (pad, sad, code):
            return
        steps = []
        if current is not None and current[:2] != (pad, sad):
            steps.append((p.ni_session_close_message(), 'bank-2 session close', (1, 1)))
        if current is None or current[:2] != (pad, sad):
            steps.append((p.ni_session_open_message(pad, sad, code), 'bank-2 session configuration', (1, 4)))
        else:
            steps.append((p.ni_session_mark_message(), 'bank-2 session mark', (1,)))
            steps.append((p.ni_session_update_message(pad, sad, code), 'bank-2 timeout update', (1, 4)))
        for message, operation, writes in steps:
            reply = self._link.transact(message, p.SMALL_REPLY_BUFFER, SHORT_WAIT_S)
            self._check_ni_reply(reply, operation, writes, strict=True)
        self._ni_session_state = (pad, sad, code)

    def _check_ni_reply(self, reply: bytes, operation: str, writes: Sequence[int], strict: bool) -> None:
        """The blocks of a reply to one of NI's messages besides the data instruction's own (§10.2.1).

        An addressing 0x0c that failed raises its error; register writes
        that did not all complete (§8.14) are a ``ProtocolError`` when
        ``strict`` and a warning otherwise, since the data instruction of
        the same message has already run and its result is kept.
        """
        blocks = p.split_reply_blocks(reply)
        for block_id, block in blocks:
            if block_id == p.OP_COMMAND:
                self._link.raise_for_error(p.parse_status_block(block), operation)
        done = tuple(block[8] for block_id, block in blocks if block_id == p.OP_REGISTER_WRITE)
        if done != tuple(writes):
            if strict:
                raise ProtocolError('%s: register writes completed %r of %r: %s'
                                    % (operation, done, tuple(writes), reply.hex()))
            logger.warning('%s: %s: register writes completed %r of %r', self._link.model.name,
                           operation, done, tuple(writes))

    def _write_bytes(self, data: bytes, code: int, send_eoi: bool, eos_char: Optional[int],
                     deadline: Optional[float], address: Optional[Address] = None) -> int:
        """Write instructions of at most 0xffff bytes each, EOI only with the last (§5.1).

        Framed or raw is decided per chunk, so a short tail after a raw
        chunk goes framed: each write instruction is complete in itself, its
        data with it. (The read loop decides once per call instead.) Every
        chunk carries ``code``, and none starts once ``deadline`` has passed
        (``_refuse_after_deadline``): the timeout bounds the write as a whole.
        A write that times out raises ``GpibTimeout`` whose ``partial`` is
        the data of the chunks that crossed before it.
        With ``address`` a raw chunk is NI's message, which addresses the
        instrument itself (``_raw_write_instruction``); a framed tail after
        it finds the instrument still addressed to listen.
        """
        # Both instructions carry at most 0xffff bytes, so one chunk size serves.
        step = min(p.MAX_TRANSFER_BYTES, p.MAX_RAW_TRANSFER_BYTES)
        written = 0
        for start in range(0, len(data), step):
            if start:
                self._refuse_after_deadline(deadline, 'write', written, len(data), data[:written])
            chunk = data[start:start + step]
            eoi = send_eoi and start + len(chunk) == len(data)
            if self._link.raw and len(chunk) >= RAW_WRITE_MIN_BYTES:
                try:
                    written += self._raw_write_instruction(chunk, code, eoi, eos_char, address)
                except GpibTimeout as exc:
                    exc.partial = data[:written]  # the chunks before this one crossed
                    raise
            else:
                # The data rides inside the message, and the adapter takes the
                # message only as fast as the instrument takes the data: the
                # tail of NI's 2080-byte message needed 103 ms (§10.5.2, §7.2).
                # So the OUT follows the device timeout and the byte count, not
                # the short wait. Once it completes all but the adapter's own
                # buffer is on the bus, and the reply waits for that remainder.
                buffered = min(len(chunk), ADAPTER_OUT_BUFFER_BYTES)
                try:
                    status, _ = self._link.exchange(p.write_message(chunk, code, eoi), p.STATUS_REPLY_LENGTH,
                                                    self._link.transfer_wait_s(code, buffered), 'write',
                                                    paced_wait_s=self._link.transfer_wait_s(code, len(chunk)))
                except GpibTimeout as exc:
                    exc.partial = data[:written]  # the chunks before this one crossed
                    raise
                written += status.transferred(len(chunk))
        return written

    def _raw_write_instruction(self, chunk: bytes, code: int, send_eoi: bool,
                               eos_char: Optional[int], address: Optional[Address] = None) -> int:
        """One 0x0e (§10.5.2): the header on the primary OUT, the bytes raw on the alternate OUT.

        With ``address`` (an instrument session) the header is the message
        NI sends, byte for byte: the status snapshot, the addressing 0x0c
        with NI's code 0xfd, the 0x0e, the bank-2 mark, after NI's bank-2
        session configuration (``_ni_session``); ``e`` is the session's
        termination character, which NI sends whether or not the compare is
        on (§10.5.1, §10.5.2). The wait allows for both timed blocks (§7.2).
        Without, for a caller that addressed the bus itself, it is the bare
        0x0e, which no capture shows.

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
        timeout, or an adapter found gone from the bus (§10.11), is taken
        for that refusal until the reply says otherwise: how libusb on
        macOS reports the STALL has not been seen, and if an
        unrecognised error skipped this path the reply would stay queued
        and the alternate OUT halted for every later 0x0e.
        """
        if address is None:
            message = p.write_raw_message(len(chunk), code, send_eoi, eos_char)
            wait_s = self._link.transfer_wait_s(code, len(chunk))
        else:
            self._ni_session(address, code)
            message = p.ni_write_raw_message(self._own_address, address[0], address[1], len(chunk), code,
                                             send_eoi, eos_char)
            wait_s = self._link.transfer_wait_s(code, len(chunk), also=(t.NI_ADDRESSING_CODE,))
        self._link.host_stopped = False
        self._link.send(message)
        try:
            accepted = self._link.transport.bulk_out_raw(chunk, int(wait_s * 1000))
        except TransportTimeout:
            accepted = 0
        except TransportGone:
            raise  # no adapter to read a reply from or reset a pipe on
        except TransportError as refusal:
            gone = self._link.gone_instead(refusal)  # macOS: the error does not say (§10.11)
            if gone is not None:
                raise gone from refusal
            self._refused_raw_write(refusal)
        stranded = accepted < len(chunk)
        if stranded:
            # The host wait ran out with the instrument not accepting: the
            # transport reports bytes moved as a short count, none as a
            # timeout. The device is still mid-instruction, and §5.11 makes it
            # finish so the reply can say how much reached the bus. NI was not
            # observed using the stop request (§10.8); none of its captures
            # has a host wait expiring, which is the one case it is kept for.
            self._link.stop_device()
        try:
            if stranded:
                reply = self._link.reply_after_stop(p.SMALL_REPLY_BUFFER)
            else:
                reply = self._link.reply_or_stop(p.SMALL_REPLY_BUFFER, wait_s)
        except TransportGone:
            raise
        except BaseException:
            if stranded:
                self._abandon_raw_out()
            raise
        if stranded:
            self._abandon_raw_out()
        parsed = p.parse_raw_write_reply(reply)
        if address is not None:
            self._check_ni_reply(reply, 'address to listen', (1,), strict=False)
        self._link.raise_for_error(parsed.status, 'write')
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
        self._link.reset_out_pipes()
        self._link.resync_pending = True

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
                       self._link.model.name, type(refusal).__name__, type(refusal.__cause__).__name__,
                       getattr(refusal, 'errno', None), getattr(refusal, 'backend_code', None), refusal)
        status: Optional[StatusBlock] = None
        try:
            status = p.parse_raw_write_reply(self._link.reply_or_stop(p.SMALL_REPLY_BUFFER, SHORT_WAIT_S)).status
        except ProtocolError as exc:
            logger.warning('%s: no usable reply after the refused data: %s', self._link.model.name, exc)
            self._link.resync()
        except TransportGone:
            raise
        except TransportError as exc:
            self._link.end_if_gone(exc)
            logger.warning('%s: reading the reply after the refused data failed: %s', self._link.model.name, exc)
        self._link.reset_out_pipes()
        if status is not None:
            if self._link.host_stopped or status.error != t.ERR_NO_LISTENER:
                # Only the refusal NI's captures show -- error 8, ended by the
                # adapter -- is known to leave the adapter ready for the next
                # operation. Ended by our stop request, or for another reason,
                # the alternate OUT may hold bytes that would lead the data of
                # the next 0x0e, as after a stranded write.
                self._link.resync_pending = True
            self._link.raise_for_error(status, 'write')
        raise refusal

    def _reads_raw(self, max_bytes: int) -> bool:
        """Whether a read of ``max_bytes`` takes 0x0b: the requested count decides, once (§10.1.1)."""
        return self._link.raw and max_bytes >= RAW_READ_MIN_BYTES

    def _read_bytes(self, max_bytes: int, code: int, eos: Optional[int],
                    eos_8bit: bool, operation: str, deadline: Optional[float],
                    address: Optional[Address] = None, termchar: Optional[int] = None) -> Tuple[bytes, bool]:
        """Read instructions until END, the count, or a short result; framed or raw by size.

        One instruction carries at most ``FRAMED_READ_MAX_BYTES`` on the
        framed path and 0xffff on the raw one, so a larger request loops.
        The instrument stays addressed between pieces: addressing changes
        only by command bytes (§6), the 0x0a reply reports ATN still false
        (§10.1.5), and an instrument that gave up fewer bytes than it holds
        keeps the rest for the next read (§10.1.7, where NI's second
        ``viRead`` re-addressed first and that was harmless, not needed).
        So the 0x0c and the 0x06 go once per call and each piece costs one
        round trip.

        The read is bounded by its timeout as a whole, as NI's is: NI sends
        one instruction whose code bounds it from its start, and a read
        still receiving data ends at the code's expiry with the bytes so
        far and error 0x0a (§7.1, §10.10.2). Here ``deadline`` is that
        expiry from the start of the call (``Controller._deadline``); every
        piece carries ``code``, with its host wait (``transfer_wait_s`` of
        one piece), and once the deadline has passed no piece starts
        (``_refuse_after_deadline``). Either way the read ends with ``GpibTimeout``
        carrying everything read so far as its partial (§5.2: the partial
        data of a timed-out read is valid), which the pyvisa-py session
        returns with VI_ERROR_TMO as pyvisa-py's own sessions return a
        timed-out read's bytes. Before, each piece took the session's code
        afresh, and a read of many pieces could run for many expiries.

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
        raw = self._reads_raw(max_bytes)
        step = p.MAX_TRANSFER_BYTES if raw else FRAMED_READ_MAX_BYTES
        while remaining > 0:
            if chunks:
                read = b''.join(chunks)
                self._refuse_after_deadline(deadline, operation, len(read), max_bytes, read)
            count = min(remaining, step)
            try:
                if raw:
                    data, end = self._raw_read_instruction(count, code, eos, eos_8bit, operation, address, termchar)
                else:
                    data, end = self._read_instruction(count, code, self._link.transfer_wait_s(code, count), eos,
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
        buffer = p.read_reply_buffer_size(count, self._link.transport.max_packet_size)
        _, reply = self._link.exchange(p.read_message(count, code, eos, eos_8bit), buffer, wait_s,
                                       operation, tolerate=(t.ERR_TIMEOUT, t.ERR_STOPPED))
        parsed = p.parse_read_reply(reply, count)
        self._link.raise_for_error(parsed.status, operation, partial=parsed.data)
        return parsed.data, parsed.end

    def _raw_read_instruction(self, count: int, code: int, eos: Optional[int], eos_8bit: bool, operation: str,
                              address: Optional[Address] = None, termchar: Optional[int] = None) -> Tuple[bytes, bool]:
        """One 0x0b (§10.1.2-10.1.3): the data arrives raw on the alternate bulk IN, the status on the primary.

        With ``address`` (an instrument session) the message is the one NI
        sends, byte for byte (§10.1.2): the status snapshot, the addressing
        0x0c with NI's code 0xfd and no 0x06 behind it, the 0x0b, the
        clear-END write and the bank-2 mark, all in one, after NI's bank-2
        session configuration (``_ni_session``). With the compare off, ``e``
        is ``termchar``, the session's character, as NI sends it (§10.1.6):
        ``m e`` = ``00 0a``. That form is an open combination here: NI sent
        it under its own AUXRA 0x99 and never got error 4, but whether
        AUXRA 0x81, this driver's, rejects it is not established (§8.3,
        §11.1). It is left as NI sends it; the application reads with the
        compare on (``14 0a``), NI's form either way.
        Every piece of a split read is that whole message, as NI's every
        ``viRead`` is (§10.1.4). The wait allows for both timed blocks
        (§7.2). Without ``address``, for a caller that addressed the bus
        itself, the message is the 0x0b and the clear-END write, as before.
        """
        if address is None:
            message = p.read_raw_message(count, code, eos, eos_8bit)
            wait_s = self._link.transfer_wait_s(code, count)
        else:
            self._ni_session(address, code)
            message = p.ni_read_raw_message(self._own_address, address[0], address[1], count, code,
                                            eos, eos_8bit, termchar)
            wait_s = self._link.transfer_wait_s(code, count, also=(t.NI_ADDRESSING_CODE,))
        buffer = p.raw_read_buffer_size(count, self._link.transport.max_packet_size_raw)
        data, reply = self._raw_read_transact(message, buffer, wait_s)
        if address is not None:
            self._check_ni_reply(reply, 'address to talk', (1, 1), strict=False)
        parsed = p.parse_raw_read_reply(reply, count, data)
        self._link.raise_for_error(parsed.status, operation, partial=parsed.data)
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
        self._link.host_stopped = False
        self._link.send(message)
        data = b''
        remaining_ms = max(1, int(wait_s * 1000))
        slice_limit_ms = max(1, int(RAW_READ_SLICE_S * 1000))
        while remaining_ms > 0:
            slice_ms = min(slice_limit_ms, remaining_ms)
            try:
                data += self._link.transport.bulk_in_raw(data_buffer - len(data), slice_ms)
            except TransportTimeout as expired:
                # What the transport had received before the slice ran out (it
                # reports a partial transfer this way) stays; the next read
                # continues the same transfer.
                data += expired.partial
            else:
                # The transfer ended by itself; the reply follows within a millisecond.
                return data, self._link.reply_or_stop(p.SMALL_REPLY_BUFFER, SHORT_WAIT_S)
            remaining_ms -= slice_ms
            if remaining_ms <= 0:
                break
            try:
                reply = self._link.transport.bulk_in(p.SMALL_REPLY_BUFFER, max(1, int(RAW_REPLY_POLL_S * 1000)))
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
                data += self._link.transport.bulk_in_raw(data_buffer - len(data), int(DRAIN_WAIT_S * 1000))
            except TransportTimeout as nothing_more:
                data += nothing_more.partial
            return data, reply
        # §5.11: the host wait is over and the device still owes both transfers; make it finish now.
        self._link.stop_device()
        try:
            data += self._link.transport.bulk_in_raw(data_buffer - len(data), int(RECOVERY_WAIT_S * 1000))
        except TransportTimeout as still:
            # Whether a stopped 0x0b completes its data transfer is not
            # established (a timed-out one does, with zero bytes). The
            # reply's count decides whether anything was lost.
            data += still.partial
        return data, self._link.reply_after_stop(p.SMALL_REPLY_BUFFER)

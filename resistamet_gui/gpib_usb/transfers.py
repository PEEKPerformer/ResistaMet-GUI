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
from typing import List, NoReturn, Optional, Tuple

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
#: The shortest timeout a later piece of a transfer is given when little of
#: its deadline is left: a millisecond, VISA's own unit, which goes out as
#: 0xf5, the shortest code captured (§7.1, §10.10.1). Below it are the codes
#: 0xf1-0xf4, which no capture shows.
PIECE_TIMEOUT_MIN_S = 1e-3
#: What the adapter buffers of a framed 0x0d message: the hung adapter of
#: §8.17 took about 4 KB on the primary OUT before it stopped accepting. The
#: OUT of a framed write completes once the adapter holds the message, so up
#: to this much has still to reach the instrument before the reply can come.
ADAPTER_OUT_BUFFER_BYTES = 4096


class _TransferMixin:
    """The write and read paths of ``Controller`` (see the module docstring)."""

    def _code_for_the_rest(self, deadline: Optional[float], operation: str, done: int, asked: int,
                           partial: bytes = b'') -> int:
        """The timeout code for a later piece of a transfer: the time left before ``deadline``.

        Rounded as every timeout is (``protocol.timeout_code``), so the piece
        does not end before the deadline either. With the deadline passed no
        piece is started, and the transfer ends with a timeout carrying what
        it has, as NI's one instruction does at its code's expiry (§7.1,
        §10.10.2). None (no timeout) keeps the disabled code.
        """
        if deadline is None:
            return t.TIMEOUT_DISABLED_CODE
        left = deadline - self._clock()
        if left <= 0:
            raise GpibTimeout('%s: the timeout ran out after %d of %d bytes' % (operation, done, asked), partial)
        return p.timeout_code(max(left, PIECE_TIMEOUT_MIN_S))

    def _write_bytes(self, data: bytes, code: int, send_eoi: bool, eos_char: Optional[int],
                     deadline: Optional[float]) -> int:
        """Write instructions of at most 0xffff bytes each, EOI only with the last (§5.1).

        Framed or raw is decided per chunk, so a short tail after a raw
        chunk goes framed: each write instruction is complete in itself, its
        data with it. (The read loop decides once per call instead.) The
        first chunk carries ``code``; a later one the code for what is left
        of ``deadline``, and none starts once it has passed
        (``_code_for_the_rest``): the timeout bounds the write as a whole.
        """
        # Both instructions carry at most 0xffff bytes, so one chunk size serves.
        step = min(p.MAX_TRANSFER_BYTES, p.MAX_RAW_TRANSFER_BYTES)
        written = 0
        for start in range(0, len(data), step):
            if start:
                code = self._code_for_the_rest(deadline, 'write', written, len(data))
            chunk = data[start:start + step]
            eoi = send_eoi and start + len(chunk) == len(data)
            if self._link.raw and len(chunk) >= RAW_WRITE_MIN_BYTES:
                written += self._raw_write_instruction(chunk, code, eoi, eos_char)
            else:
                # The data rides inside the message, and the adapter takes the
                # message only as fast as the instrument takes the data: the
                # tail of NI's 2080-byte message needed 103 ms (§10.5.2, §7.2).
                # So the OUT follows the device timeout and the byte count, not
                # the short wait. Once it completes all but the adapter's own
                # buffer is on the bus, and the reply waits for that remainder.
                buffered = min(len(chunk), ADAPTER_OUT_BUFFER_BYTES)
                status, _ = self._link.exchange(p.write_message(chunk, code, eoi), p.STATUS_REPLY_LENGTH,
                                                self._link.transfer_wait_s(code, buffered), 'write',
                                                paced_wait_s=self._link.transfer_wait_s(code, len(chunk)))
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
        wait_s = self._link.transfer_wait_s(code, len(chunk))
        self._link.host_stopped = False
        self._link.send(message)
        try:
            accepted = self._link.transport.bulk_out_raw(chunk, int(wait_s * 1000))
        except TransportTimeout:
            accepted = 0
        except TransportGone:
            raise  # no adapter to read a reply from or reset a pipe on
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

    def _read_bytes(self, max_bytes: int, code: int, eos: Optional[int],
                    eos_8bit: bool, operation: str, deadline: Optional[float]) -> Tuple[bytes, bool]:
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
        far and error 0x0a (§7.1, §10.10.2). Here the first piece carries
        ``code`` and every later one the code for what is left of
        ``deadline`` (``_code_for_the_rest``), with that code's host wait
        (``transfer_wait_s`` of one piece); once the deadline has passed no
        piece starts. Either way the read ends with ``GpibTimeout``
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
        raw = self._link.raw and max_bytes >= RAW_READ_MIN_BYTES
        step = p.MAX_TRANSFER_BYTES if raw else FRAMED_READ_MAX_BYTES
        while remaining > 0:
            if chunks:
                read = b''.join(chunks)
                code = self._code_for_the_rest(deadline, operation, len(read), max_bytes, read)
            count = min(remaining, step)
            try:
                if raw:
                    data, end = self._raw_read_instruction(count, code, eos, eos_8bit, operation)
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

    def _raw_read_instruction(self, count: int, code: int, eos: Optional[int],
                              eos_8bit: bool, operation: str) -> Tuple[bytes, bool]:
        """One 0x0b (§10.1.2-10.1.3): the data arrives raw on the alternate bulk IN, the status on the primary."""
        message = p.read_raw_message(count, code, eos, eos_8bit)
        buffer = p.raw_read_buffer_size(count, self._link.transport.max_packet_size_raw)
        wait_s = self._link.transfer_wait_s(code, count)
        data, reply = self._raw_read_transact(message, buffer, wait_s)
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

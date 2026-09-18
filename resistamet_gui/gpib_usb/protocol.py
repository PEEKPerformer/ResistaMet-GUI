"""Wire format of the NI GPIB-USB adapters: message builders and reply parsers.

Everything the adapter is told and everything it answers is a byte string
with a fixed shape (``docs/design/ni_usb_gpib_protocol.md`` §3-4, §7). This
module builds and parses those byte strings and nothing else: no USB, no
state, no timing. The constants it encodes (ids, register values, command
bytes, timeout rows) live in ``tables``. That keeps every rule about counts,
padding, terminators and status bits checkable byte-for-byte against the
worked examples in the specification (§3.6) without hardware.

Section numbers in comments refer to that specification.

Bench notes (GPIB-USB-HS 01CEE482, 2026-09-18): every host-to-device
message here was accepted as built. Replies differed from the
specification in one place: a read reply ends in a 16-byte trailer (status,
ADR1, last-block count, pad, termination) with no embedded 0x09 status
block; ``parse_read_reply`` follows the device and tolerates the longer
form. Small reads arrive in 0x36 blocks, a 256-byte read in 0x37 blocks,
and the filler after the valid bytes is stale data.
"""
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from . import tables as t

# --------------------------------------------------------------------------
# §3.2 opcodes and §3.5 reply block ids
# --------------------------------------------------------------------------

OP_TAKE_CONTROL = 0x01
OP_TERMINATION = 0x04
OP_GO_TO_STANDBY = 0x06
OP_PARALLEL_POLL = 0x07
OP_REGISTER_READ = 0x08
OP_REGISTER_WRITE = 0x09
OP_READ = 0x0A
OP_COMMAND = 0x0C
OP_WRITE = 0x0D
OP_INTERFACE_CLEAR = 0x0F

BLOCK_REGISTER_VALUES = 0x34   # up to 3 register values
BLOCK_REGISTER_END = 0x35
BLOCK_DATA_15 = 0x36           # id + 15 data bytes
BLOCK_DATA_30 = 0x37           # id + 00 + 30 data bytes
BLOCK_READ_STATUS = 0x38

TERMINATION_BLOCK = b'\x04\x00\x00\x00'
STATUS_BLOCK_LENGTH = 8
STATUS_REPLY_LENGTH = 12
REGISTER_WRITE_REPLY_LENGTH = 16
REGISTER_READ_REPLY_LENGTH = 32
#: Observed on GPIB-USB-HS 01CEE482: status block (8) + ADR1 + last-block
#: count + 2 pad + termination (4). The specification derived 28 bytes with
#: an embedded 0x09 status block that the device does not send.
READ_REPLY_TRAILER_LENGTH = 16
#: Receive buffers are sized for the longer, specification-derived trailer
#: so a firmware that does send the embedded status still fits.
READ_REPLY_TRAILER_MAX = 28

MAX_COMMAND_BYTES = 16       # §5.3, all models
MAX_TRANSFER_BYTES = 0xFFFF  # §5.1, §5.2

WRITE_FLAG_EOI = 0x08        # §5.1 ``f``
EOS_MODE_REOS = 0x04         # §5.2 ``m``: terminate on the EOS character
EOS_MODE_BIN = 0x10          # §5.2 ``m``: compare all 8 bits

#: The two AUXMR writes embedded in every read instruction (§5.2).
READ_EMBEDDED_WRITES: Tuple[Tuple[int, int, int], ...] = (
    (1, 0x0A, 0x51),  # holdoff handshake immediately
    (1, 0x0A, 0x55),  # clear END status
)

# --------------------------------------------------------------------------
# exceptions
# --------------------------------------------------------------------------


class GpibError(Exception):
    """An adapter reported a nonzero error code, or the exchange itself failed."""

    def __init__(self, message: str, code: int = -1) -> None:
        super().__init__(message)
        self.code = code


class GpibTimeout(GpibError):
    """The device timeout expired; ``partial`` holds whatever was transferred."""

    def __init__(self, message: str, partial: bytes = b'', code: int = t.ERR_TIMEOUT) -> None:
        super().__init__(message, code)
        self.partial = partial


class NoListener(GpibError):
    """Codes 5 (nothing on the bus accepted a command) and 8 (no listener addressed)."""


class AdapterNotReady(GpibError):
    """The adapter did not come up, or cannot be driven (firmware missing, not attached)."""


class ProtocolError(GpibError):
    """A reply did not have the shape the specification gives it."""


class NoReply(ProtocolError):
    """A message was accepted but never answered, not even after a stop request."""


def error_for_code(code: int, operation: str) -> GpibError:
    """The exception for a nonzero status-block error code (§4.3)."""
    message = '%s: error %d (%s)' % (operation, code, t.error_label(code))
    if code == t.ERR_TIMEOUT:
        return GpibTimeout(message)
    if code in (t.ERR_NO_ACCEPTOR, t.ERR_NO_LISTENER):
        return NoListener(message, code)
    return GpibError(message, code)


# --------------------------------------------------------------------------
# §3.3 count encoding
# --------------------------------------------------------------------------


def encode_count16(length: int) -> bytes:
    """Two's-complement negative of ``length``, little-endian (§3.3)."""
    if not 1 <= length <= MAX_TRANSFER_BYTES:
        raise ValueError('transfer length %d outside 1..%d' % (length, MAX_TRANSFER_BYTES))
    return ((0x10000 - length) & 0xFFFF).to_bytes(2, 'little')


def encode_count8(length: int) -> int:
    """Two's-complement negative of ``length`` in one byte (§3.3)."""
    if not 1 <= length <= 0xFF:
        raise ValueError('count %d outside 1..255' % length)
    return (0x100 - length) & 0xFF


# --------------------------------------------------------------------------
# §3.1 message assembly
# --------------------------------------------------------------------------


def _pad4(block: bytes) -> bytes:
    return block + b'\x00' * (-len(block) % 4)


def build_message(*blocks: bytes) -> bytes:
    """Pad each instruction block to 4 bytes and append the termination block."""
    return b''.join(_pad4(block) for block in blocks) + TERMINATION_BLOCK


def take_control_message(synchronous: bool = True) -> bytes:
    """§5.4: ``01 s 00 00``."""
    return build_message(bytes((OP_TAKE_CONTROL, 0x01 if synchronous else 0x00, 0, 0)))


def go_to_standby_message() -> bytes:
    """§5.4: ``06 00 00 00``."""
    return build_message(bytes((OP_GO_TO_STANDBY, 0, 0, 0)))


def interface_clear_message() -> bytes:
    """§5.5: ``0f 00 00 00``."""
    return build_message(bytes((OP_INTERFACE_CLEAR, 0, 0, 0)))


def command_message(command_bytes: bytes, timeout_code: int) -> bytes:
    """§5.3: ``0c c 00 t <cmd...>``; at most 16 command bytes."""
    if not 1 <= len(command_bytes) <= MAX_COMMAND_BYTES:
        raise ValueError('%d command bytes; one instruction carries 1..%d'
                         % (len(command_bytes), MAX_COMMAND_BYTES))
    header = bytes((OP_COMMAND, encode_count8(len(command_bytes)), 0x00, timeout_code))
    return build_message(header + command_bytes)


def write_message(data: bytes, timeout_code: int, send_eoi: bool) -> bytes:
    """§5.1: ``0d cl ch t 00 00 f 00 <data...>``."""
    header = (bytes((OP_WRITE,)) + encode_count16(len(data))
              + bytes((timeout_code, 0x00, 0x00, WRITE_FLAG_EOI if send_eoi else 0x00, 0x00)))
    return build_message(header + data)


def read_message(max_bytes: int, timeout_code: int,
                 eos: Optional[int] = None, eos_8bit: bool = False) -> bytes:
    """§5.2: ``0a m e t cl ch 00 00`` plus the embedded two-write block.

    With EOS disabled both ``m`` and ``e`` are zero; anything else there
    earns error 4 from the device.
    """
    if eos is None:
        mode, char = 0x00, 0x00
    else:
        if not 0 <= eos <= 0xFF:
            raise ValueError('EOS character %r is not a byte' % (eos,))
        mode = EOS_MODE_REOS | (EOS_MODE_BIN if eos_8bit else 0x00)
        char = eos
    header = (bytes((OP_READ, mode, char, timeout_code)) + encode_count16(max_bytes)
              + b'\x00\x00')
    return build_message(header, register_write_block(READ_EMBEDDED_WRITES))


def register_write_block(writes: Sequence[Tuple[int, int, int]]) -> bytes:
    """§3.4: ``09 nn 00 | b a v ...`` (unpadded; ``build_message`` pads)."""
    if not 1 <= len(writes) <= 0xFF:
        raise ValueError('%d register writes' % len(writes))
    body = b''.join(bytes((bank, addr, value)) for bank, addr, value in writes)
    return bytes((OP_REGISTER_WRITE, len(writes), 0x00)) + body


def register_write_message(writes: Sequence[Tuple[int, int, int]]) -> bytes:
    return build_message(register_write_block(writes))


def register_read_message(reads: Sequence[Tuple[int, int]]) -> bytes:
    """§3.4: ``08 nn | b a ...``."""
    if not 1 <= len(reads) <= 0xFF:
        raise ValueError('%d register reads' % len(reads))
    body = b''.join(bytes((bank, addr)) for bank, addr in reads)
    return build_message(bytes((OP_REGISTER_READ, len(reads))) + body)


# --------------------------------------------------------------------------
# §4 status block
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusBlock:
    """The 8-byte status block (§4.1). ``count`` is the raw 16-bit field."""

    id: int
    ibsta: int
    error: int
    count: int

    @property
    def bytes_not_transferred(self) -> int:
        return (0x10000 - self.count) & 0xFFFF

    def transferred(self, requested: int) -> int:
        return requested - self.bytes_not_transferred

    # ibsta bits the specification calls reliable (§4.2), plus END.
    @property
    def end(self) -> bool:
        return bool(self.ibsta & t.IBSTA_END)

    @property
    def srqi(self) -> bool:
        return bool(self.ibsta & t.IBSTA_SRQI)

    @property
    def lok(self) -> bool:
        return bool(self.ibsta & t.IBSTA_LOK)

    @property
    def rem(self) -> bool:
        return bool(self.ibsta & t.IBSTA_REM)

    @property
    def cic(self) -> bool:
        return bool(self.ibsta & t.IBSTA_CIC)

    @property
    def atn(self) -> bool:
        return bool(self.ibsta & t.IBSTA_ATN)

    @property
    def tacs(self) -> bool:
        return bool(self.ibsta & t.IBSTA_TACS)

    @property
    def lacs(self) -> bool:
        return bool(self.ibsta & t.IBSTA_LACS)

    # Derived from ibsta as reported; §4.2 says to trust the error code for
    # ERR/TIMO instead, so these are informational.
    @property
    def err(self) -> bool:
        return bool(self.ibsta & t.IBSTA_ERR)

    @property
    def timo(self) -> bool:
        return bool(self.ibsta & t.IBSTA_TIMO)

    @property
    def cmpl(self) -> bool:
        return bool(self.ibsta & t.IBSTA_CMPL)


def parse_status_block(buf: bytes, offset: int = 0) -> StatusBlock:
    """ibsta is big-endian, the count little-endian, in the same 8 bytes (§8.7)."""
    if len(buf) < offset + STATUS_BLOCK_LENGTH:
        raise ProtocolError('status block needs 8 bytes at offset %d, got %s'
                            % (offset, buf.hex()))
    return StatusBlock(
        id=buf[offset],
        ibsta=int.from_bytes(buf[offset + 1:offset + 3], 'big'),
        error=buf[offset + 3],
        count=int.from_bytes(buf[offset + 4:offset + 6], 'little'),
    )


def parse_status_reply(reply: bytes, expected_id: int) -> StatusBlock:
    """The 12-byte reply to 0x01, 0x06, 0x0c, 0x0d, 0x0f (§3.5)."""
    if len(reply) != STATUS_REPLY_LENGTH:
        raise ProtocolError('expected a %d-byte status reply, got %d: %s'
                            % (STATUS_REPLY_LENGTH, len(reply), reply.hex()))
    status = parse_status_block(reply)
    if status.id != expected_id:
        raise ProtocolError('reply id 0x%02x, expected 0x%02x: %s'
                            % (status.id, expected_id, reply.hex()))
    # Observed on the GPIB-USB-HS: the trailing 4 bytes are the termination
    # block; anything else means the pipes are out of step.
    if reply[8:] != TERMINATION_BLOCK:
        raise ProtocolError('status reply does not end in a termination block: %s' % reply.hex())
    return status


def parse_register_write_reply(reply: bytes) -> Tuple[StatusBlock, int]:
    """The 16-byte reply to 0x09: status block, writes completed, termination."""
    if len(reply) != REGISTER_WRITE_REPLY_LENGTH:
        raise ProtocolError('expected a %d-byte register-write reply, got %d: %s'
                            % (REGISTER_WRITE_REPLY_LENGTH, len(reply), reply.hex()))
    status = parse_status_block(reply)
    if status.id != OP_REGISTER_WRITE:
        raise ProtocolError('register-write reply id 0x%02x: %s' % (status.id, reply.hex()))
    return status, reply[8]


def parse_register_read_reply(reply: bytes, count: int) -> List[int]:
    """0x34 chunks of up to three values, then a 0x35 end block (§3.5)."""
    values: List[int] = []
    offset = 0
    while True:
        if len(reply) < offset + 4:
            raise ProtocolError('register-read reply ended before 0x35: %s' % reply.hex())
        block_id = reply[offset]
        if block_id == BLOCK_REGISTER_VALUES:
            values.extend(reply[offset + 1:offset + 4])
        elif block_id == BLOCK_REGISTER_END:
            # Observed on the GPIB-USB-HS: a termination block follows 0x35
            # (12 bytes for one register). spec gap: the meaning of the 0x35
            # count byte is still unsettled; it is not used.
            break
        else:
            raise ProtocolError('unexpected block 0x%02x in register-read reply: %s'
                                % (block_id, reply.hex()))
        offset += 4
    if len(values) < count:
        raise ProtocolError('register read returned %d values, wanted %d: %s'
                            % (len(values), count, reply.hex()))
    return values[:count]


@dataclass(frozen=True)
class ReadReply:
    """A parsed 0x0a reply (§5.2)."""

    data: bytes
    status: StatusBlock  # id 0x38
    adr1: int            # bit 7 = EOI seen with the last byte; §5.2 says ignore
    #: The specification describes a second status block (id 0x09, for the
    #: embedded register writes) after the pad bytes; the GPIB-USB-HS does not
    #: send one. Kept when present, None otherwise.
    embedded_status: Optional[StatusBlock] = None

    @property
    def end(self) -> bool:
        return self.status.end


def read_status_offset(reply: bytes) -> int:
    """Where the 0x38 block starts: after the leading data blocks (§5.2)."""
    offset = 0
    while offset < len(reply) and reply[offset] in (BLOCK_DATA_15, BLOCK_DATA_30):
        offset += 16 if reply[offset] == BLOCK_DATA_15 else 32
    return offset


def parse_read_reply(reply: bytes, requested: int) -> ReadReply:
    """Data blocks, then the fixed 28-byte trailer (§5.2 reply layout)."""
    payloads: List[bytes] = []
    offset = 0
    while offset < len(reply) and reply[offset] in (BLOCK_DATA_15, BLOCK_DATA_30):
        # Blocks are told apart by their id, so a reply mixing the two sizes
        # (the specification is unsure whether that happens) parses too.
        if reply[offset] == BLOCK_DATA_15:
            payloads.append(reply[offset + 1:offset + 16])
            offset += 16
        else:
            payloads.append(reply[offset + 2:offset + 32])
            offset += 32
    if len(reply) < offset + READ_REPLY_TRAILER_LENGTH:
        raise ProtocolError('read reply trailer short: %s' % reply.hex())
    status = parse_status_block(reply, offset)
    if status.id != BLOCK_READ_STATUS:
        raise ProtocolError('read status id 0x%02x, expected 0x38: %s'
                            % (status.id, reply.hex()))
    adr1 = reply[offset + 8]
    last_block_count = reply[offset + 9]
    tail = offset + 12
    embedded: Optional[StatusBlock] = None
    if len(reply) >= tail + 8 and reply[tail] == OP_REGISTER_WRITE:
        # The specification's layout: the embedded register write reports
        # separately before the termination block. Not seen on the HS.
        embedded = parse_status_block(reply, tail)
        tail += 12
    if len(reply) >= tail + 4 and reply[tail] != OP_TERMINATION:
        raise ProtocolError('read reply does not end in a termination block: %s' % reply.hex())

    if payloads:
        if any(len(p) < 15 for p in payloads[:-1]) or last_block_count > len(payloads[-1]):
            raise ProtocolError('data block shorter than its header implies: %s' % reply.hex())
        data = b''.join(payloads[:-1]) + payloads[-1][:last_block_count]
    else:
        data = b''
    expected = status.transferred(requested)
    if len(data) != expected:
        raise ProtocolError('read reply carries %d data bytes but the count field says %d'
                            % (len(data), expected))
    return ReadReply(data=data, status=status, embedded_status=embedded, adr1=adr1)


def read_reply_buffer_size(max_bytes: int, max_packet_size: int) -> int:
    """Host receive buffer for a read of ``max_bytes`` (§5.2, §8.6)."""
    blocks_30 = -(-max_bytes // 30) * 32
    blocks_15 = -(-max_bytes // 15) * 16
    total = max(blocks_30, blocks_15) + READ_REPLY_TRAILER_MAX
    return -(-total // max_packet_size) * max_packet_size


# --------------------------------------------------------------------------
# §2.2 / §2.3 control-request replies
# --------------------------------------------------------------------------


def parse_serial_number(reply: bytes) -> int:
    """Byte 0 echoes 0x41; bytes 1..4 are the serial, little-endian (§2.2)."""
    if len(reply) < 5 or reply[0] != t.SERIAL_NUMBER_QUERY.request:
        raise ProtocolError('serial-number query answered %s' % reply.hex())
    return int.from_bytes(reply[1:5], 'little')


def readiness_reported(reply: bytes) -> bool:
    """Ready when any of bytes 6, 7, 10 is nonzero (§2.3)."""
    if not reply or reply[0] != t.READINESS_QUERY.request:
        raise ProtocolError('readiness query answered %s' % reply.hex())
    if len(reply) < 11:
        return False
    return bool(reply[6] or reply[7] or reply[10])


# --------------------------------------------------------------------------
# §7 timeouts
# --------------------------------------------------------------------------


def effective_timeout(seconds: Optional[float]) -> Tuple[int, Optional[float]]:
    """The device timeout code for ``seconds`` and the limit that code enforces (§7.1).

    The code is the smallest table row with ``seconds`` <= limit; the limit is
    what the device will wait, which is what the host wait must be derived
    from. None or 0 disables the timeout; so does anything past 1000 s.
    """
    if seconds is None or seconds <= 0:
        return t.TIMEOUT_DISABLED_CODE, None
    for limit, code in t.TIMEOUT_TABLE:
        # A hair of slack so 3.0 s rounded through milliseconds still lands on 0xfc.
        if seconds <= limit * 1.001:
            return code, limit
    return t.TIMEOUT_DISABLED_CODE, None


def timeout_code(seconds: Optional[float]) -> int:
    return effective_timeout(seconds)[0]


def host_wait_s(device_limit_s: Optional[float], infinite_wait_s: float) -> float:
    """How long the host waits for the bulk reply to a 0x0a/0x0c/0x0d (§7.2).

    ``device_limit_s`` is the effective device timeout from ``effective_timeout``,
    not the requested one: the device waits the full table row.
    """
    if device_limit_s is None:
        return infinite_wait_s
    return device_limit_s + max(2.0, 0.5 * device_limit_s)

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
and the filler after the valid bytes is stale data. A read that times out
with nothing read still carries one 0x36 block on this unit, and its
last-block-count byte then holds min(requested, 15), not zero (2026-09-21,
§5.2): the 0x38 count field alone says how many bytes were read.
"""
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from . import tables as t

# --------------------------------------------------------------------------
# §3.2 opcodes and §3.5 reply block ids
# --------------------------------------------------------------------------

OP_TAKE_CONTROL = 0x01
OP_PRESENCE_PROBE = 0x02       # §10.6.1
OP_STATUS_SNAPSHOT = 0x03      # §10.2.2
OP_TERMINATION = 0x04
OP_GO_TO_STANDBY = 0x06
OP_PARALLEL_POLL = 0x07
OP_REGISTER_READ = 0x08
OP_REGISTER_WRITE = 0x09
OP_READ = 0x0A
OP_READ_RAW = 0x0B             # §10.1.2: data on the alternate bulk IN
OP_COMMAND = 0x0C
OP_WRITE = 0x0D
OP_WRITE_RAW = 0x0E            # §10.5.2: data on the alternate bulk OUT
OP_INTERFACE_CLEAR = 0x0F
OP_SERIAL_POLL = 0x10          # §10.5.4

BLOCK_PAD = 0x11               # ``11 00 00 00``, four before a 0x37 run (§10.1.5); skip
BLOCK_STATUS_QUERY = 0x21      # the 0x21 control request's reply id (§10.3.5)
BLOCK_REGISTER_VALUES = 0x34   # up to 3 register values
BLOCK_REGISTER_END = 0x35
BLOCK_DATA_15 = 0x36           # id + 15 data bytes
BLOCK_DATA_30 = 0x37           # id + 00 + 30 data bytes
BLOCK_READ_STATUS = 0x38
BLOCK_SERIAL_POLL_STATUS = 0x39  # follows the 0x3a block (§10.5.4)
BLOCK_SERIAL_POLL_RESULT = 0x3A  # ``3a P S sb``; absent when the poll failed (§10.6.6)

TERMINATION_BLOCK = b'\x04\x00\x00\x00'
STATUS_BLOCK_LENGTH = 8
STATUS_REPLY_LENGTH = 12
REGISTER_WRITE_REPLY_LENGTH = 16
REGISTER_READ_REPLY_LENGTH = 32
#: Reply lengths by block id (§10.2.1, §3.5). The 0x38 read status is listed
#: with its 4-byte tail (ADR1, last-block count, pad), the 0x09 status with
#: its writes-completed word, the 0x0b status with its EOI tail.
REPLY_BLOCK_LENGTHS = {
    OP_TAKE_CONTROL: 8, OP_STATUS_SNAPSHOT: 8, OP_GO_TO_STANDBY: 8, OP_COMMAND: 8,
    OP_WRITE: 8, OP_WRITE_RAW: 8, OP_INTERFACE_CLEAR: 8, BLOCK_STATUS_QUERY: 8,
    BLOCK_SERIAL_POLL_STATUS: 8,
    OP_PRESENCE_PROBE: 12, OP_REGISTER_WRITE: 12, OP_READ_RAW: 12, BLOCK_READ_STATUS: 12,
    BLOCK_PAD: 4, BLOCK_REGISTER_VALUES: 4, BLOCK_REGISTER_END: 4, BLOCK_SERIAL_POLL_RESULT: 4,
    BLOCK_DATA_15: 16, BLOCK_DATA_30: 32,
}
#: The 0x84 reply to a 0x0b, 0x0e or 0x10 message from this driver is a few
#: blocks, parsed by id; one max-size packet holds it with room for anything
#: the device adds.
SMALL_REPLY_BUFFER = 512
#: The 8-byte interrupt push of §10.4.2: ``30 18 00 sb 31 a1 01 00``.
SRQ_PUSH_LENGTH = 8
SRQ_PUSH_ID = 0x30
#: Observed on GPIB-USB-HS 01CEE482: status block (8) + ADR1 + last-block
#: count + 2 pad + termination (4). The specification derived 28 bytes with
#: an embedded 0x09 status block that the device does not send.
READ_REPLY_TRAILER_LENGTH = 16
#: Receive buffers are sized for the longer, specification-derived trailer
#: so a firmware that does send the embedded status still fits.
READ_REPLY_TRAILER_MAX = 28

MAX_COMMAND_BYTES = 16       # §5.3, all models
MAX_TRANSFER_BYTES = 0xFFFF  # §5.1, §5.2
#: The 0x0b / 0x0e count field is 32 bits wide as observed (§10.1.2), but no
#: count above 0xffff was captured and §10.1.2 leaves open whether the field is
#: 32 bits or 16 bits followed by ``ff ff``. Up to 0xffff the two readings
#: encode identically, so one instruction carries at most that; callers loop.
MAX_RAW_TRANSFER_BYTES = 0xFFFF

WRITE_FLAG_EOI = 0x08        # §5.1 ``f``
EOS_MODE_REOS = 0x04         # §5.2 ``m``: terminate on the EOS character
EOS_MODE_BIN = 0x10          # §5.2 ``m``: compare all 8 bits
#: Bit 7 of the read tail byte (item 3 of §5.2; byte 8 of the 0x0b block,
#: §10.1.3): the last byte came with EOI. Tells EOI from an EOS match.
TAIL_EOI = 0x80

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


def encode_count32(length: int) -> bytes:
    """Two's-complement negative of ``length``, 32-bit little-endian (§3.3, §10.1.2).

    ``00 b0 ff ff`` = -20480, ``fe f7 ff ff`` = -2050. Capped at
    ``MAX_RAW_TRANSFER_BYTES`` so the bytes are the same under both readings
    of the field's width.
    """
    if not 1 <= length <= MAX_RAW_TRANSFER_BYTES:
        raise ValueError('raw transfer length %d outside 1..%d' % (length, MAX_RAW_TRANSFER_BYTES))
    return (-length).to_bytes(4, 'little', signed=True)


def decode_count32(buf: bytes, offset: int) -> int:
    """The signed 32-bit (transferred - requested) of a 0x0b / 0x0e status (§10.1.3)."""
    return int.from_bytes(buf[offset:offset + 4], 'little', signed=True)


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


def status_snapshot_block() -> bytes:
    """§10.2.2: ``03 00 00 00``; replies with the ibsta current when it executes."""
    return bytes((OP_STATUS_SNAPSHOT, 0, 0, 0))


def command_block(command_bytes: bytes, timeout_code: int) -> bytes:
    """§5.3: ``0c c 00 t <cmd...>`` (unpadded); at most 16 command bytes."""
    if not 1 <= len(command_bytes) <= MAX_COMMAND_BYTES:
        raise ValueError('%d command bytes; one instruction carries 1..%d'
                         % (len(command_bytes), MAX_COMMAND_BYTES))
    return bytes((OP_COMMAND, encode_count8(len(command_bytes)), 0x00, timeout_code)) + command_bytes


def command_message(command_bytes: bytes, timeout_code: int) -> bytes:
    return build_message(command_block(command_bytes, timeout_code))


def _eos_byte(eos: Optional[int], name: str) -> int:
    value = 0x00 if eos is None else eos
    if not 0 <= value <= 0xFF:
        raise ValueError('%s %r is not a byte' % (name, eos))
    return value


def write_block(data: bytes, timeout_code: int, send_eoi: bool, eos_char: Optional[int] = None) -> bytes:
    """§5.1: ``0d cl ch t 00 e f 00 <data...>`` (unpadded).

    ``e`` (byte 5) is 0x00 in the bench-proven form, which is all the driver
    sends. NI fills it with the session's termination character on every
    write (§10.5.1); ``eos_char`` exists so the capture tests can rebuild
    NI's blocks byte for byte.
    """
    return (bytes((OP_WRITE,)) + encode_count16(len(data))
            + bytes((timeout_code, 0x00, _eos_byte(eos_char, 'termination character'),
                     WRITE_FLAG_EOI if send_eoi else 0x00, 0x00))
            + data)


def write_message(data: bytes, timeout_code: int, send_eoi: bool) -> bytes:
    """The framed write as this driver sends it: ``e`` = 0x00, the bench-proven form."""
    return build_message(write_block(data, timeout_code, send_eoi))


def write_raw_block(length: int, timeout_code: int, send_eoi: bool,
                    eos_char: Optional[int] = None) -> bytes:
    """§10.5.2: ``0e 00 00 t 00 e f 00 c0 c1 c2 c3``; the data goes on the alternate bulk OUT.

    NI's one observed 0x0e carried ``e`` = the termination character (0x0a)
    and ``f`` = 0x08: ``0e 00 00 fe 00 0a 08 00 fe f7 ff ff`` for 2050 bytes.
    """
    return (bytes((OP_WRITE_RAW, 0x00, 0x00, timeout_code, 0x00,
                   _eos_byte(eos_char, 'termination character'),
                   WRITE_FLAG_EOI if send_eoi else 0x00, 0x00))
            + encode_count32(length))


def write_raw_message(length: int, timeout_code: int, send_eoi: bool,
                      eos_char: Optional[int] = None) -> bytes:
    return build_message(write_raw_block(length, timeout_code, send_eoi, eos_char))


def read_eos_bytes(eos: Optional[int], eos_8bit: bool, termchar: Optional[int]) -> bytes:
    """The ``m e`` bytes of a read instruction (§5.2, §10.1.6).

    ``eos`` given: ``m`` = REOS (+ BIN for an 8-bit compare), ``e`` = the
    character; END is then reported for a match as for EOI. ``eos`` None:
    ``m`` = 0x00 and ``e`` = ``termchar`` -- NI puts the session's
    termination character there with the compare disabled and never got
    error 4 (§10.1.6); ``termchar`` None keeps the bench-proven ``00 00``.
    The controller never passes ``termchar``: it is here, and in the read
    message builders, so the capture tests can rebuild NI's blocks.
    """
    if eos is None:
        return bytes((0x00, _eos_byte(termchar, 'termination character')))
    return bytes((EOS_MODE_REOS | (EOS_MODE_BIN if eos_8bit else 0x00), _eos_byte(eos, 'EOS character')))


def read_message(max_bytes: int, timeout_code: int,
                 eos: Optional[int] = None, eos_8bit: bool = False,
                 termchar: Optional[int] = None) -> bytes:
    """§5.2: ``0a m e t cl ch 00 00`` plus the embedded two-write block."""
    header = (bytes((OP_READ,)) + read_eos_bytes(eos, eos_8bit, termchar) + bytes((timeout_code,))
              + encode_count16(max_bytes) + b'\x00\x00')
    return build_message(header, register_write_block(READ_EMBEDDED_WRITES))


def read_raw_block(max_bytes: int, timeout_code: int,
                   eos: Optional[int] = None, eos_8bit: bool = False,
                   termchar: Optional[int] = None) -> bytes:
    """§10.1.2: ``0b m e t c0 c1 c2 c3``; the data arrives on the alternate bulk IN.

    ``0b 00 0a fe 00 b0 ff ff`` is NI's read of 20480 with the termination
    character disabled and a 30 s timeout.
    """
    return (bytes((OP_READ_RAW,)) + read_eos_bytes(eos, eos_8bit, termchar) + bytes((timeout_code,))
            + encode_count32(max_bytes))


#: The register write NI sends after every 0x0b (§10.1.2): AUXMR 0x55, clear END.
#: The 0x51 holdoff of the 0x0a form is not sent with 0x0b.
READ_RAW_FOLLOWING_WRITES: Tuple[Tuple[int, int, int], ...] = ((1, 0x0A, 0x55),)


def read_raw_message(max_bytes: int, timeout_code: int,
                     eos: Optional[int] = None, eos_8bit: bool = False,
                     termchar: Optional[int] = None) -> bytes:
    """The 0x0b instruction followed by the clear-END register write, as NI sends them."""
    return build_message(read_raw_block(max_bytes, timeout_code, eos, eos_8bit, termchar),
                         register_write_block(READ_RAW_FOLLOWING_WRITES))


def serial_poll_block(pad: int, timeout_code: int, sad: Optional[int] = None, flag: int = 0x00) -> bytes:
    """§10.5.4: ``10 01 00 x P S t 00``.

    ``x`` was 0x00 in a fresh session and 0x01 after an SRQ had been serviced;
    its meaning is not established, so 0x00 unless a caller knows better.
    ``S`` is 0x60 | secondary, 0x00 without one: ``10 01 00 00 18 61 fc 00``
    polled address 24 through secondary address 1, and the reply's 0x3a
    block echoed both bytes (§10.5.4, sad_poll.pcap).
    """
    if not 0 <= pad <= 30:
        raise ValueError('primary address %d outside 0..30' % pad)
    if flag not in (0x00, 0x01):
        raise ValueError('serial poll flag byte %r; only 0x00 and 0x01 were observed' % (flag,))
    return bytes((OP_SERIAL_POLL, 0x01, 0x00, flag, pad,
                  0x00 if sad is None else t.secondary_address(sad), timeout_code, 0x00))


def serial_poll_message(pad: int, timeout_code: int, sad: Optional[int] = None, flag: int = 0x00) -> bytes:
    return build_message(serial_poll_block(pad, timeout_code, sad, flag))


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
            # (12 bytes for one register). The 0x35 count byte is the number
            # of registers read, for up to three reads (§3.5, §10.8); above
            # three it is not established, and it is not used here.
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
    #: Item 5 of the §5.2 layout as the device sent it. Not what ``data`` is
    #: cut to -- the count field is -- and stale when nothing was read: the
    #: bench unit puts min(requested, block size) there on a timed-out read.
    last_block_count: int = 0

    @property
    def end(self) -> bool:
        return self.status.end


_READ_LEADING_BLOCKS = (BLOCK_DATA_15, BLOCK_DATA_30, BLOCK_PAD)


def read_status_offset(reply: bytes) -> int:
    """Where the 0x38 block starts: after the leading data and pad blocks (§5.2, §10.1.5)."""
    offset = 0
    while offset < len(reply) and reply[offset] in _READ_LEADING_BLOCKS:
        offset += REPLY_BLOCK_LENGTHS[reply[offset]]
    return offset


def parse_read_reply(reply: bytes, requested: int) -> ReadReply:
    """Data blocks, then the trailer (§5.2 reply layout).

    The trailer is 16 bytes as the bench adapter sends it for this driver's
    message (0x38 status, ADR1, last-block count, two pad bytes,
    termination) and 28 when a 0x09 status for the embedded register write
    sits before the termination block, the form the specification first
    derived; both parse.

    How many bytes were read is the 0x38 count field's to say (§5.2): the
    data is the first that many bytes of the data blocks joined, and the
    filler behind them is never returned. The last-block-count byte is
    reported, not trusted: on a timed-out 0x36-sized read the bench unit
    sends one stale block and min(requested, 15) in that byte while the
    count field says nothing was read, and a parser that sized the data by
    the byte and then checked the field rejected every such read as
    malformed, with a stop request and a re-attach in place of the timeout
    the adapter had reported. The one malformed case is a count field
    claiming more bytes than the blocks hold.
    """
    payloads: List[bytes] = []
    offset = 0
    while offset < len(reply) and reply[offset] in _READ_LEADING_BLOCKS:
        # Blocks are told apart by their id, so a reply mixing the two sizes
        # (the specification is unsure whether that happens) parses too. The
        # ``11 00 00 00`` blocks NI's batched replies carry are skipped (§10.9).
        if reply[offset] == BLOCK_DATA_15:
            payloads.append(reply[offset + 1:offset + 16])
            offset += 16
        elif reply[offset] == BLOCK_DATA_30:
            payloads.append(reply[offset + 2:offset + 32])
            offset += 32
        else:
            offset += 4
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

    joined = b''.join(payloads)
    transferred = status.transferred(requested)
    if not 0 <= transferred <= len(joined):
        raise ProtocolError('read reply count field says %d of %d bytes were read but its data '
                            'blocks hold %d: %s' % (transferred, requested, len(joined), reply.hex()))
    return ReadReply(data=joined[:transferred], status=status, embedded_status=embedded, adr1=adr1,
                     last_block_count=last_block_count)


def read_reply_buffer_size(max_bytes: int, max_packet_size: int) -> int:
    """Host receive buffer for a read of ``max_bytes`` (§5.2, §8.6)."""
    blocks_30 = -(-max_bytes // 30) * 32
    blocks_15 = -(-max_bytes // 15) * 16
    total = max(blocks_30, blocks_15) + READ_REPLY_TRAILER_MAX
    return -(-total // max_packet_size) * max_packet_size


def raw_read_buffer_size(max_bytes: int, max_packet_size: int) -> int:
    """Host receive buffer on the alternate bulk IN for a 0x0b of ``max_bytes``.

    At least one packet larger than the longest transfer the device may send
    for the request: the count itself, or one byte more, since the device
    pads an odd transfer to an even length (6 bytes on the wire for a 5-byte
    reply, trac.pcap 13.0075). A transfer that fills whole packets exactly
    is then ended by the device's zero-length packet, as NI's 32768-byte
    reads of 20480-byte chunks were (§10.1.4); a buffer the transfer fills
    exactly would leave that packet queued for the next read, which would
    return no data. Hence the padded count, not the count, decides: a
    65535-byte request answered in full arrives as 65536 bytes.
    """
    return ((max_bytes + 1) // max_packet_size + 1) * max_packet_size


# --------------------------------------------------------------------------
# §10.2.1 block-by-block replies; §10.1.3, §10.5.2, §10.5.4 the new blocks
# --------------------------------------------------------------------------


def split_reply_blocks(reply: bytes) -> List[Tuple[int, bytes]]:
    """The blocks of a reply up to its termination block, as (id, bytes) (§10.2.1).

    Lengths come from ``REPLY_BLOCK_LENGTHS``; an id outside it, a block cut
    short, or a reply without a termination block is a ``ProtocolError``.
    """
    blocks: List[Tuple[int, bytes]] = []
    offset = 0
    while True:
        if offset >= len(reply):
            raise ProtocolError('reply has no termination block: %s' % reply.hex())
        block_id = reply[offset]
        if block_id == OP_TERMINATION:
            return blocks
        length = REPLY_BLOCK_LENGTHS.get(block_id)
        if length is None:
            raise ProtocolError('unknown block id 0x%02x at offset %d: %s' % (block_id, offset, reply.hex()))
        if offset + length > len(reply):
            raise ProtocolError('block 0x%02x cut short at offset %d: %s' % (block_id, offset, reply.hex()))
        blocks.append((block_id, reply[offset:offset + length]))
        offset += length


def _single_block(blocks: Sequence[Tuple[int, bytes]], block_id: int, reply: bytes) -> bytes:
    found = [block for found_id, block in blocks if found_id == block_id]
    if len(found) != 1:
        raise ProtocolError('expected one 0x%02x block, found %d: %s' % (block_id, len(found), reply.hex()))
    return found[0]


@dataclass(frozen=True)
class RawReadReply:
    """A parsed 0x0b reply (§10.1.3) joined with its data from the alternate bulk IN."""

    data: bytes
    status: StatusBlock  # id 0x0b; ``count`` is the low 16 bits only
    count32: int         # (transferred - requested), signed
    eoi: bool            # bit 7 of the tail byte: the last byte came with EOI

    @property
    def end(self) -> bool:
        return self.status.end


def parse_raw_read_reply(reply: bytes, requested: int, data: bytes) -> RawReadReply:
    """The 0x84 reply to a 0x0b, with the bytes that arrived on the alternate bulk IN.

    Bytes read = requested + count (count <= 0). The count is authoritative:
    the transfer on the alternate endpoint is truncated to it (one capture
    shows 6 bytes on the wire for a count of 5), and fewer bytes than the
    count says is a ``ProtocolError``.
    """
    blocks = split_reply_blocks(reply)
    block = _single_block(blocks, OP_READ_RAW, reply)
    status = parse_status_block(block)
    count32 = decode_count32(block, 4)
    transferred = requested + count32
    if not 0 <= transferred <= requested:
        raise ProtocolError('0x0b count %d for a request of %d: %s' % (count32, requested, reply.hex()))
    if len(data) < transferred:
        raise ProtocolError('0x0b reply says %d bytes read but %d arrived' % (transferred, len(data)))
    return RawReadReply(data=data[:transferred], status=status, count32=count32,
                        eoi=bool(block[8] & TAIL_EOI))


@dataclass(frozen=True)
class RawWriteReply:
    """A parsed 0x0e reply (§10.5.2): an 8-byte status block with a 32-bit count."""

    status: StatusBlock
    count32: int

    def transferred(self, requested: int) -> int:
        return requested + self.count32


def parse_raw_write_reply(reply: bytes) -> RawWriteReply:
    blocks = split_reply_blocks(reply)
    block = _single_block(blocks, OP_WRITE_RAW, reply)
    return RawWriteReply(status=parse_status_block(block), count32=decode_count32(block, 4))


@dataclass(frozen=True)
class SerialPollReply:
    """A parsed 0x10 reply: ``3a P S sb`` then a status block with id 0x39 (§10.5.4).

    A poll that failed is answered with the status block alone (§10.6.6);
    ``status_byte``, ``pad`` and ``sad_byte`` are then None and
    ``status.error`` says why.
    """

    status_byte: Optional[int]
    pad: Optional[int]
    sad_byte: Optional[int]
    status: StatusBlock


def parse_serial_poll_reply(reply: bytes) -> SerialPollReply:
    """The 0x3a block is there exactly when the poll produced a status byte (§10.6.6).

    Its absence next to error 0 would be a success without a result, which
    is a ``ProtocolError``. Its presence next to an error is accepted: the
    error decides, and the caller raises for it.
    """
    blocks = split_reply_blocks(reply)
    status = parse_status_block(_single_block(blocks, BLOCK_SERIAL_POLL_STATUS, reply))
    results = [block for block_id, block in blocks if block_id == BLOCK_SERIAL_POLL_RESULT]
    if len(results) > 1:
        raise ProtocolError('expected at most one 0x3a block, found %d: %s' % (len(results), reply.hex()))
    if not results:
        if status.error == t.ERR_SUCCESS:
            raise ProtocolError('serial poll succeeded without a 0x3a block: %s' % reply.hex())
        return SerialPollReply(status_byte=None, pad=None, sad_byte=None, status=status)
    result = results[0]
    return SerialPollReply(status_byte=result[3], pad=result[1], sad_byte=result[2], status=status)


@dataclass(frozen=True)
class SrqPush:
    """The 8-byte interrupt push on a service request (§10.4.2)."""

    ibsta: int        # 0x1800 = SRQI | RQS in both captures
    status_byte: int  # the instrument's status byte, already serial-polled by the adapter
    raw: bytes

    @property
    def srqi(self) -> bool:
        return bool(self.ibsta & t.IBSTA_SRQI)


def parse_srq_push(push: bytes) -> SrqPush:
    """``30 18 00 sb 31 a1 01 00``: ibsta big-endian at 1-2, the status byte at 3.

    Bytes 4-7 are not established. Byte 0 was 0x30 in every push captured;
    it is not checked, since no other push has been seen to compare with.
    """
    if len(push) < SRQ_PUSH_LENGTH:
        raise ProtocolError('interrupt push of %d bytes, expected %d: %s'
                            % (len(push), SRQ_PUSH_LENGTH, push.hex()))
    return SrqPush(ibsta=int.from_bytes(push[1:3], 'big'), status_byte=push[3], raw=bytes(push[:SRQ_PUSH_LENGTH]))


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
    """The device timeout code for ``seconds`` and the nominal limit of that code (§7.1).

    The code is the smallest table row with ``seconds`` <= limit. The limit
    is the row's nominal value, not what the adapter waits: that is the
    expiry of §7.3 (``tables.timeout_expiry_s``), which is what a host wait
    is derived from. None or 0 disables the timeout; so does anything past
    1000 s.
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


#: §7.2: what the host waits beyond the adapter's own expiry. Nothing observed
#: scales with the timeout -- on one unit the reply trailed the power of two
#: by at most 1.9 ms at 0.13 s and at 33.6 s alike, on the other it was exact
#: to the millisecond across repeats (§7.3) -- so the margin is fixed.
HOST_WAIT_MARGIN_S = 2.0


def host_wait_s(code: int, infinite_wait_s: float) -> float:
    """How long the host waits for the reply to an instruction sent with ``code`` (§7.2).

    The longest expiry either timed adapter showed under the code
    (``tables.timeout_expiry_s``, §7.3) plus ``HOST_WAIT_MARGIN_S``, so the
    adapter always ends the instruction first and says so in its reply.
    The code on the wire decides, not the timeout asked for, and not the
    code's nominal limit: a wait of nominal + max(2 s, 50 %) is 15 s for
    0xfd, which one unit runs for 16.78 s and the other for 20.0 s. A wait
    sized by the first unit alone, 18.78 s, reached the stop request on
    the second 1.2 s before its own error 0x0a reply, and a timeout was
    reported as an I/O error. For a code nobody timed the expiry is 1.25
    times the larger of the nominal limit and the inferred power of two.
    ``infinite_wait_s`` is returned for the disabled code 0xf0, where only
    the host can end the wait.

    This is the wait for one timed instruction. A message with two would
    need the sum of their expiries (§7.2: NI's read messages carry a 0x0c
    and a 0x0a / 0x0b); every message this driver builds carries one, the
    addressing 0x0c being a message of its own and the register write that
    rides with a read having no timeout code.
    """
    expiry = t.timeout_expiry_s(code)
    if expiry is None:
        return infinite_wait_s
    return expiry + HOST_WAIT_MARGIN_S

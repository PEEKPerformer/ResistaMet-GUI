"""Round-trip and hostile-input properties of the NI GPIB-USB codec.

``test_gpib_usb_protocol.py`` asserts the specification's worked examples
byte for byte, and ``test_gpib_usb_captures.py`` replays NI's own traffic.
Neither says what happens to bytes nobody wrote down. Here hypothesis draws
them:

* every message an encoder builds is whole 4-byte blocks ending in
  ``04 00 00 00``, and the negative counts in it decode back to what was
  asked for;
* replies assembled at random from the block kinds of the specification
  (``docs/design/ni_usb_gpib_protocol.md`` §3.5, §5.2, §10.1-10.6) parse back
  to the fields they were built from, and ``split_reply_blocks`` accounts for
  every byte;
* a parser handed anything else -- noise, a reply cut short, a reply with
  bits flipped, nothing -- raises the package's own ``GpibError`` family or
  returns, and never IndexError, struct.error, KeyError or ValueError. A USB
  glitch must surface as a protocol error the controller knows how to
  recover from, not as a crash in the measurement thread.

Written from the specification and ``protocol.py`` alone, like the package.
The run is derandomised and keeps no example database.
"""

import random
import threading

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t

# No deadline: a 65535-byte message is slow to draw, not slow to build, and
# shared CI runners make the per-example clock flaky. max_examples bounds it.
PROPERTY = settings(max_examples=200, deadline=None, database=None, derandomize=True)

byte = st.integers(0, 0xFF)
word = st.integers(0, 0xFFFF)
timeout_codes = st.sampled_from([t.TIMEOUT_DISABLED_CODE] + [code for _, code in t.TIMEOUT_TABLE])
eos_chars = st.one_of(st.none(), byte)
KNOWN_IDS = sorted(p.REPLY_BLOCK_LENGTHS)


def _well_framed(message: bytes) -> bool:
    return len(message) % 4 == 0 and message.endswith(p.TERMINATION_BLOCK) and len(message) >= 8


def _minus16(field: bytes) -> int:
    return (0x10000 - int.from_bytes(field, "little")) & 0xFFFF


# ---------------------------------------------------------------------------
# host -> adapter
# ---------------------------------------------------------------------------


class TestEncoders:
    @PROPERTY
    @given(st.binary(min_size=1, max_size=300), timeout_codes, st.booleans(), eos_chars)
    def test_write(self, data, code, eoi, eos):
        # write_message is the form the driver sends (e = 0x00); the block
        # builder under it still takes the character NI puts there.
        message = p.build_message(p.write_block(data, code, eoi, eos))
        assert _well_framed(message)
        assert message[0] == p.OP_WRITE and message[3] == code
        assert _minus16(message[1:3]) == len(data)
        assert message[5] == (eos or 0) and message[6] == (p.WRITE_FLAG_EOI if eoi else 0)
        assert message[8:8 + len(data)] == data
        # Only padding lies between the data and the termination block.
        assert set(message[8 + len(data):-4]) <= {0} and len(message) - 12 - len(data) < 4

    @pytest.mark.parametrize("length", [1, 2, 3, 4, 0xFF, 0x100, 0xFFFE, 0xFFFF])
    def test_write_at_the_edges_of_the_count(self, length):
        message = p.write_message(bytes(length), 0xFC, True)
        assert _well_framed(message) and _minus16(message[1:3]) == length

    @PROPERTY
    @given(st.integers(1, 0xFFFF), timeout_codes, eos_chars, st.booleans(), eos_chars)
    def test_read(self, count, code, eos, eos_8bit, termchar):
        message = p.read_message(count, code, eos, eos_8bit, termchar)
        assert _well_framed(message) and len(message) == 24
        assert message[0] == p.OP_READ and message[3] == code
        assert _minus16(message[4:6]) == count
        if eos is None:
            assert message[1] == 0 and message[2] == (termchar or 0)
        else:
            assert message[1] == (p.EOS_MODE_REOS | (p.EOS_MODE_BIN if eos_8bit else 0)) and message[2] == eos
        # The count as the status block will report it: nothing transferred yet.
        status = p.parse_status_block(bytes((p.BLOCK_READ_STATUS, 0, 0, 0)) + message[4:6] + b"\xff\xff")
        assert status.bytes_not_transferred == count and status.transferred(count) == 0

    @PROPERTY
    @given(st.integers(1, p.MAX_RAW_TRANSFER_BYTES), timeout_codes, eos_chars, st.booleans(), eos_chars, st.booleans())
    def test_raw_read_and_raw_write(self, count, code, eos, eos_8bit, termchar, eoi):
        read = p.read_raw_message(count, code, eos, eos_8bit, termchar)
        assert _well_framed(read) and read[0] == p.OP_READ_RAW and read[3] == code
        assert p.decode_count32(read, 4) == -count
        assert read[8] == p.OP_REGISTER_WRITE         # the clear-END write follows
        write = p.write_raw_message(count, code, eoi, termchar)
        assert _well_framed(write) and len(write) == 16
        assert write[0] == p.OP_WRITE_RAW and write[3] == code
        assert p.decode_count32(write, 8) == -count
        # Up to 0xffff a 32-bit count and a 16-bit count followed by ff ff
        # are the same bytes; the driver relies on that (MAX_RAW_TRANSFER_BYTES).
        assert p.encode_count32(count) == p.encode_count16(count) + b"\xff\xff"

    @PROPERTY
    @given(st.binary(min_size=1, max_size=p.MAX_COMMAND_BYTES), timeout_codes)
    def test_command(self, commands, code):
        message = p.command_message(commands, code)
        assert _well_framed(message)
        assert message[0] == p.OP_COMMAND and message[3] == code
        assert (0x100 - message[1]) & 0xFF == len(commands)
        assert message[4:4 + len(commands)] == commands

    @PROPERTY
    @given(st.lists(st.tuples(byte, byte, byte), min_size=1, max_size=0xFF),
           st.lists(st.tuples(byte, byte), min_size=1, max_size=0xFF))
    def test_register_messages(self, writes, reads):
        message = p.register_write_message(writes)
        assert _well_framed(message) and message[0] == p.OP_REGISTER_WRITE and message[1] == len(writes)
        assert [tuple(message[3 + 3 * k:6 + 3 * k]) for k in range(len(writes))] == writes
        message = p.register_read_message(reads)
        assert _well_framed(message) and message[0] == p.OP_REGISTER_READ and message[1] == len(reads)
        assert [tuple(message[2 + 2 * k:4 + 2 * k]) for k in range(len(reads))] == reads

    @PROPERTY
    @given(st.integers(0, 30), timeout_codes, st.one_of(st.none(), st.integers(0, 31)), st.sampled_from([0, 1]))
    def test_serial_poll_and_the_fixed_messages(self, pad, code, sad, flag):
        message = p.serial_poll_message(pad, code, sad, flag)
        assert _well_framed(message) and len(message) == 12
        assert message[0] == p.OP_SERIAL_POLL and message[4] == pad and message[6] == code
        assert message[5] == (0 if sad is None else 0x60 | sad)
        for fixed in (p.take_control_message(), p.take_control_message(False), p.go_to_standby_message(),
                      p.interface_clear_message(), p.build_message(p.status_snapshot_block())):
            assert _well_framed(fixed) and len(fixed) == 8

    @PROPERTY
    @given(st.lists(st.binary(max_size=40), max_size=6))
    def test_build_message_pads_each_block(self, blocks):
        message = p.build_message(*blocks)
        assert len(message) % 4 == 0 and message.endswith(p.TERMINATION_BLOCK)
        offset = 0
        for block in blocks:
            assert message[offset:offset + len(block)] == block
            offset += len(block) + (-len(block) % 4)
        assert offset == len(message) - 4

    @PROPERTY
    @given(st.integers(-5, 0x20000))
    def test_counts_round_trip_or_are_refused(self, length):
        for encode, limit in ((p.encode_count16, 0xFFFF), (p.encode_count32, p.MAX_RAW_TRANSFER_BYTES)):
            if 1 <= length <= limit:
                field = encode(length)
                assert int.from_bytes(field, "little", signed=True) == -length or len(field) == 2
                assert _minus16(field[:2]) == length
            else:
                with pytest.raises(ValueError):
                    encode(length)
        if 1 <= length <= 0xFF:
            assert (0x100 - p.encode_count8(length)) & 0xFF == length
        else:
            with pytest.raises(ValueError):
                p.encode_count8(length)

    @PROPERTY
    @given(st.integers(-3, 300), st.integers(-3, 40), st.integers(-3, 40))
    def test_out_of_range_arguments_are_value_errors(self, eos, pad, sad):
        """The encoders' own refusal is ValueError -- a caller's mistake, not
        a protocol event -- and never a bytes() complaint from inside."""
        def attempt(build):
            try:
                assert _well_framed(build())
                return True
            except ValueError:
                return False

        assert attempt(lambda: p.build_message(p.write_block(b"x", 0xFC, True, eos))) == (0 <= eos <= 0xFF)
        assert attempt(lambda: p.read_message(10, 0xFC, eos)) == (0 <= eos <= 0xFF)
        assert attempt(lambda: p.read_raw_message(10, 0xFC, None, False, eos)) == (0 <= eos <= 0xFF)
        assert attempt(lambda: p.serial_poll_message(pad, 0xFC, sad)) == (0 <= pad <= 30 and 0 <= sad <= 31)
        assert not attempt(lambda: p.write_message(b"", 0xFC, True))
        assert not attempt(lambda: p.command_message(bytes(17), 0xFC))


class TestTimeoutCodes:
    @PROPERTY
    @given(st.floats(1e-7, 2000.0), st.floats(1e-7, 2000.0))
    def test_the_code_waits_at_least_as_long_as_asked_and_no_longer_than_needed(self, a, b):
        code, limit = p.effective_timeout(a)
        if limit is None:
            assert a > t.TIMEOUT_MAX_S and code == t.TIMEOUT_DISABLED_CODE
        else:
            assert limit * 1.001 >= a
            shorter = [row for row, _ in t.TIMEOUT_TABLE if row < limit]
            assert all(row * 1.001 < a for row in shorter)
        # More time asked for is never less time given.
        low, high = sorted((a, b))
        limit_low, limit_high = p.effective_timeout(low)[1], p.effective_timeout(high)[1]
        assert limit_high is None or (limit_low is not None and limit_low <= limit_high)

    def test_every_code_the_encoder_can_emit_has_a_host_wait_that_outlasts_it(self):
        for nominal, code in t.TIMEOUT_TABLE:
            expiry = t.timeout_expiry_s(code)
            assert expiry is not None
            assert p.host_wait_s(code, 1e9) == pytest.approx(expiry + p.HOST_WAIT_MARGIN_S)
            assert p.host_wait_s(code, 1e9) > expiry
        assert p.host_wait_s(t.TIMEOUT_DISABLED_CODE, 123.0) == 123.0
        for seconds in (None, 0, 0.0, -1.0):
            assert p.effective_timeout(seconds) == (t.TIMEOUT_DISABLED_CODE, None)


# ---------------------------------------------------------------------------
# adapter -> host: replies built from the specification's layouts
# ---------------------------------------------------------------------------


def _status(block_id: int, ibsta: int, error: int, count: int, tail: bytes = b"\x00\x00") -> bytes:
    return bytes((block_id,)) + ibsta.to_bytes(2, "big") + bytes((error,)) + count.to_bytes(2, "little") + tail


@st.composite
def framed_read_replies(draw):
    """(reply, data, requested, fields) for a 0x0a read, §5.2 and §10.1.5."""
    data = draw(st.binary(max_size=200))
    requested = len(data) + draw(st.integers(0, 300))
    if requested == 0:
        requested = 1
    wide = draw(st.booleans())
    payload = 30 if wide else 15
    filler = draw(byte)
    pads = draw(st.sampled_from([0, 0, 4]))                           # §10.1.5: four, or none
    parts = [bytes((p.BLOCK_PAD, 0, 0, 0))] * pads
    chunks = [data[k:k + payload] for k in range(0, len(data), payload)]
    if not chunks and not wide and draw(st.booleans()):
        chunks = [b""]              # a timed-out 0x36 read still carries one block, 0 valid (§10.1.5)
    for chunk in chunks:
        body = chunk + bytes((filler,)) * (payload - len(chunk))      # stale bytes after the valid ones
        parts.append((bytes((p.BLOCK_DATA_30, 0)) if wide else bytes((p.BLOCK_DATA_15,))) + body)
    ibsta, error, adr1 = draw(word), draw(byte), draw(byte)
    # With no data block at all the last-block count is stale (§10.1.5).
    last = len(chunks[-1]) if chunks else draw(byte)
    trailer = _status(p.BLOCK_READ_STATUS, ibsta, error, (len(data) - requested) & 0xFFFF, b"\xff\xff")
    trailer += bytes((adr1, last, 0, 0))
    embedded = draw(st.booleans())
    if embedded:
        trailer += _status(p.OP_REGISTER_WRITE, 0, 0, 0) + bytes((2, 0, 0, 0))
    reply = b"".join(parts) + trailer + p.TERMINATION_BLOCK
    return reply, data, requested, dict(ibsta=ibsta, error=error, adr1=adr1, embedded=embedded,
                                        status_offset=len(reply) - len(trailer) - 4, pad_bytes=4 * pads)


@st.composite
def block_replies(draw):
    """A reply of random known blocks, a termination block, and whatever
    the device left in the buffer after it."""
    blocks = []
    for block_id in draw(st.lists(st.sampled_from(KNOWN_IDS), max_size=12)):
        length = p.REPLY_BLOCK_LENGTHS[block_id]
        blocks.append((block_id, bytes((block_id,)) + draw(st.binary(min_size=length - 1, max_size=length - 1))))
    leftover = draw(st.binary(max_size=12))
    return blocks, b"".join(body for _, body in blocks) + p.TERMINATION_BLOCK + leftover


class TestRepliesRoundTrip:
    @PROPERTY
    @given(framed_read_replies())
    def test_framed_read(self, case):
        reply, data, requested, fields = case
        parsed = p.parse_read_reply(reply, requested)
        assert parsed.data == data
        assert parsed.status.id == p.BLOCK_READ_STATUS
        assert parsed.status.ibsta == fields["ibsta"] and parsed.status.error == fields["error"]
        assert parsed.status.transferred(requested) == len(data)
        assert parsed.adr1 == fields["adr1"]
        assert (parsed.embedded_status is not None) == fields["embedded"]
        assert parsed.end == bool(fields["ibsta"] & t.IBSTA_END)
        assert p.read_status_offset(reply) == fields["status_offset"]
        # The buffer the driver allocates holds the reply to its own bare
        # 0x0a (no pad blocks: those follow other instructions' blocks),
        # whichever block size the adapter chose.
        for packet in (64, 512):
            size = p.read_reply_buffer_size(requested, packet)
            assert size % packet == 0 and size >= len(reply) - fields["pad_bytes"]

    @PROPERTY
    @given(framed_read_replies(), st.data())
    def test_framed_read_cut_short_is_an_error_or_the_same_data(self, case, data_strategy):
        reply, data, requested, fields = case
        cut = data_strategy.draw(st.integers(0, len(reply)))
        try:
            parsed = p.parse_read_reply(reply[:cut], requested)
        except p.ProtocolError:
            return
        # What follows the read status and its tail may be missing; the data
        # may not. A short read is never passed off as a complete one.
        assert parsed.data == data and cut >= fields["status_offset"] + 12

    @PROPERTY
    @given(block_replies())
    def test_split_accounts_for_every_byte(self, case):
        blocks, reply = case
        split = p.split_reply_blocks(reply)
        assert split == blocks
        consumed = sum(len(body) for _, body in split)
        assert reply[consumed:consumed + 4] == p.TERMINATION_BLOCK
        assert b"".join(body for _, body in split) == reply[:consumed]
        assert all(len(body) == p.REPLY_BLOCK_LENGTHS[block_id] and body[0] == block_id for block_id, body in split)

    @PROPERTY
    @given(block_replies(), st.data())
    def test_split_of_a_cut_reply_is_an_error(self, case, data_strategy):
        blocks, reply = case
        whole = sum(len(body) for _, body in blocks)
        cut = data_strategy.draw(st.integers(0, whole))
        with pytest.raises(p.ProtocolError):
            p.split_reply_blocks(reply[:cut])

    @PROPERTY
    @given(byte, word, byte, word)
    def test_status_reply(self, block_id, ibsta, error, count):
        reply = _status(block_id, ibsta, error, count) + p.TERMINATION_BLOCK
        status = p.parse_status_reply(reply, block_id)
        assert (status.id, status.ibsta, status.error, status.count) == (block_id, ibsta, error, count)
        assert status.bytes_not_transferred == (-count) & 0xFFFF
        for name, bit in (("end", t.IBSTA_END), ("srqi", t.IBSTA_SRQI), ("cic", t.IBSTA_CIC),
                          ("atn", t.IBSTA_ATN), ("err", t.IBSTA_ERR), ("timo", t.IBSTA_TIMO)):
            assert getattr(status, name) == bool(ibsta & bit)
        with pytest.raises(p.ProtocolError):
            p.parse_status_reply(reply, (block_id + 1) & 0xFF)

    @PROPERTY
    @given(st.lists(byte, min_size=1, max_size=20), byte, st.binary(min_size=3, max_size=3))
    def test_register_read(self, values, filler, end_tail):
        reply = b""
        for k in range(0, len(values), 3):
            chunk = bytes(values[k:k + 3])
            reply += bytes((p.BLOCK_REGISTER_VALUES,)) + chunk + bytes((filler,)) * (3 - len(chunk))
        reply += bytes((p.BLOCK_REGISTER_END,)) + end_tail + p.TERMINATION_BLOCK
        assert p.parse_register_read_reply(reply, len(values)) == values
        padded_to = -(-len(values) // 3) * 3
        with pytest.raises(p.ProtocolError):
            p.parse_register_read_reply(reply, padded_to + 1)

    @PROPERTY
    @given(st.binary(max_size=300), st.integers(0, 300), word, byte, st.booleans(), st.binary(max_size=3), st.booleans())
    def test_raw_read(self, data, short_by, ibsta, error, eoi, wire_padding, with_write_status):
        """§10.1.3: bytes read = requested + count; the transfer on the
        alternate endpoint may be longer (padded) and is cut to the count."""
        requested = max(1, len(data) + short_by)
        count32 = len(data) - requested
        def reply_with(count: int) -> bytes:
            block = (bytes((p.OP_READ_RAW,)) + ibsta.to_bytes(2, "big") + bytes((error,))
                     + count.to_bytes(4, "little", signed=True) + bytes((p.TAIL_EOI if eoi else 0, 0, 0, 0)))
            if with_write_status:
                block += _status(p.OP_REGISTER_WRITE, 0, 0, 0) + bytes((1, 0, 0, 0))
            return block + p.TERMINATION_BLOCK

        reply = reply_with(count32)
        parsed = p.parse_raw_read_reply(reply, requested, data + wire_padding)
        assert parsed.data == data and parsed.count32 == count32 and parsed.eoi == eoi
        assert parsed.status.error == error and parsed.status.ibsta == ibsta
        if data:
            with pytest.raises(p.ProtocolError):
                p.parse_raw_read_reply(reply, requested, data[:-1])
        # More read than asked for, or less than nothing: not a count.
        for impossible in (1, -(requested + 1)):
            with pytest.raises(p.ProtocolError):
                p.parse_raw_read_reply(reply_with(impossible), requested, data + bytes(2))

    @PROPERTY
    @given(st.integers(1, p.MAX_RAW_TRANSFER_BYTES), st.integers(0, p.MAX_RAW_TRANSFER_BYTES), word, byte)
    def test_raw_write(self, requested, done, ibsta, error):
        done = min(done, requested)
        block = (bytes((p.OP_WRITE_RAW,)) + ibsta.to_bytes(2, "big") + bytes((error,))
                 + (done - requested).to_bytes(4, "little", signed=True))
        parsed = p.parse_raw_write_reply(block + p.TERMINATION_BLOCK)
        assert parsed.transferred(requested) == done and parsed.status.error == error

    @PROPERTY
    @given(st.integers(0, 30), byte, byte, word, byte, st.booleans())
    def test_serial_poll(self, pad, sad_byte, status_byte, ibsta, error, answered):
        status = _status(p.BLOCK_SERIAL_POLL_STATUS, ibsta, error, 0)
        result = bytes((p.BLOCK_SERIAL_POLL_RESULT, pad, sad_byte, status_byte))
        reply = (result if answered else b"") + status + p.TERMINATION_BLOCK
        if not answered and error == t.ERR_SUCCESS:
            with pytest.raises(p.ProtocolError):
                p.parse_serial_poll_reply(reply)
            return
        parsed = p.parse_serial_poll_reply(reply)
        assert parsed.status.error == error
        if answered:
            assert (parsed.pad, parsed.sad_byte, parsed.status_byte) == (pad, sad_byte, status_byte)
        else:
            assert parsed.status_byte is None and parsed.pad is None
        with pytest.raises(p.ProtocolError):
            p.parse_serial_poll_reply(result + result + status + p.TERMINATION_BLOCK)

    @PROPERTY
    @given(st.integers(0, 0xFFFFFFFF), word, byte, st.binary(min_size=4, max_size=12))
    def test_control_replies(self, serial, ibsta, status_byte, rest):
        reply = bytes((t.SERIAL_NUMBER_QUERY.request,)) + serial.to_bytes(4, "little") + rest
        assert p.parse_serial_number(reply) == serial
        push = bytes((p.SRQ_PUSH_ID,)) + ibsta.to_bytes(2, "big") + bytes((status_byte,)) + rest[:4]
        parsed = p.parse_srq_push(push + rest)
        assert (parsed.ibsta, parsed.status_byte, parsed.raw) == (ibsta, status_byte, push)


# ---------------------------------------------------------------------------
# adapter -> host: anything at all
# ---------------------------------------------------------------------------


def _every_parser(reply: bytes, number: int, extra: bytes):
    """Each parser once. Returns how many accepted the bytes."""
    calls = (
        lambda: p.parse_status_block(reply),
        lambda: p.parse_status_block(reply, number % 64),
        lambda: p.parse_status_reply(reply, number & 0xFF),
        lambda: p.parse_status_reply(reply, reply[0] if reply else 0),
        lambda: p.parse_register_write_reply(reply),
        lambda: p.parse_register_read_reply(reply, number % 9),
        lambda: p.read_status_offset(reply),
        lambda: p.parse_read_reply(reply, number),
        lambda: p.split_reply_blocks(reply),
        lambda: p.parse_raw_read_reply(reply, number, extra),
        lambda: p.parse_raw_write_reply(reply),
        lambda: p.parse_serial_poll_reply(reply),
        lambda: p.parse_srq_push(reply),
        lambda: p.parse_serial_number(reply),
        lambda: p.readiness_reported(reply),
    )
    accepted = 0
    for call in calls:
        try:
            call()
            accepted += 1
        except p.GpibError:
            pass            # ProtocolError and its relatives: the contract
    return accepted


@st.composite
def damaged_replies(draw):
    """Known block ids with random bodies, then cut and bit-flipped."""
    ids = st.one_of(st.sampled_from(KNOWN_IDS + [p.OP_TERMINATION]), byte)
    reply = b""
    for block_id in draw(st.lists(ids, max_size=10)):
        length = p.REPLY_BLOCK_LENGTHS.get(block_id, 4)
        body = draw(st.one_of(st.just(bytes(length - 1)), st.binary(min_size=length - 1, max_size=length - 1)))
        reply += bytes((block_id,)) + body
    if draw(st.booleans()):
        reply += p.TERMINATION_BLOCK
    return _damage(draw, reply)


def _damage(draw, reply: bytes) -> bytes:
    if draw(st.booleans()):
        reply = reply[:draw(st.integers(0, len(reply)))]
    damaged = bytearray(reply)
    for _ in range(draw(st.integers(0, 3))):
        if damaged:
            damaged[draw(st.integers(0, len(damaged) - 1))] ^= 1 << draw(st.integers(0, 7))
    return bytes(damaged)


class TestHostileReplies:
    @PROPERTY
    @given(st.binary(max_size=120), st.integers(0, 70000), st.binary(max_size=40))
    def test_noise(self, reply, number, extra):
        _every_parser(reply, number, extra)

    @PROPERTY
    @given(damaged_replies(), st.integers(0, 70000), st.binary(max_size=40))
    def test_damaged_block_sequences(self, reply, number, extra):
        _every_parser(reply, number, extra)

    @PROPERTY
    @given(framed_read_replies(), st.data())
    def test_damaged_read_replies(self, case, data_strategy):
        reply, data, requested, _ = case
        _every_parser(_damage(data_strategy.draw, reply), requested, data)

    def test_nothing_at_all(self):
        assert _every_parser(b"", 0, b"") == 1            # only read_status_offset has an answer: 0
        assert p.read_status_offset(b"") == 0

    def test_a_large_seeded_corpus_finishes(self):
        """No parser loops for ever: each advances by at least one block per
        turn. Run off the main thread so that a regression fails here
        instead of hanging the suite."""
        rng = random.Random(20260919)
        ids = KNOWN_IDS + [p.OP_TERMINATION, 0x00, 0xFF]

        def corpus():
            for _ in range(4000):
                if rng.random() < 0.3:
                    yield bytes(rng.randrange(256) for _ in range(rng.randrange(0, 96)))
                    continue
                reply = b""
                for _ in range(rng.randrange(0, 40)):
                    block_id = rng.choice(ids)
                    length = p.REPLY_BLOCK_LENGTHS.get(block_id, 4)
                    reply += bytes((block_id,)) + bytes(rng.randrange(256) for _ in range(length - 1))
                yield reply[:rng.randrange(0, len(reply) + 1)] if rng.random() < 0.5 else reply

        failures = []

        def work():
            try:
                for reply in corpus():
                    _every_parser(reply, rng.randrange(0, 70000), reply[:16])
            except BaseException as exc:      # reported from the main thread
                failures.append(exc)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(timeout=60.0)
        assert not worker.is_alive(), "a parser did not return"
        assert not failures, failures

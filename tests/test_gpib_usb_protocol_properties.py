"""Seeded random checks of the NI GPIB-USB codec (``gpib_usb.protocol``).

The example tests pin the worked examples byte for byte; these pin what
must hold for every input: a parser fed anything at all raises
``ProtocolError`` or returns, counts and buffer sizes obey their rules for
every length, replies built at random parse back to what went in, and the
timeout table is monotonic. ``random.Random`` with a fixed seed, so a
failure reproduces; each failure message carries the offending bytes.
"""
import random
from typing import Callable, Dict, List, Tuple

import pytest

from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t

SEED = 20260919
PACKET = 512


def random_bytes(rng: random.Random, length: int) -> bytes:
    return bytes(rng.getrandbits(8) for _ in range(length))


def random_blocks(rng: random.Random, terminated: bool) -> Tuple[List[Tuple[int, bytes]], bytes]:
    """Up to six blocks with known ids and random bodies, as (id, bytes) and as one reply."""
    ids = [block_id for block_id in p.REPLY_BLOCK_LENGTHS if block_id != p.OP_TERMINATION]
    blocks = []
    for _ in range(rng.randint(0, 6)):
        block_id = rng.choice(ids)
        blocks.append((block_id, bytes((block_id,)) + random_bytes(rng, p.REPLY_BLOCK_LENGTHS[block_id] - 1)))
    reply = b''.join(block for _, block in blocks) + (p.TERMINATION_BLOCK if terminated else b'')
    return blocks, reply


def parsers(rng: random.Random) -> Dict[str, Callable[[bytes], object]]:
    return {
        'status_reply': lambda b: p.parse_status_reply(b, b[0] if b else 1),
        'status_block': p.parse_status_block,
        'register_write': p.parse_register_write_reply,
        'register_read': lambda b: p.parse_register_read_reply(b, rng.randint(1, 4)),
        'read': lambda b: p.parse_read_reply(b, rng.randint(1, 300)),
        'read_status': lambda b: p.parse_status_block(b, p.read_status_offset(b)),
        'raw_read': lambda b: p.parse_raw_read_reply(b, rng.randint(1, 65535), bytes(rng.randint(0, 40))),
        'raw_write': p.parse_raw_write_reply,
        'serial_poll': p.parse_serial_poll_reply,
        'srq_push': p.parse_srq_push,
        'serial_number': p.parse_serial_number,
        'readiness': p.readiness_reported,
        'split': p.split_reply_blocks,
    }


class TestParsersNeverEscape:
    def test_only_protocol_error_leaves_a_parser(self):
        rng = random.Random(SEED)
        table = parsers(rng)
        for _ in range(6000):
            kind = rng.random()
            if kind < 0.5:
                _, buf = random_blocks(rng, terminated=rng.random() < 0.7)
            elif kind < 0.8:
                _, buf = random_blocks(rng, terminated=rng.random() < 0.7)
                buf = buf[:rng.randint(0, len(buf))]
            else:
                buf = random_bytes(rng, rng.randint(0, 80))
            for name, parse in table.items():
                try:
                    parse(buf)
                except p.ProtocolError:
                    pass
                except Exception as exc:  # noqa: BLE001 - the point of the test
                    pytest.fail('%s let %s out for %s' % (name, type(exc).__name__, buf.hex()))


class TestCountsAndBuffers:
    LENGTHS = list(range(1, 300)) + [511, 512, 513, 1023, 1024, 1025, 20480, 32768, 65534, 65535]

    def lengths(self) -> List[int]:
        rng = random.Random(SEED + 1)
        return self.LENGTHS + [rng.randint(1, 65535) for _ in range(2000)]

    def test_counts_are_the_negative_length_and_round_trip(self):
        for n in self.lengths():
            assert 0x10000 - int.from_bytes(p.encode_count16(n), 'little') == n, n
            assert -p.decode_count32(p.encode_count32(n), 0) == n, n
            # Up to 0xffff the 32-bit field reads the same as 16 bits followed by ff ff (§10.1.2).
            assert p.encode_count32(n) == p.encode_count16(n) + b'\xff\xff', n
        for n in range(1, 256):
            assert (0x100 - p.encode_count8(n)) == n

    @pytest.mark.parametrize('bad', [0, -1, 0x10000])
    def test_counts_outside_one_instruction_are_refused(self, bad):
        for encode in (p.encode_count16, p.encode_count32):
            with pytest.raises(ValueError):
                encode(bad)

    def test_the_raw_read_buffer_always_leaves_room_for_the_ending_packet(self):
        for n in self.lengths():
            size = p.raw_read_buffer_size(n, PACKET)
            padded = n + n % 2   # an odd transfer is padded to even on the wire
            assert size % PACKET == 0 and size > padded, n

    def test_the_framed_read_buffer_holds_either_block_size_and_the_long_trailer(self):
        for n in self.lengths():
            size = p.read_reply_buffer_size(n, PACKET)
            assert size % PACKET == 0, n
            assert size >= -(-n // 30) * 32 + p.READ_REPLY_TRAILER_MAX, n
            assert size >= -(-n // 15) * 16 + p.READ_REPLY_TRAILER_MAX, n


class TestBlockSequences:
    def test_split_returns_the_blocks_that_were_joined(self):
        rng = random.Random(SEED + 2)
        for _ in range(3000):
            blocks, reply = random_blocks(rng, terminated=True)
            assert p.split_reply_blocks(reply + random_bytes(rng, rng.randint(0, 8))) == blocks, reply.hex()

    def test_a_reply_cut_anywhere_before_its_termination_block_is_a_protocol_error(self):
        rng = random.Random(SEED + 3)
        for _ in range(1500):
            _, reply = random_blocks(rng, terminated=True)
            body = len(reply) - len(p.TERMINATION_BLOCK)
            for cut in {0, body, rng.randint(0, body)}:
                with pytest.raises(p.ProtocolError):
                    p.split_reply_blocks(reply[:cut])


def framed_read_reply(rng: random.Random, data: bytes, requested: int, block_id: int, pads: int,
                      embedded: bool, end: bool) -> bytes:
    """A 0x0a reply as the adapter lays it out: pads, data blocks with stale filler, the trailer."""
    out = b'\x11\x00\x00\x00' * pads
    step = 15 if block_id == p.BLOCK_DATA_15 else 30
    for start in range(0, len(data), step):
        chunk = data[start:start + step]
        lead = bytes((block_id,)) if block_id == p.BLOCK_DATA_15 else bytes((block_id, 0))
        out += lead + chunk + random_bytes(rng, step - len(chunk))
    last = (len(data) - 1) % step + 1 if data else rng.getrandbits(8)
    ibsta = (t.IBSTA_END if end else 0) | 0x0064
    out += (bytes((p.BLOCK_READ_STATUS,)) + ibsta.to_bytes(2, 'big') + b'\x00'
            + ((len(data) - requested) & 0xFFFF).to_bytes(2, 'little') + b'\xff\xff')
    out += bytes((0xE0 if end else 0x60, last, 0, 0))
    if embedded:
        out += bytes((p.OP_REGISTER_WRITE, 0, 0x64, 0)) + bytes(4) + b'\x02\x00\x00\x00'
    return out + p.TERMINATION_BLOCK


class TestFramedReadRoundTrip:
    def test_random_replies_parse_back_to_their_data(self):
        rng = random.Random(SEED + 4)
        for _ in range(3000):
            requested = rng.randint(1, 400)
            data = random_bytes(rng, rng.randint(0, requested))
            end = rng.random() < 0.5
            reply = framed_read_reply(rng, data, requested, rng.choice((p.BLOCK_DATA_15, p.BLOCK_DATA_30)),
                                      rng.choice((0, 4)), rng.random() < 0.3, end)
            parsed = p.parse_read_reply(reply, requested)
            assert parsed.data == data and parsed.end is end, reply.hex()
            assert p.parse_status_block(reply, p.read_status_offset(reply)).id == p.BLOCK_READ_STATUS

    def test_a_framed_reply_is_never_a_whole_number_of_packets_short_of_its_buffer(self):
        # The reply must end in a short packet for the host read to complete: it always fits
        # the buffer with room to spare, so the transfer can never fill the buffer exactly.
        rng = random.Random(SEED + 5)
        for _ in range(500):
            requested = rng.randint(1, 3000)
            data = random_bytes(rng, requested)
            for block_id in (p.BLOCK_DATA_15, p.BLOCK_DATA_30):
                reply = framed_read_reply(rng, data, requested, block_id, 0, False, True)
                assert len(reply) < p.read_reply_buffer_size(requested, PACKET), requested


class TestEosBytes:
    def test_the_m_e_table(self):
        for termchar in (None, 0x00, 0x0A, 0xFF):
            assert p.read_eos_bytes(None, False, termchar) == bytes((0x00, termchar or 0x00))
            assert p.read_eos_bytes(None, True, termchar) == bytes((0x00, termchar or 0x00))
        for eos in range(256):
            assert p.read_eos_bytes(eos, False, None) == bytes((0x04, eos))
            assert p.read_eos_bytes(eos, True, None) == bytes((0x14, eos))
            # The compare character wins over the session's termination character.
            assert p.read_eos_bytes(eos, True, 0x2C) == bytes((0x14, eos))

    @pytest.mark.parametrize('bad', [-1, 256])
    def test_a_character_that_is_not_a_byte_is_refused(self, bad):
        with pytest.raises(ValueError):
            p.read_eos_bytes(bad, True, None)
        with pytest.raises(ValueError):
            p.read_eos_bytes(None, False, bad)


class TestTimeouts:
    def test_the_code_chosen_never_promises_less_than_was_asked(self):
        rng = random.Random(SEED + 6)
        previous_limit = 0.0
        for seconds in sorted(10 ** rng.uniform(-6, 3) for _ in range(3000)):
            code, limit = p.effective_timeout(seconds)
            assert code != t.TIMEOUT_DISABLED_CODE and limit is not None, seconds
            assert limit * 1.001 >= seconds, seconds
            assert limit >= previous_limit, seconds      # monotonic in the request
            previous_limit = limit

    def test_nothing_and_too_much_disable_the_timeout(self):
        for seconds in (None, 0, -1.0, t.TIMEOUT_MAX_S * 1.01, 1e9):
            assert p.effective_timeout(seconds) == (t.TIMEOUT_DISABLED_CODE, None)

    def test_limits_expiries_and_host_waits_rise_together(self):
        limits = [limit for limit, _ in t.TIMEOUT_TABLE]
        assert limits == sorted(limits) and len(set(limits)) == len(limits)
        expiries = [t.timeout_expiry_s(code) for _, code in t.TIMEOUT_TABLE]
        assert expiries == sorted(expiries)
        waits = [p.host_wait_s(code, 600.0) for _, code in t.TIMEOUT_TABLE]
        assert waits == sorted(waits)
        for expiry, wait in zip(expiries, waits):
            assert wait == pytest.approx(expiry + p.HOST_WAIT_MARGIN_S)
        assert p.host_wait_s(t.TIMEOUT_DISABLED_CODE, 123.0) == 123.0

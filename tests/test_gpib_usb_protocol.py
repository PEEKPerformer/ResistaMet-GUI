"""The NI GPIB-USB wire format, checked byte for byte against the specification.

Every hex example in docs/design/ni_usb_gpib_protocol.md §3.6 appears here in
both directions, plus the edge cases of the count encoding, the timeout
table, status-block fields, multi-block read replies and register reads.
"""
import pytest

from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t


def h(text: str) -> bytes:
    return bytes.fromhex(text.replace(' ', ''))


# ---------------------------------------------------------------------------
# §3.6 worked examples, host -> device
# ---------------------------------------------------------------------------

class TestWorkedExamplesOut:
    def test_register_write_ren_on(self):
        assert p.register_write_message([t.REN_ON_WRITE]) == h('09 01 00 01 0a 1f 00 00 04 00 00 00')

    def test_command_unl_mta0_lad22(self):
        assert p.command_message(bytes((0x3F, 0x40, 0x36)), 0xFC) == h('0c fd 00 fc 3f 40 36 00 04 00 00 00')

    def test_write_idn_with_eoi(self):
        assert p.write_message(b'*IDN?\n', 0xFC, send_eoi=True) == h(
            '0d fa ff fc 00 00 08 00 2a 49 44 4e 3f 0a 00 00 04 00 00 00')

    def test_read_256_eos_disabled(self):
        assert p.read_message(256, 0xFC) == h(
            '0a 00 00 fc 00 ff 00 00 09 02 00 01 0a 51 01 0a 55 00 00 00 04 00 00 00')
        assert len(p.read_message(256, 0xFC)) == 24

    def test_register_read_bsr(self):
        assert p.register_read_message([t.BSR_REGISTER]) == h('08 01 01 1f 04 00 00 00')

    def test_take_control_synchronous(self):
        assert p.take_control_message(True) == h('01 01 00 00 04 00 00 00')
        assert p.take_control_message(False) == h('01 00 00 00 04 00 00 00')

    def test_go_to_standby_and_ifc(self):
        assert p.go_to_standby_message() == h('06 00 00 00 04 00 00 00')
        assert p.interface_clear_message() == h('0f 00 00 00 04 00 00 00')

    def test_addressing_helpers_produce_the_example_bytes(self):
        assert t.address_listener_command(0, 22) == bytes((0x3F, 0x40, 0x36))
        assert t.address_talker_command(0, 22) == bytes((0x3F, 0x20, 0x56))
        assert t.address_listener_command(0, 22, sad=5) == bytes((0x3F, 0x40, 0x36, 0x65))
        assert t.serial_poll_enable_command(0, 24) == bytes((0x3F, 0x20, 0x18, 0x58))
        assert t.SERIAL_POLL_DISABLE_COMMAND == bytes((0x19, 0x5F))
        assert t.addressed_command(24, t.CMD_SDC) == bytes((0x3F, 0x38, 0x04))
        assert t.addressed_command(24, t.CMD_GET, sad=1) == bytes((0x3F, 0x38, 0x61, 0x08))

    def test_addresses_are_range_checked(self):
        with pytest.raises(ValueError):
            t.listen_address(31)
        with pytest.raises(ValueError):
            t.talk_address(-1)
        with pytest.raises(ValueError):
            t.secondary_address(32)


#: §2.6 rows 1-26 for own address 0, system controller, T1 = 2000 ns, written
#: out by hand from the table and framed per §3.4 / §3.1 (84-byte block + termination).
REGISTER_INIT_GOLDEN = h(
    '09 1a 00'
    '03 10 00'   # 1  device-level register
    '01 1c 22'   # 2  CMDR soft reset
    '01 0a 81'   # 3  AUXRA holdoff-all
    '01 06 81'   # 4  note (a)
    '01 0d 01'   # 5  HSSEL
    '01 0a 02'   # 6  chip reset
    '01 1d 80'   # 7  IMR0
    '01 02 00'   # 8  IMR1
    '01 04 00'   # 9  IMR2
    '01 12 00'   # 10 IMR3
    '01 0a 51'   # 11 holdoff immediately
    '01 0a e1'   # 12 AUXRI (2000 ns row)
    '01 0a a0'   # 13 AUXRB
    '01 17 00'   # 14 KEYREG
    '01 0a 48'   # 15 AUXRG NTNL
    '01 1c 03'   # 16 CMDR set system controller
    '01 0a 16'   # 17 clear IFC
    '01 0c 00'   # 18 ADR = 0
    '02 00 00'   # 19 bank-2 mirror
    '01 0c e0'   # 20 ADR second register, no secondary
    '01 08 31'   # 21 ADMR mode 1
    '02 01 00'   # 22 bank-2 mirror of the secondary
    '02 02 fd'   # 23
    '01 0f 11'   # 24
    '01 0a 00'   # 25 pon
    '01 0a 01'   # 26 clear ppoll flag
    '00 00 00'   # pad 81 -> 84
    '04 00 00 00')


class TestRegisterInitGolden:
    def test_full_message_matches_the_hand_built_bytes(self):
        assert len(REGISTER_INIT_GOLDEN) == 88
        assert p.register_write_message(t.register_init_writes()) == REGISTER_INIT_GOLDEN


# ---------------------------------------------------------------------------
# §3.6 worked examples, device -> host
# ---------------------------------------------------------------------------

class TestWorkedExamplesIn:
    def test_register_write_reply(self):
        status, completed = p.parse_register_write_reply(
            h('09 01 30 00 00 00 00 00 01 00 00 00 04 00 00 00'))
        assert status.id == 0x09
        assert status.ibsta == 0x0130
        assert status.error == 0
        assert completed == 1

    def test_command_reply(self):
        status = p.parse_status_reply(h('0c 01 30 00 00 00 00 00 04 00 00 00'), p.OP_COMMAND)
        assert status.error == 0
        assert status.transferred(3) == 3

    def test_write_reply(self):
        status = p.parse_status_reply(h('0d 01 38 00 00 00 00 00 04 00 00 00'), p.OP_WRITE)
        assert status.transferred(6) == 6
        assert status.tacs

    def test_read_reply_as_the_specification_draws_it_is_tolerated(self):
        # The device disagreed with this layout (see TestObservedReplies): the
        # HS sends no embedded 0x09 block. The parser still accepts the longer form.
        reply = h('36 41 42 43 44 45 0a ee ee ee ee ee ee ee ee ee'
                  '38 20 00 00 06 ff 00 00'
                  'aa 06 00 00'
                  '09 00 00 00 00 00 00 00'
                  '02 00 00 00'
                  '04 00 00 00')
        assert len(reply) == 44
        parsed = p.parse_read_reply(reply, 256)
        assert parsed.data == b'ABCDE\n'
        assert parsed.end is True
        assert parsed.status.count == 0xFF06
        assert parsed.status.transferred(256) == 6
        assert parsed.adr1 == 0xAA
        assert parsed.embedded_status is not None and parsed.embedded_status.id == 0x09
        assert p.read_status_offset(reply) == 16

    def test_register_read_bsr_reply(self):
        assert p.parse_register_read_reply(h('34 5a 00 00 35 01 00 00 04 00 00 00'), 1) == [0x5A]
        assert p.parse_register_read_reply(h('34 5a 00 00 35 01 00 00'), 1) == [0x5A]

    def test_take_control_reply(self):
        status = p.parse_status_reply(h('01 00 30 00 00 00 00 00 04 00 00 00'), p.OP_TAKE_CONTROL)
        assert status.cic and status.atn


#: Captured from GPIB-USB-HS 01CEE482 with a Keithley 2400 at PAD 3, 2026-09-18.
IDN_TEXT = b'KEITHLEY INSTRUMENTS INC.,MODEL 2400,1175680,C30   Mar 17 2006 09:29:29/A02  /K/J\n'
IDN_REPLY_256 = h(
    '37 00 4b 45 49 54 48 4c 45 59 20 49 4e 53 54 52 55 4d 45 4e 54 53 20 49 4e 43 2e 2c 4d 4f 44 45'
    '37 00 4c 20 32 34 30 30 2c 31 31 37 35 36 38 30 2c 43 33 30 20 20 20 4d 61 72 20 31 37 20 32 30'
    '37 00 30 36 20 30 39 3a 32 39 3a 32 39 2f 41 30 32 20 20 2f 4b 2f 4a 0a 00 00 00 00 00 00 00 00'
    '38 20 20 00 52 ff ff ff e0 16 00 00 04 00 00 00')
#: Serial-poll read of one byte (STB 0); the 0x36 filler is stale bus data.
STB_REPLY_1 = h('36 00 20 00 aa 55 ff ff 04 00 00 00 04 00 00 00'
                '38 00 20 00 00 00 01 00 60 01 00 00 04 00 00 00')
#: Count-limited read of 5 bytes out of "KEITHLEY...": no END.
KEITH_REPLY_5 = h('36 4b 45 49 54 48 ff ff 04 00 00 00 04 00 00 00'
                  '38 00 20 00 00 00 01 00 60 05 00 00 04 00 00 00')
#: Read of up to 64 bytes with nothing to read: device-side timeout, no data blocks.
TIMEOUT_REPLY_64 = h('38 00 20 0a c0 ff ff ff e0 5e 00 00 04 00 00 00')


class TestObservedReplies:
    """Read replies exactly as the GPIB-USB-HS sent them (the spec derived a longer trailer)."""

    def test_idn_in_three_extended_blocks(self):
        assert len(IDN_REPLY_256) == 112
        parsed = p.parse_read_reply(IDN_REPLY_256, 256)
        assert parsed.data == IDN_TEXT and len(parsed.data) == 82
        assert parsed.end is True
        assert parsed.status.ibsta == 0x2020  # END | CIC
        assert parsed.status.error == 0
        assert parsed.status.count == 0xFF52 and parsed.status.transferred(256) == 82
        assert parsed.adr1 == 0xE0  # EOI seen
        assert parsed.embedded_status is None
        assert p.read_status_offset(IDN_REPLY_256) == 96

    def test_one_byte_serial_poll_reply(self):
        parsed = p.parse_read_reply(STB_REPLY_1, 1)
        assert parsed.data == b'\x00' and parsed.end is False
        assert parsed.status.transferred(1) == 1

    def test_count_limited_read(self):
        parsed = p.parse_read_reply(KEITH_REPLY_5, 5)
        assert parsed.data == b'KEITH' and parsed.end is False
        assert parsed.adr1 == 0x60

    def test_timeout_reply_has_no_data_blocks(self):
        parsed = p.parse_read_reply(TIMEOUT_REPLY_64, 64)
        assert parsed.data == b''
        assert parsed.status.error == t.ERR_TIMEOUT
        assert parsed.status.transferred(64) == 0
        assert p.read_status_offset(TIMEOUT_REPLY_64) == 0

    def test_trailer_must_end_in_a_termination_block(self):
        broken = bytearray(TIMEOUT_REPLY_64)
        broken[12] = 0x09
        with pytest.raises(p.ProtocolError):
            p.parse_read_reply(bytes(broken), 64)

    def test_status_reply_must_end_in_a_termination_block(self):
        with pytest.raises(p.ProtocolError):
            p.parse_status_reply(h('0c 00 78 00 00 00 ff ff 00 00 00 00'), p.OP_COMMAND)
        status = p.parse_status_reply(h('0c 00 78 00 00 00 ff ff 04 00 00 00'), p.OP_COMMAND)
        assert status.ibsta == 0x0078  # observed after addressing: REM | CIC | ATN | TACS


# ---------------------------------------------------------------------------
# §3.3 count encoding
# ---------------------------------------------------------------------------

class TestCountEncoding:
    @pytest.mark.parametrize('length, expected', [
        (1, 'ff ff'), (3, 'fd ff'), (6, 'fa ff'), (256, '00 ff'), (65535, '01 00'),
    ])
    def test_sixteen_bit(self, length, expected):
        assert p.encode_count16(length) == h(expected)

    @pytest.mark.parametrize('length, expected', [(1, 0xFF), (3, 0xFD), (16, 0xF0), (255, 0x01)])
    def test_eight_bit(self, length, expected):
        assert p.encode_count8(length) == expected

    @pytest.mark.parametrize('length', [0, 65536, -1])
    def test_sixteen_bit_rejects_out_of_range(self, length):
        with pytest.raises(ValueError):
            p.encode_count16(length)

    @pytest.mark.parametrize('length', [0, 256])
    def test_eight_bit_rejects_out_of_range(self, length):
        with pytest.raises(ValueError):
            p.encode_count8(length)

    def test_reply_count_is_transferred_minus_requested(self):
        assert p.StatusBlock(0, 0, 0, 0).transferred(10) == 10
        assert p.StatusBlock(0, 0, 0, 0xFF06).transferred(256) == 6
        assert p.StatusBlock(0, 0, 0, 0xFF00).transferred(256) == 0


# ---------------------------------------------------------------------------
# §3.1 padding, §5.3 chunk limit, §5.2 EOS bytes
# ---------------------------------------------------------------------------

class TestMessageShape:
    def test_blocks_are_padded_to_four_and_terminated(self):
        assert p.command_message(b'\x3f', 0xFC) == h('0c ff 00 fc 3f 00 00 00 04 00 00 00')
        assert p.command_message(bytes(4), 0xFC) == h('0c fc 00 fc 00 00 00 00 04 00 00 00')
        assert p.write_message(b'A', 0xFB, False) == h('0d ff ff fb 00 00 00 00 41 00 00 00 04 00 00 00')

    def test_command_instruction_carries_at_most_16_bytes(self):
        p.command_message(bytes(16), 0xFC)
        with pytest.raises(ValueError):
            p.command_message(bytes(17), 0xFC)
        with pytest.raises(ValueError):
            p.command_message(b'', 0xFC)

    def test_write_length_limits(self):
        assert len(p.write_message(bytes(0xFFFF), 0xFC, True)) == 8 + 0xFFFF + 1 + 4
        with pytest.raises(ValueError):
            p.write_message(b'', 0xFC, True)
        with pytest.raises(ValueError):
            p.write_message(bytes(0x10000), 0xFC, True)

    def test_read_eos_bytes(self):
        assert p.read_message(10, 0xFC, eos=0x0A)[1:3] == h('04 0a')
        assert p.read_message(10, 0xFC, eos=0x0A, eos_8bit=True)[1:3] == h('14 0a')
        assert p.read_message(10, 0xFC)[1:3] == h('00 00')
        with pytest.raises(ValueError):
            p.read_message(10, 0xFC, eos=256)

    def test_read_eos_bytes_as_ni_sends_them(self):
        # §10.1.6: compare disabled -> m = 0x00 with e = the session's termination
        # character; enabled -> m = 0x14 with it. The enabled character wins over termchar.
        assert p.read_eos_bytes(None, True, 0x0A) == h('00 0a')
        assert p.read_eos_bytes(0x0A, True, 0x0A) == h('14 0a')
        assert p.read_eos_bytes(0x2C, True, 0x0A) == h('14 2c')
        assert p.read_eos_bytes(None, True, None) == h('00 00')
        assert p.read_message(200, 0xFC, termchar=0x0A)[1:3] == h('00 0a')
        with pytest.raises(ValueError):
            p.read_eos_bytes(None, True, 300)

    def test_register_read_of_four(self):
        assert p.register_read_message(t.USB_B_SERIAL_REGISTERS) == h(
            '08 04 03 0b 03 0a 03 09 03 08 00 00 04 00 00 00')


# ---------------------------------------------------------------------------
# §7 timeouts
# ---------------------------------------------------------------------------

class TestTimeouts:
    @pytest.mark.parametrize('seconds, code', [
        (None, 0xF0), (0, 0xF0), (10e-6, 0xF1), (30e-6, 0xF2), (100e-6, 0xF3), (300e-6, 0xF4),
        (1e-3, 0xF5), (3e-3, 0xF6), (10e-3, 0xF7), (30e-3, 0xF8), (100e-3, 0xF9), (300e-3, 0xFA),
        (1.0, 0xFB), (3.0, 0xFC), (10.0, 0xFD), (30.0, 0xFE), (100.0, 0xFF), (300.0, 0x01),
        (1000.0, 0x02), (1001.0, 0xF0),
    ])
    def test_table_entries(self, seconds, code):
        assert p.timeout_code(seconds) == code

    def test_rounds_up_to_the_next_row(self):
        assert p.timeout_code(2.0) == 0xFC
        assert p.timeout_code(3.1) == 0xFD
        assert p.timeout_code(0.5) == 0xFB
        assert p.timeout_code(5e-6) == 0xF1

    @pytest.mark.parametrize('seconds, code, limit', [
        (5.0, 0xFD, 10.0), (3.0, 0xFC, 3.0), (3.001, 0xFC, 3.0), (20.0, 0xFE, 30.0),
        (150.0, 0x01, 300.0), (1000.0, 0x02, 1000.0), (None, 0xF0, None), (0, 0xF0, None),
        (5000.0, 0xF0, None),
    ])
    def test_effective_timeout_reports_the_limit_the_device_enforces(self, seconds, code, limit):
        assert p.effective_timeout(seconds) == (code, limit)

    def test_host_wait_is_derived_from_the_effective_limit(self):
        assert p.host_wait_s(1.0, 600) == 3.0
        assert p.host_wait_s(3.0, 600) == 5.0
        assert p.host_wait_s(10.0, 600) == 15.0
        assert p.host_wait_s(None, 600) == 600
        # A 5 s request runs on the 10 s row, so the host waits 15 s, not 7.
        _, limit = p.effective_timeout(5.0)
        assert p.host_wait_s(limit, 600) == 15.0

    def test_timeout_max(self):
        assert t.TIMEOUT_MAX_S == 1000.0


# ---------------------------------------------------------------------------
# §4 status block
# ---------------------------------------------------------------------------

class TestStatusBlock:
    def test_ibsta_big_endian_count_little_endian(self):
        status = p.parse_status_block(h('38 60 00 0a 06 ff 00 00'))
        assert status.ibsta == 0x6000
        assert status.timo and status.end
        assert status.error == 0x0A
        assert status.count == 0xFF06
        assert status.transferred(256) == 6

    def test_named_bits(self):
        status = p.parse_status_block(h('01 91 fc 00 00 00 00 00'))
        assert status.ibsta == 0x91FC
        assert status.err and status.srqi and status.cmpl
        assert status.lok and status.rem and status.cic and status.atn
        assert status.tacs and status.lacs
        assert not status.timo and not status.end

    def test_parse_at_offset(self):
        buf = b'\xff' * 16 + h('06 00 30 00 00 00 00 00')
        assert p.parse_status_block(buf, 16).id == 0x06

    def test_too_short_raises(self):
        with pytest.raises(p.ProtocolError):
            p.parse_status_block(h('0d 00 00'))

    def test_status_reply_length_and_id_are_asserted(self):
        with pytest.raises(p.ProtocolError):
            p.parse_status_reply(h('0d 00 00 00 00 00 00 00'), p.OP_WRITE)
        with pytest.raises(p.ProtocolError):
            p.parse_status_reply(h('0c 00 00 00 00 00 00 00 04 00 00 00'), p.OP_WRITE)

    def test_register_write_reply_shape(self):
        with pytest.raises(p.ProtocolError):
            p.parse_register_write_reply(h('09 00 00 00 00 00 00 00 01 00 00 00'))
        with pytest.raises(p.ProtocolError):
            p.parse_register_write_reply(h('0d 00 00 00 00 00 00 00 01 00 00 00 04 00 00 00'))

    @pytest.mark.parametrize('code, label', [
        (0, 'success'), (1, 'cut short by a stop request'), (2, 'read attempted while ATN true'),
        (3, 'not addressed'), (4, 'EOS configuration rejected / command chunk too long'),
        (5, 'no acceptor on the bus'), (7, 'not controller in charge'), (8, 'no listener addressed'),
        (10, 'device-side timeout'), (6, 'unknown'), (11, 'unknown'),
    ])
    def test_error_labels(self, code, label):
        assert t.error_label(code) == label

    def test_error_code_to_exception(self):
        assert isinstance(p.error_for_code(0x0A, 'read'), p.GpibTimeout)
        five = p.error_for_code(5, 'command')
        eight = p.error_for_code(8, 'write')
        assert isinstance(five, p.NoListener) and five.code == 5
        assert isinstance(eight, p.NoListener) and eight.code == 8
        three = p.error_for_code(3, 'read')
        assert type(three) is p.GpibError and three.code == 3
        assert 'not addressed' in str(three)


# ---------------------------------------------------------------------------
# §5.2 read reply reassembly
# ---------------------------------------------------------------------------

def read_reply(data: bytes, requested: int, *, end: bool = True, error: int = 0,
               block: int = 15, spec_layout: bool = False) -> bytes:
    """Build a 0x0a reply the way the device lays it out (16-byte trailer).

    ``spec_layout`` adds the embedded 0x09 status block the specification
    describes and the GPIB-USB-HS does not send.
    """
    payload = 15 if block == 15 else 30
    blocks = []
    for start in range(0, len(data), payload):
        chunk = data[start:start + payload]
        filler = b'\xee' * (payload - len(chunk))
        if block == 15:
            blocks.append(bytes((0x36,)) + chunk + filler)
        else:
            blocks.append(bytes((0x37, 0x00)) + chunk + filler)
    last_count = len(data) - (len(blocks) - 1) * payload if blocks else 0
    ibsta = (t.IBSTA_END if end else 0) | (t.IBSTA_TIMO if error == t.ERR_TIMEOUT else 0)
    count = (len(data) - requested) & 0xFFFF
    trailer = (bytes((0x38,)) + ibsta.to_bytes(2, 'big') + bytes((error,))
               + count.to_bytes(2, 'little') + b'\xff\xff'
               + bytes((0xE0 if end else 0x60, last_count, 0, 0)))
    if spec_layout:
        trailer += h('09 00 00 00 00 00 00 00') + h('02 00 00 00')
    return b''.join(blocks) + trailer + h('04 00 00 00')


class TestReadReplyReassembly:
    def test_two_short_blocks(self):
        data = bytes(range(20))
        parsed = p.parse_read_reply(read_reply(data, 64), 64)
        assert parsed.data == data
        assert parsed.end

    def test_extended_blocks(self):
        data = bytes(range(40))
        reply = read_reply(data, 100, block=30)
        assert reply[0] == 0x37 and reply[32] == 0x37
        assert p.parse_read_reply(reply, 100).data == data
        assert p.read_status_offset(reply) == 64

    def test_specification_layout_still_parses(self):
        data = bytes(range(20))
        reply = read_reply(data, 64, spec_layout=True)
        parsed = p.parse_read_reply(reply, 64)
        assert parsed.data == data and parsed.embedded_status is not None

    def test_exactly_one_full_block(self):
        data = bytes(15)
        parsed = p.parse_read_reply(read_reply(data, 15, end=False), 15)
        assert parsed.data == data
        assert parsed.end is False

    def test_no_data_on_timeout(self):
        parsed = p.parse_read_reply(read_reply(b'', 256, end=False, error=t.ERR_TIMEOUT), 256)
        assert parsed.data == b''
        assert parsed.status.error == t.ERR_TIMEOUT
        assert parsed.status.transferred(256) == 0
        assert p.read_status_offset(read_reply(b'', 256, end=False)) == 0

    def test_partial_data_on_timeout(self):
        parsed = p.parse_read_reply(read_reply(b'ABC', 256, end=False, error=t.ERR_TIMEOUT), 256)
        assert parsed.data == b'ABC'

    def test_count_field_must_agree_with_blocks(self):
        reply = bytearray(read_reply(b'ABCDE', 256))
        reply[16 + 4:16 + 6] = (0x0004 - 256 & 0xFFFF).to_bytes(2, 'little')  # says 4, blocks say 5
        with pytest.raises(p.ProtocolError):
            p.parse_read_reply(bytes(reply), 256)

    def test_missing_trailer_raises(self):
        with pytest.raises(p.ProtocolError):
            p.parse_read_reply(read_reply(b'ABC', 10)[:20], 10)

    def test_wrong_status_id_raises(self):
        reply = bytearray(read_reply(b'ABC', 10))
        reply[16] = 0x0D
        with pytest.raises(p.ProtocolError):
            p.parse_read_reply(bytes(reply), 10)

    @pytest.mark.parametrize('requested, packet, expected', [
        (256, 512, 512), (65535, 512, 70144), (1, 512, 512), (1, 64, 64), (30, 64, 64), (31, 64, 128),
        (1000, 512, 1536),
    ])
    def test_receive_buffer_size(self, requested, packet, expected):
        assert p.read_reply_buffer_size(requested, packet) == expected


# ---------------------------------------------------------------------------
# §3.5 register-read replies
# ---------------------------------------------------------------------------

class TestRegisterReadReply:
    def test_one_register(self):
        assert p.parse_register_read_reply(h('34 07 00 00 35 01 00 00'), 1) == [0x07]

    def test_four_registers_usb_b_serial(self):
        reply = h('34 78 56 34 34 12 00 00 35 04 00 00 04 00 00 00')
        values = p.parse_register_read_reply(reply, 4)
        assert values == [0x78, 0x56, 0x34, 0x12]
        assert int.from_bytes(bytes(values), 'little') == 0x12345678

    def test_end_block_missing_raises(self):
        with pytest.raises(p.ProtocolError):
            p.parse_register_read_reply(h('34 07 00 00'), 1)

    def test_unexpected_block_raises(self):
        with pytest.raises(p.ProtocolError):
            p.parse_register_read_reply(h('38 07 00 00 35 01 00 00'), 1)

    def test_too_few_values_raises(self):
        with pytest.raises(p.ProtocolError):
            p.parse_register_read_reply(h('34 07 00 00 35 01 00 00'), 4)


# ---------------------------------------------------------------------------
# §2 attach data
# ---------------------------------------------------------------------------

class TestAttachData:
    def test_register_init_is_26_writes_in_spec_order(self):
        writes = t.register_init_writes(own_address=0, system_controller=True)
        assert len(writes) == t.REGISTER_INIT_COUNT == 26
        assert writes[0] == (3, 0x10, 0x00)
        assert writes[1] == (1, 0x1C, 0x22)
        assert writes[2] == (1, 0x0A, 0x81)
        assert writes[3] == (1, 0x06, 0x81)
        assert writes[4] == (1, 0x0D, 0x01)
        assert writes[10] == (1, 0x0A, 0x51)
        assert writes[14] == (1, 0x0A, 0x48)
        assert writes[15] == (1, 0x1C, 0x03)
        assert writes[16] == (1, 0x0A, 0x16)
        assert writes[17] == (1, 0x0C, 0x00) and writes[18] == (2, 0x00, 0x00)
        assert writes[19] == (1, 0x0C, 0xE0) and writes[20] == (1, 0x08, 0x31)
        assert writes[21] == (2, 0x01, 0x00) and writes[22] == (2, 0x02, 0xFD)
        assert writes[23] == (1, 0x0F, 0x11)
        assert writes[24] == (1, 0x0A, 0x00) and writes[25] == (1, 0x0A, 0x01)

    def test_not_system_controller_and_own_address(self):
        writes = t.register_init_writes(own_address=5, system_controller=False)
        assert writes[15] == (1, 0x1C, 0x02)
        assert writes[17] == (1, 0x0C, 5) and writes[18] == (2, 0x00, 5)

    def test_secondary_address_rows(self):
        writes = t.register_init_writes(secondary=3)
        assert writes[19] == (1, 0x0C, 0x83)
        assert writes[20] == (1, 0x08, 0x32)
        assert writes[21] == (2, 0x01, 0x63)

    @pytest.mark.parametrize('t1_ns, rows', [
        (350, (0xE9, 0xA4, 0x20)), (500, (0xE9, 0xA4, 0x00)), (1100, (0xE9, 0xA0, 0x00)),
        (2000, (0xE1, 0xA0, 0x00)), (1, (0xE9, 0xA4, 0x20)), (9999, (0xE1, 0xA0, 0x00)),
    ])
    def test_t1_rows(self, t1_ns, rows):
        writes = t.register_init_writes(t1_ns=t1_ns)
        assert writes[11] == (1, 0x0A, rows[0])
        assert writes[12] == (1, 0x0A, rows[1])
        assert writes[13] == (1, 0x17, rows[2])

    def test_shutdown_and_ren_writes(self):
        assert t.SHUTDOWN_WRITES == ((1, 0x0A, 0x02), (3, 0x10, 0x00))
        assert t.REN_ON_WRITE == (1, 0x0A, 0x1F) and t.REN_OFF_WRITE == (1, 0x0A, 0x17)

    def test_readiness(self):
        assert p.readiness_reported(h('40 01 00 01 30 01 00 00 00 00 00 00 00 00 00 00')) is False
        assert p.readiness_reported(h('40 01 00 01 30 01 02 03 00 03 96 00 00 00 00 00')) is True
        assert p.readiness_reported(h('40 00 00 00 00 00 0f 00 00 30 00 00 00 00 00 00')) is True
        assert p.readiness_reported(h('40 00 00 00 00 00 00 00 00 00 07')) is True
        assert p.readiness_reported(h('40 01 00')) is False
        with pytest.raises(p.ProtocolError):
            p.readiness_reported(h('41 00 00 00 00 00 00 00 00 00 00'))

    def test_serial_number(self):
        assert p.parse_serial_number(h('41 78 56 34 12')) == 0x12345678
        assert p.parse_serial_number(h('41 78 56 34 12 00 00 00 00 00 00 00 00 00 00 00')) == 0x12345678
        with pytest.raises(p.ProtocolError):
            p.parse_serial_number(h('40 78 56 34 12'))

    def test_hs_plus_requests_in_order(self):
        requests = [request.request for request, _ in t.HS_PLUS_INIT_REQUESTS]
        assert requests == [0x48, 0x4B, 0xF8]
        last, expected = t.HS_PLUS_INIT_REQUESTS[2]
        assert last.request_type == 0xC1 and last.index == 1 and last.length == 9
        assert expected == h('f8 01 00 00 00 01 00 00 00')

    def test_raw_endpoint_pairs(self):
        # §1.2: the alternate pair on the HS family and the HS+; the USB-B has only an alternate IN.
        for pid in (t.PID_HS, t.PID_KUSB_488A, t.PID_MC_USB_488):
            assert (t.MODELS[pid].endpoint_out_raw, t.MODELS[pid].endpoint_in_raw) == (0x06, 0x88)
            assert t.MODELS[pid].raw_endpoints
        assert (t.MODELS[t.PID_HS_PLUS].endpoint_out_raw, t.MODELS[t.PID_HS_PLUS].endpoint_in_raw) == (0x04, 0x85)
        assert not t.MODELS[t.PID_USB_B].raw_endpoints and t.MODELS[t.PID_USB_B].endpoint_in_raw is None

    def test_srq_acknowledge_request(self):
        # §10.4.2: bmRequestType 0x40, bRequest 0x3b, wValue 0, wIndex 0, wLength 0.
        assert (t.SRQ_ACKNOWLEDGE.request_type, t.SRQ_ACKNOWLEDGE.request, t.SRQ_ACKNOWLEDGE.value,
                t.SRQ_ACKNOWLEDGE.index, t.SRQ_ACKNOWLEDGE.length) == (0x40, 0x3B, 0, 0, 0)
        assert t.INTERRUPT_READ_LENGTH == 64

    def test_models_and_endpoints(self):
        assert t.MODELS[t.PID_HS].endpoint_out == 0x02 and t.MODELS[t.PID_HS].endpoint_in == 0x84
        assert t.MODELS[t.PID_HS_PLUS].endpoint_out == 0x01 and t.MODELS[t.PID_HS_PLUS].endpoint_in == 0x82
        assert t.MODELS[t.PID_USB_B].endpoint_in == 0x82 and not t.MODELS[t.PID_USB_B].readiness_poll
        assert t.MODELS[t.PID_USB_B_PRE_FIRMWARE].needs_firmware
        assert t.MODELS[t.PID_KUSB_488A].endpoint_in == t.MODELS[t.PID_MC_USB_488].endpoint_in == 0x84
        assert t.MODELS[t.PID_HS_PLUS].hs_plus_extras and not t.MODELS[t.PID_HS].hs_plus_extras


# ---------------------------------------------------------------------------
# §10: the instructions NI's driver uses, with NI's literal bytes
# ---------------------------------------------------------------------------

class TestRawReadInstruction:
    def test_0x0b_block_of_20480_as_ni_sends_it(self):
        # idn.pcap 0.5160: termination character disabled (m 00, e 0a), code 0xfe.
        assert p.read_raw_block(20480, 0xFE, termchar=0x0A) == h('0b 00 0a fe 00 b0 ff ff')

    def test_0x0b_block_of_4096_with_the_3_s_code(self):
        # counts.pcap 13.4323
        assert p.read_raw_block(4096, 0xFC, termchar=0x0A) == h('0b 00 0a fc 00 f0 ff ff')

    def test_0x0b_block_with_the_termination_character_enabled(self):
        # eos.pcap 0.5172: read_termination = '\n' -> m 14
        assert p.read_raw_block(20480, 0xFE, eos=0x0A, eos_8bit=True, termchar=0x0A) == h('0b 14 0a fe 00 b0 ff ff')

    def test_0x0b_message_is_the_block_plus_the_clear_end_write(self):
        # The two blocks NI puts after its addressing 0x0c (idn.pcap 0.5160), then termination.
        assert p.read_raw_message(20480, 0xFE, termchar=0x0A) == h(
            '0b 00 0a fe 00 b0 ff ff 09 01 00 01 0a 55 00 00 04 00 00 00')

    def test_count32_encoding_and_limits(self):
        assert p.encode_count32(20480) == h('00 b0 ff ff')
        assert p.encode_count32(2050) == h('fe f7 ff ff')
        assert p.encode_count32(1) == h('ff ff ff ff')
        # Up to 0xffff the 32-bit and the 16-bit-plus-ffff readings of the field agree.
        assert p.encode_count32(0xFFFF) == p.encode_count16(0xFFFF) + h('ff ff')
        for bad in (0, 0x10000):
            with pytest.raises(ValueError):
                p.encode_count32(bad)

    def test_decode_count32(self):
        assert p.decode_count32(h('0b 20 64 00 52 b0 ff ff e0 00 00 00'), 4) == -20398
        assert p.decode_count32(h('0b 00 64 00 00 00 00 00 60 00 00 00'), 4) == 0
        assert p.decode_count32(h('0b 00 64 0a 00 b0 ff ff 60 00 00 00'), 4) == -20480


#: idn.pcap 0.5259: the 56-byte reply to NI's five-block read message.
IDN_RAW_REPLY = h(
    '03 00 28 00 00 00 ff ff'
    '0c 00 74 00 00 00 ff ff'
    '0b 20 64 00 52 b0 ff ff e0 00 00 00'
    '09 00 64 00 52 b0 ff ff 01 00 00 00'
    '09 00 64 00 52 b0 ff ff 01 00 00 00'
    '04 00 00 00')
IDN_2420 = b'KEITHLEY INSTRUMENTS INC.,MODEL 2420,1230523,C30   Mar 17 2006 09:29:29/A02  /H/L\n'


class TestRawReadReply:
    def test_idn_reply_as_ni_received_it(self):
        assert len(IDN_2420) == 82
        parsed = p.parse_raw_read_reply(IDN_RAW_REPLY, 20480, IDN_2420)
        assert parsed.data == IDN_2420
        assert parsed.count32 == -20398 == 82 - 20480
        assert parsed.end and parsed.eoi
        assert parsed.status.ibsta == 0x2064 and parsed.status.error == 0

    def test_full_chunk_has_end_clear_and_count_zero(self):
        # trac.pcap 4.5344: 20480 of 20480, more to come.
        reply = h('03 00 28 00 00 00 ff ff 0c 00 74 00 00 00 ff ff'
                  '0b 00 64 00 00 00 00 00 60 00 00 00'
                  '09 00 64 00 00 00 00 00 01 00 00 00 09 00 64 00 00 00 00 00 01 00 00 00 04 00 00 00')
        parsed = p.parse_raw_read_reply(reply, 20480, bytes(20480))
        assert len(parsed.data) == 20480 and parsed.count32 == 0
        assert not parsed.end and not parsed.eoi

    def test_last_chunk_of_the_trace_buffer(self):
        # trac.pcap 12.5002: 328 bytes, END, EOI.
        reply = h('03 00 64 00 00 00 00 00 0c 00 74 00 00 00 00 00'
                  '0b 20 64 00 48 b1 ff ff e0 00 00 00'
                  '09 00 64 00 48 b1 ff ff 01 00 00 00 09 00 64 00 48 b1 ff ff 01 00 00 00 04 00 00 00')
        parsed = p.parse_raw_read_reply(reply, 20480, b'x' * 328)
        assert len(parsed.data) == 328 and parsed.count32 == -20152 and parsed.end

    def test_timeout_with_nothing_read(self):
        # nolistener.pcap 9.8126: error 0x0a, count -20480, tail 0x60; the 0x88 transfer was empty.
        reply = h('03 00 28 00 f9 ff ff ff 0c 00 74 00 00 00 ff ff'
                  '0b 00 64 0a 00 b0 ff ff 60 00 00 00'
                  '09 00 64 00 00 b0 ff ff 01 00 00 00 09 00 64 00 00 b0 ff ff 01 00 00 00 04 00 00 00')
        parsed = p.parse_raw_read_reply(reply, 20480, b'')
        assert parsed.data == b'' and parsed.status.error == 0x0A and not parsed.end and not parsed.eoi

    def test_transfer_longer_than_the_count_is_truncated(self):
        # trac.pcap 13.0075 / 13.0079: 6 bytes on 0x88, count says 5.
        reply = h('03 00 68 00 00 00 00 00 0c 00 74 00 00 00 00 00'
                  '0b 20 64 00 05 b0 ff ff e0 00 00 00'
                  '09 00 64 00 05 b0 ff ff 01 00 00 00 09 00 64 00 05 b0 ff ff 01 00 00 00 04 00 00 00')
        parsed = p.parse_raw_read_reply(reply, 20480, h('31 31 30 33 0a 00'))
        assert parsed.data == b'1103\n'

    def test_our_own_two_block_reply(self):
        reply = h('0b 20 64 00 52 b0 ff ff e0 00 00 00 09 00 64 00 52 b0 ff ff 01 00 00 00 04 00 00 00')
        assert p.parse_raw_read_reply(reply, 20480, IDN_2420).data == IDN_2420

    def test_fewer_bytes_than_the_count_raises(self):
        with pytest.raises(p.ProtocolError):
            p.parse_raw_read_reply(IDN_RAW_REPLY, 20480, IDN_2420[:-1])

    def test_count_outside_the_request_raises(self):
        with pytest.raises(p.ProtocolError):
            p.parse_raw_read_reply(IDN_RAW_REPLY, 50, IDN_2420)  # 50 - 20398 < 0

    def test_missing_or_duplicate_0x0b_block_raises(self):
        with pytest.raises(p.ProtocolError):
            p.parse_raw_read_reply(h('03 00 28 00 00 00 ff ff 04 00 00 00'), 8, b'')
        twice = h('0b 00 64 00 00 00 00 00 60 00 00 00') * 2 + h('04 00 00 00')
        with pytest.raises(p.ProtocolError):
            p.parse_raw_read_reply(twice, 8, bytes(8))

    @pytest.mark.parametrize('requested, expected', [(4096, 4608), (20480, 20992), (4097, 4608), (0xFFFF, 65536)])
    def test_raw_buffer_is_at_least_one_packet_larger_than_the_request(self, requested, expected):
        assert p.raw_read_buffer_size(requested, 512) == expected


class TestSplitReplyBlocks:
    def test_ni_five_block_reply(self):
        ids = [block_id for block_id, _ in p.split_reply_blocks(IDN_RAW_REPLY)]
        assert ids == [0x03, 0x0C, 0x0B, 0x09, 0x09]

    def test_pad_blocks_and_extended_data_blocks(self):
        # counts.pcap 2.9767: count 16, four 0x11 blocks before the 0x37 block.
        reply = h('03 00 68 00 00 00 ff ff 0c 00 74 00 00 00 ff ff'
                  '11 00 00 00 11 00 00 00 11 00 00 00 11 00 00 00'
                  '37 00 4b 45 49 54 48 4c 45 59 20 49 4e 53 54 52 55 4d 00 00 00 00 00 00 00 00 00 00 00 00 00 00'
                  '38 00 64 00 00 00 ff ff 60 10 00 00'
                  '09 00 64 00 00 00 ff ff 01 00 00 00 04 00 00 00')
        blocks = p.split_reply_blocks(reply)
        assert [block_id for block_id, _ in blocks] == [0x03, 0x0C, 0x11, 0x11, 0x11, 0x11, 0x37, 0x38, 0x09]
        assert sum(len(block) for _, block in blocks) == len(reply) - 4

    def test_stops_at_the_termination_block(self):
        assert p.split_reply_blocks(h('04 00 00 00 ff ff')) == []

    def test_missing_termination_raises(self):
        with pytest.raises(p.ProtocolError):
            p.split_reply_blocks(h('03 00 28 00 00 00 ff ff'))

    def test_unknown_id_raises(self):
        with pytest.raises(p.ProtocolError):
            p.split_reply_blocks(h('7f 00 00 00 04 00 00 00'))

    def test_cut_short_block_raises(self):
        with pytest.raises(p.ProtocolError):
            p.split_reply_blocks(h('0b 20 64 00 52 b0'))


class TestFramedReadReplyWithPadBlocks:
    def test_pad_blocks_before_the_data_are_skipped(self):
        # counts.pcap 2.9767 minus the leading 0x03 / 0x0c blocks (ours are never batched).
        reply = h('11 00 00 00 11 00 00 00 11 00 00 00 11 00 00 00'
                  '37 00 4b 45 49 54 48 4c 45 59 20 49 4e 53 54 52 55 4d 00 00 00 00 00 00 00 00 00 00 00 00 00 00'
                  '38 00 64 00 00 00 ff ff 60 10 00 00 04 00 00 00')
        assert p.read_status_offset(reply) == 48
        parsed = p.parse_read_reply(reply, 16)
        assert parsed.data == b'KEITHLEY INSTRUM' and not parsed.end

    def test_timed_out_0x37_read_carries_only_pad_blocks(self):
        # partial.pcap 5.2418, without the batched 0x03 / 0x0c / 0x09 blocks.
        reply = h('11 00 00 00 11 00 00 00 11 00 00 00 11 00 00 00'
                  '38 00 64 0a 38 ff ff ff e0 1e 00 00 04 00 00 00')
        parsed = p.parse_read_reply(reply, 200)
        assert parsed.data == b'' and parsed.status.error == 0x0A

    def test_timed_out_0x36_read_carries_one_zero_block(self):
        # eos.pcap 35.1236: count 10, error 0x0a, one zero-filled 0x36 block, 0 valid.
        reply = h('36 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00'
                  '38 00 64 0a f6 ff ff ff e0 00 00 00 04 00 00 00')
        parsed = p.parse_read_reply(reply, 10)
        assert parsed.data == b'' and parsed.status.error == 0x0A


class TestRawWriteInstruction:
    def test_0x0e_block_of_2050_as_ni_sends_it(self):
        # longwrite.pcap 1.8914: code 0xfe, termination character 0x0a, EOI.
        assert p.write_raw_block(2050, 0xFE, True, eos_char=0x0A) == h('0e 00 00 fe 00 0a 08 00 fe f7 ff ff')

    def test_0x0e_without_eoi_and_without_a_termination_character(self):
        assert p.write_raw_block(2050, 0xFC, False) == h('0e 00 00 fc 00 00 00 00 fe f7 ff ff')

    def test_0x0e_message_is_the_block_plus_termination(self):
        assert p.write_raw_message(2050, 0xFE, True, 0x0A) == h('0e 00 00 fe 00 0a 08 00 fe f7 ff ff 04 00 00 00')

    def test_0x0d_block_with_the_termination_character_as_ni_sends_it(self):
        # idn.pcap 0.5133: ``*IDN?\r\n``, code 0xfe, e = 0x0a, EOI.
        assert p.write_block(b'*IDN?\r\n', 0xFE, True, eos_char=0x0A) == h(
            '0d f9 ff fe 00 0a 08 00 2a 49 44 4e 3f 0d 0a')
        # The bench-proven form keeps byte 5 at zero.
        assert p.write_block(b'*IDN?\n', 0xFC, True) == h('0d fa ff fc 00 00 08 00 2a 49 44 4e 3f 0a')

    def test_0x0e_reply(self):
        # longwrite.pcap 2.0354
        reply = h('03 00 30 00 00 00 ff ff 0c 00 38 00 00 00 ff ff'
                  '0e 00 28 00 00 00 00 00 09 00 28 00 00 00 00 00 01 00 00 00 04 00 00 00')
        parsed = p.parse_raw_write_reply(reply)
        assert parsed.count32 == 0 and parsed.transferred(2050) == 2050
        assert parsed.status.ibsta == 0x0028 and parsed.status.error == 0

    def test_0x0e_reply_with_a_short_count(self):
        reply = h('0e 00 28 08 f9 ff ff ff 04 00 00 00')
        parsed = p.parse_raw_write_reply(reply)
        assert parsed.status.error == 8 and parsed.transferred(7) == 0


class TestSerialPollInstruction:
    def test_0x10_message_as_ni_sends_it(self):
        # stb.pcap 0.5134: primary 24, no secondary, code 0xfe, x = 0.
        assert p.serial_poll_block(24, 0xFE) == h('10 01 00 00 18 00 fe 00')
        assert p.serial_poll_message(24, 0xFE) == h('10 01 00 00 18 00 fe 00 04 00 00 00')

    def test_flag_and_secondary_address(self):
        # srq_poll.pcap 2.5361 carried x = 1; the secondary byte follows the 0x02 probe's form.
        assert p.serial_poll_block(24, 0xFE, flag=1) == h('10 01 00 01 18 00 fe 00')
        assert p.serial_poll_block(24, 0xFC, sad=1) == h('10 01 00 00 18 61 fc 00')
        with pytest.raises(ValueError):
            p.serial_poll_block(31, 0xFE)
        with pytest.raises(ValueError):
            p.serial_poll_block(24, 0xFE, flag=2)

    def test_0x10_reply_with_status_byte_32(self):
        # srq_poll.pcap 2.5379
        reply = h('03 00 74 00 00 00 ff ff 3a 18 00 20 39 00 74 00 00 00 ff ff'
                  '09 00 74 00 00 00 ff ff 01 00 00 00 04 00 00 00')
        parsed = p.parse_serial_poll_reply(reply)
        assert parsed.status_byte == 0x20 and parsed.pad == 24 and parsed.sad_byte == 0
        assert parsed.status.id == 0x39 and parsed.status.ibsta == 0x0074 and parsed.status.error == 0

    def test_0x10_reply_with_status_byte_0_and_our_bare_form(self):
        # stb.pcap 0.5151 carries ``3a 18 00 00``; a bare 0x10 gets the two blocks and termination.
        parsed = p.parse_serial_poll_reply(h('3a 18 00 00 39 00 74 00 00 00 ff ff 04 00 00 00'))
        assert parsed.status_byte == 0

    def test_0x10_reply_missing_a_block_raises(self):
        with pytest.raises(p.ProtocolError):
            p.parse_serial_poll_reply(h('3a 18 00 00 04 00 00 00'))
        with pytest.raises(p.ProtocolError):
            p.parse_serial_poll_reply(h('39 00 74 00 00 00 ff ff 04 00 00 00'))


class TestSrqPush:
    def test_push_as_captured(self):
        # srq.pcap 1.5266 and srq_poll.pcap 1.0308: status byte 0x60 = RQS | ESB.
        push = p.parse_srq_push(h('30 18 00 60 31 a1 01 00'))
        assert push.ibsta == 0x1800 and push.srqi and push.status_byte == 0x60
        assert push.raw == h('30 18 00 60 31 a1 01 00')

    def test_a_64_byte_read_that_returned_more_is_cut_to_the_push(self):
        assert p.parse_srq_push(h('30 18 00 40 31 a1 01 00') + bytes(56)).status_byte == 0x40

    def test_short_push_raises(self):
        with pytest.raises(p.ProtocolError):
            p.parse_srq_push(h('30 18 00'))


class TestStatusSnapshotBlock:
    def test_bytes(self):
        assert p.status_snapshot_block() == h('03 00 00 00')
        assert p.build_message(p.status_snapshot_block(), p.command_block(bytes((0x3F, 0x20, 0x58)), 0xFD),
                               p.read_raw_block(20480, 0xFE, termchar=0x0A),
                               p.register_write_block(p.READ_RAW_FOLLOWING_WRITES),
                               p.register_write_block([(2, 0x03, 0x01)])) == h(
            '03 00 00 00 0c fd 00 fd 3f 20 58 00 0b 00 0a fe 00 b0 ff ff'
            '09 01 00 01 0a 55 00 00 09 01 00 02 03 01 00 00 04 00 00 00')  # idn.pcap 0.5160, all 40 bytes

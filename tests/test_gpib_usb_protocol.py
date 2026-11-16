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

    def test_read_reply_abcde(self):
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
        assert parsed.status.id == 0x38
        assert parsed.status.count == 0xFF06
        assert parsed.status.bytes_not_transferred == 250
        assert parsed.status.transferred(256) == 6
        assert parsed.adr1 == 0xAA
        assert parsed.embedded_status.id == 0x09
        assert p.read_status_offset(reply) == 16

    def test_register_read_bsr_reply(self):
        assert p.parse_register_read_reply(h('34 5a 00 00 35 01 00 00 04 00 00 00'), 1) == [0x5A]
        assert p.parse_register_read_reply(h('34 5a 00 00 35 01 00 00'), 1) == [0x5A]

    def test_take_control_reply(self):
        status = p.parse_status_reply(h('01 00 30 00 00 00 00 00 04 00 00 00'), p.OP_TAKE_CONTROL)
        assert status.cic and status.atn


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
        (5, 'no acceptor on the bus'), (8, 'no listener addressed'), (10, 'device-side timeout'),
        (6, 'unknown'), (7, 'unknown'), (11, 'unknown'),
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
               block: int = 15) -> bytes:
    """Build a 0x0a reply the way the device lays it out."""
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
               + count.to_bytes(2, 'little') + b'\x00\x00'
               + bytes((0x80 if end else 0x00, last_count, 0, 0))
               + h('09 00 00 00 00 00 00 00') + h('02 00 00 00') + h('04 00 00 00'))
    return b''.join(blocks) + trailer


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
        assert p.read_status_offset(parsed and read_reply(b'', 256, end=False)) == 0

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

    def test_models_and_endpoints(self):
        assert t.MODELS[t.PID_HS].endpoint_out == 0x02 and t.MODELS[t.PID_HS].endpoint_in == 0x84
        assert t.MODELS[t.PID_HS_PLUS].endpoint_out == 0x01 and t.MODELS[t.PID_HS_PLUS].endpoint_in == 0x82
        assert t.MODELS[t.PID_USB_B].endpoint_in == 0x82 and not t.MODELS[t.PID_USB_B].readiness_poll
        assert t.MODELS[t.PID_USB_B_PRE_FIRMWARE].needs_firmware
        assert t.MODELS[t.PID_KUSB_488A].endpoint_in == t.MODELS[t.PID_MC_USB_488].endpoint_in == 0x84
        assert t.MODELS[t.PID_HS_PLUS].hs_plus_extras and not t.MODELS[t.PID_HS].hs_plus_extras

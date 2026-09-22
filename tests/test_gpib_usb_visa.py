"""The pyvisa-py session end to end: install, list, open, query, dispatch.

A behavioural fake adapter answers each protocol message the way the real one
would (readiness, register writes, addressing, data in and out, the NDAC
presence probe), holding one fake instrument at address 24 that answers
``*IDN?``. That lets pyvisa drive the whole stack: ``ResourceManager("@py")``,
``list_resources``, ``open_resource``, ``query``.
"""
import sys
import threading
from typing import List

import pytest

pyvisa_py = pytest.importorskip('pyvisa_py')
# pyvisa-py 0.8 requires Python 3.10, so a 3.9 run resolves to a release whose
# Session API predates what the session is written against. The driver itself
# degrades (install() catches the ImportError and registers nothing); there is
# nothing here to test on such a run.
if not hasattr(pytest.importorskip('pyvisa_py.sessions'), 'OpenError'):
    pytest.skip('pyvisa-py predates the Session API this needs (0.8+)',
                allow_module_level=True)

import pyvisa  # noqa: E402
from pyvisa import constants  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402
from pyvisa_py.sessions import Session  # noqa: E402

import resistamet_gui.gpib_usb as gpib_usb  # noqa: E402
from resistamet_gui.gpib_usb import protocol as p  # noqa: E402
from resistamet_gui.gpib_usb import tables as t  # noqa: E402
from resistamet_gui.gpib_usb import controller as controller_module  # noqa: E402
from resistamet_gui.gpib_usb import transport, visa_session  # noqa: E402
from resistamet_gui.gpib_usb import boards  # noqa: E402
from resistamet_gui.gpib_usb.boards import BoardRegistry  # noqa: E402
from resistamet_gui.gpib_usb.transport import AdapterInfo, TransportError  # noqa: E402
from resistamet_gui.gpib_usb.visa_session import GPIB_INSTR, NiUsbGpibDispatch  # noqa: E402
from tests.fakes.gpib_usb import FakeInstrument, SimulatedAdapter, fake_adapter_info, h  # noqa: E402
from tests.fakes.gpib_usb_visa import (Sentinel, enumeration, ni_instructions,  # noqa: E402,F401
                                       session_registry, switch_unset)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def framed_counts(adapter: SimulatedAdapter) -> List[int]:
    """The requested count of every framed 0x0a the adapter has seen, in order."""
    return [0x10000 - int.from_bytes(m[4:6], 'little') for m in adapter.instructions(p.OP_READ)]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def adapter(monkeypatch, session_registry, enumeration):
    """One fake HS at GPIB0 with a Keithley-like instrument at address 24, installed."""
    sim = SimulatedAdapter({24: FakeInstrument('KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30')})
    monkeypatch.setattr(gpib_usb, 'available', lambda: True)
    monkeypatch.setattr(controller_module, 'IFC_SETTLE_S', 0.0)  # the fake needs no settle
    monkeypatch.setattr(visa_session, '_REGISTRY',
                        BoardRegistry(open_transport=lambda i: sim, first_board=0))
    session_registry[GPIB_INSTR] = Sentinel
    gpib_usb.install()
    return sim


@pytest.fixture
def rm(adapter):
    # The class itself, not the ``pyvisa.ResourceManager`` name: the --simulate
    # machinery rebinds that name process-wide and other test modules leave it so.
    manager = pyvisa.highlevel.ResourceManager('@py')
    yield manager
    manager.close()


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

class TestInstall:
    def test_dispatcher_wraps_the_previous_class(self, adapter):
        assert Session._session_classes[GPIB_INSTR] is NiUsbGpibDispatch
        assert NiUsbGpibDispatch.previous is Sentinel

    def test_install_is_idempotent(self, adapter):
        gpib_usb.install()
        gpib_usb.install()
        assert Session._session_classes[GPIB_INSTR] is NiUsbGpibDispatch
        assert NiUsbGpibDispatch.previous is Sentinel

    def test_install_is_a_no_op_without_libusb(self, monkeypatch, session_registry):
        monkeypatch.setattr(gpib_usb, 'available', lambda: False)
        session_registry[GPIB_INSTR] = Sentinel
        gpib_usb.install()
        assert session_registry[GPIB_INSTR] is Sentinel


class TestListResources:
    def test_lists_the_instrument_found_by_the_probe(self, rm, adapter):
        resources = rm.list_resources()
        assert 'GPIB0::24::INSTR' in resources
        assert not [r for r in resources if r.startswith('GPIB0::') and r != 'GPIB0::24::INSTR']

    def test_merges_the_previous_class_listing(self, rm):
        assert 'GPIB9::1::INSTR' in rm.list_resources()

    def test_probing_attaches_then_releases_the_board(self, rm, adapter):
        rm.list_resources()
        assert adapter.control_requests[:2] == [0x41, 0x40]
        assert adapter.messages[0] == p.register_write_message(t.register_init_writes())
        assert adapter.messages[-1] == p.register_write_message(t.SHUTDOWN_WRITES)
        assert adapter.closed

    def test_previous_listing_failure_does_not_hide_our_boards(self, rm, monkeypatch):
        def boom():
            raise RuntimeError('linux-gpib exploded')
        monkeypatch.setattr(Sentinel, 'list_resources', staticmethod(boom))
        assert 'GPIB0::24::INSTR' in rm.list_resources()

    def test_listing_while_a_session_is_open_keeps_the_board_attached(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        assert 'GPIB0::24::INSTR' in rm.list_resources()
        assert adapter.control_requests.count(0x41) == 1
        assert not adapter.closed
        inst.query('*IDN?')
        inst.close()
        assert adapter.closed


class TestInstrumentSession:
    def test_query_round_trips(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 5000
        assert inst.query('*IDN?') == 'KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30\n'
        write = adapter.instructions(p.OP_WRITE)[-1]
        assert write == p.write_message(b'*IDN?\r\n', 0xFD, send_eoi=True)
        # pyvisa reads in 20480-byte chunks. Unless the raw paths are switched on, that is the
        # framed 0x0a the bench has run, not the 0x0b NI's driver would send (§10.1.1), asking
        # for 1024 at a time, the most a framed read ever asks for (§11.2); the 82-byte answer
        # ends the first piece with END.
        assert adapter.instructions(p.OP_READ_RAW) == []
        assert len(adapter.instructions(p.OP_READ)) == 1
        read = adapter.instructions(p.OP_READ)[-1]
        # Compare off: m 00 and e 00, 10 s code, -1024, then the embedded two-write block.
        assert read == h('0a 00 00 fd 00 fc 00 00 09 02 00 01 0a 51 01 0a 55 00 00 00 04 00 00 00')
        # Addressing: controller talks / instrument listens, then instrument talks.
        commands = adapter.instructions(p.OP_COMMAND)[-2:]
        assert commands[0][4:7] == bytes((0x3F, 0x40, 0x38))
        assert commands[1][4:7] == bytes((0x3F, 0x20, 0x58))
        inst.close()

    def test_query_with_ni_instructions_on_reads_through_0x0b(self, rm, adapter, ni_instructions):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 5000
        assert inst.query('*IDN?') == 'KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30\n'
        # The 20480-byte chunk is then a 0x0b with the data on the alternate endpoint, as it
        # is under NI's driver (§10.1.1).
        assert adapter.instructions(p.OP_READ) == []
        read = adapter.instructions(p.OP_READ_RAW)[-1]
        # Compare off: m 00 and e 00 (the bench-proven form under our AUXRA 0x81 init; NI
        # sends e 0a under its 0x99 init, §10.1.6), 10 s code, -20480.
        assert read[:8] == h('0b 00 00 fd 00 b0 ff ff')
        assert adapter.raw_in_timeouts[-1] == 1000  # the first slice of the 0x88 wait; the data was there
        inst.close()

    def test_read_termination_selects_eos_and_is_stripped(self, rm, adapter, ni_instructions):
        inst = rm.open_resource('GPIB0::24::INSTR', read_termination='\n')
        assert inst.query('*IDN?') == 'KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30'
        read = adapter.instructions(p.OP_READ_RAW)[-1]
        assert read[1:3] == h('14 0a')
        inst.close()

    def test_a_long_write_goes_raw(self, rm, adapter, ni_instructions):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 20000
        inst.write('*CLS;' * 409 + '*CL')  # 2048 + '\r\n' = 2050 bytes, as longwrite.pcap
        assert adapter.instructions(p.OP_WRITE) == []
        header = adapter.instructions(p.OP_WRITE_RAW)[-1]
        # NI's header (longwrite.pcap 1.8914) with e = 0x00 in place of its 0x0a: the character
        # goes into e only with the compare on (see _termchar_byte).
        assert header == h('0e 00 00 fe 00 00 08 00 fe f7 ff ff 04 00 00 00')
        inst.set_visa_attribute(constants.VI_ATTR_TERMCHAR_EN, True)
        inst.write('*CLS;' * 409 + '*CL')
        assert adapter.instructions(p.OP_WRITE_RAW)[-1][5] == 0x0A
        assert adapter.raw_writes[-1] == b'*CLS;' * 409 + b'*CL\r\n'
        assert adapter.instruments[24].received[-1] == adapter.raw_writes[-1]
        inst.close()

    def test_a_long_write_to_an_empty_address_reports_no_listeners(self, rm, adapter, ni_instructions):
        # §10.6.5: the data is refused with a STALL; NI resets 0x06, then 0x02, and carries on.
        inst = rm.open_resource('GPIB0::5::INSTR')
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            inst.write('x' * 3000)
        assert info.value.error_code == StatusCode.error_no_listeners
        assert adapter.halts_cleared == [0x06, 0x02] and not adapter.halted
        assert 0x20 not in adapter.control_requests  # no stop request
        other = rm.open_resource('GPIB0::24::INSTR')  # same board, still attached
        # Expected, not observed: the fake un-halts 0x06 on the reset, but no capture has a second
        # 0x0e after a refused one (§10.6.7) and the raw paths have not run on our adapter. What
        # is pinned is the driver's side: an ordinary 0x0e, with no re-attach in between.
        other.write('y' * 3000)
        assert adapter.raw_writes[-1] == b'y' * 3000 + b'\r\n'
        assert len(adapter.instructions(p.OP_INTERFACE_CLEAR)) == 1
        other.close()
        inst.close()

    def test_plain_reads_send_the_bench_proven_eos_bytes(self, rm, adapter, ni_instructions):
        # The first *IDN? of a bench day, on both read forms: m 00 e 00 with the compare off,
        # whatever VI_ATTR_TERMCHAR holds (pyvisa's default is 0x0a).
        inst = rm.open_resource('GPIB0::24::INSTR')
        assert inst.get_visa_attribute(constants.VI_ATTR_TERMCHAR) == 0x0A
        assert inst.get_visa_attribute(constants.VI_ATTR_TERMCHAR_EN) is False
        inst.write('*IDN?')
        inst.read()
        assert adapter.instructions(p.OP_READ_RAW)[-1] == h('0b 00 00 fc 00 b0 ff ff 09 01 00 01 0a 55 00 00 04 00 00 00')
        inst.chunk_size = 256
        inst.write('*IDN?')
        inst.read()
        assert adapter.instructions(p.OP_READ)[-1] == h(
            '0a 00 00 fc 00 ff 00 00 09 02 00 01 0a 51 01 0a 55 00 00 00 04 00 00 00')  # §3.6 worked example
        inst.close()

    def test_changing_the_termination_character_changes_e_only_when_enabled(self, rm, adapter, ni_instructions):
        # eosmodes.pcap: TERMCHAR 0x2c enabled -> 14 2c. Disabled, we keep 00 00 (see _termchar_byte).
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.set_visa_attribute(constants.VI_ATTR_TERMCHAR, 0x2C)
        inst.write('*IDN?')
        assert inst.read() == 'KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30\n'
        assert adapter.instructions(p.OP_READ_RAW)[-1][1:3] == h('00 00')
        inst.set_visa_attribute(constants.VI_ATTR_TERMCHAR_EN, True)
        inst.write('*IDN?')
        assert inst.read() == 'KEITHLEY INSTRUMENTS INC.,'
        assert adapter.instructions(p.OP_READ_RAW)[-1][1:3] == h('14 2c')
        inst.close()

    @pytest.mark.parametrize('value', [None, '0'])
    def test_without_the_environment_switch_every_transfer_is_framed(self, rm, adapter, monkeypatch, value):
        if value is not None:
            monkeypatch.setenv(boards.NI_INSTRUCTIONS_ENV, value)
        inst = rm.open_resource('GPIB0::24::INSTR')
        assert inst.query('*IDN?') == 'KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30\n'
        inst.write('*CLS;' * 500)
        assert adapter.instructions(p.OP_READ_RAW) == [] and adapter.instructions(p.OP_WRITE_RAW) == []
        assert adapter.instructions(p.OP_READ)[-1][4:6] == h('00 fc')     # pyvisa's 20480-byte chunk, framed: 1024 per 0x0a
        assert len(adapter.instructions(p.OP_WRITE)[-1]) == 8 + 2502 + 2 + 4
        inst.close()

    def test_the_environment_switch_spellings(self, monkeypatch):
        for value in ('1', 'true', 'Yes', ' on '):
            monkeypatch.setenv(boards.NI_INSTRUCTIONS_ENV, value)
            assert boards.ni_instructions_enabled() is True, value
        for value in ('0', 'false', 'no', 'off', '', 'raw'):
            monkeypatch.setenv(boards.NI_INSTRUCTIONS_ENV, value)
            assert boards.ni_instructions_enabled() is False, value
        monkeypatch.delenv(boards.NI_INSTRUCTIONS_ENV)
        assert boards.ni_instructions_enabled() is False

    def test_the_switch_s_first_name_still_works_and_the_new_name_wins(self, monkeypatch):
        assert boards.RAW_TRANSFERS_ENV == 'RESISTAMET_GPIB_RAW_TRANSFERS'
        assert boards.NI_INSTRUCTIONS_ENV == 'RESISTAMET_GPIB_NI_INSTRUCTIONS'
        monkeypatch.setenv(boards.RAW_TRANSFERS_ENV, '1')
        assert boards.ni_instructions_enabled() is True
        monkeypatch.setenv(boards.NI_INSTRUCTIONS_ENV, '0')
        assert boards.ni_instructions_enabled() is False
        monkeypatch.setenv(boards.RAW_TRANSFERS_ENV, '0')
        monkeypatch.setenv(boards.NI_INSTRUCTIONS_ENV, '1')
        assert boards.ni_instructions_enabled() is True

    def test_the_attach_log_line_says_which_instructions(self, rm, adapter, monkeypatch, caplog):
        with caplog.at_level('INFO', logger='resistamet_gui.gpib_usb.boards'):
            rm.open_resource('GPIB0::24::INSTR').close()
            monkeypatch.setenv(boards.NI_INSTRUCTIONS_ENV, '1')
            rm.open_resource('GPIB0::24::INSTR').close()
        attached = [record.getMessage() for record in caplog.records if 'attached' in record.getMessage()]
        assert len(attached) == 2
        assert 'framed transfers' in attached[0] and '0x10' not in attached[0]
        assert 'raw transfers' in attached[1] and '0x10' in attached[1]

    def test_a_small_chunk_size_reads_through_the_framed_instruction(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.chunk_size = 256
        assert inst.query('*IDN?') == 'KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30\n'
        assert adapter.instructions(p.OP_READ_RAW) == []
        read = adapter.instructions(p.OP_READ)[-1]
        assert read[1:6] == h('00 00 fc 00 ff')  # compare off: 00 00; 3 s default timeout, -256
        inst.close()

    def test_a_3000_byte_answer_through_pyvisa_s_chunk_is_three_framed_pieces(self, rm, adapter):
        # §11.2: a 20480-byte 0x0a whose answer ran long wedged the bench adapter. The framed
        # path asks for 1024 per instruction (§10.1.1) and loops inside the controller, so
        # pyvisa's one 20480-byte chunk is three 0x0a after one addressing, and pyvisa sees
        # one read ending on END.
        answer = bytes(range(256)) * 11 + b'\n' * 184
        adapter.instruments[24].pending = answer
        inst = rm.open_resource('GPIB0::24::INSTR')
        commands = len(adapter.instructions(p.OP_COMMAND))
        assert inst.read_raw() == answer
        assert framed_counts(adapter) == [1024, 1024, 1024]
        assert adapter.instructions(p.OP_READ_RAW) == []
        assert len(adapter.instructions(p.OP_COMMAND)) == commands + 1   # addressed to talk once, not per piece
        inst.close()

    def test_read_bytes_of_3000_asks_for_1024_1024_and_952(self, rm, adapter):
        answer = bytes(range(256)) * 11 + bytes(range(184))
        adapter.instruments[24].pending = answer
        inst = rm.open_resource('GPIB0::24::INSTR')
        assert inst.read_bytes(3000) == answer
        assert framed_counts(adapter) == [1024, 1024, 952]
        # Compare off, 3 s default code, -952 = 0xfc48, the embedded two-write block.
        assert adapter.instructions(p.OP_READ)[-1] == h('0a 00 00 fc 48 fc 00 00 09 02 00 01 0a 51 01 0a 55 00 00 00 04 00 00 00')
        inst.close()

    def test_a_timeout_on_the_second_piece_is_error_timeout_with_the_first_piece_at_the_session(self, rm, adapter):
        first = bytes(range(256)) * 4
        adapter.instruments[24].pending = first
        adapter.withhold_eoi = True   # exactly one piece, and no EOI with its last byte
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 100
        session = inst.visalib.sessions[inst.session]
        assert session.read(3000) == (first, StatusCode.error_timeout)
        assert framed_counts(adapter) == [1024, 1024]
        # Through pyvisa the same read is VI_ERROR_TMO, as any timed-out read is.
        adapter.instruments[24].pending = first
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            inst.read_bytes(3000)
        assert info.value.error_code == StatusCode.error_timeout
        assert framed_counts(adapter) == [1024, 1024] * 2
        inst.close()

    def test_timeout_attribute_reaches_the_instruction(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 300
        inst.write('*IDN?')
        assert adapter.instructions(p.OP_WRITE)[-1][3] == 0xFA
        inst.timeout = 20000
        inst.write('*IDN?')
        assert adapter.instructions(p.OP_WRITE)[-1][3] == 0xFE
        inst.close()

    def test_timeout_is_rounded_to_the_device_table(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 5000
        assert inst.timeout == 10000
        inst.timeout = 2000
        assert inst.timeout == 3000
        inst.timeout = 2_000_000
        assert inst.timeout == 1_000_000
        inst.write('*IDN?')
        assert adapter.instructions(p.OP_WRITE)[-1][3] == 0x02
        # The host waited 1.25 times the expiry inferred for the 1000 s code, 2^30 us (§7.3:
        # the second unit's factor over the larger of nominal and the power of two), plus 2 s,
        # plus 1 ms for each of the 7 bytes of the write.
        assert adapter.bulk_in_timeouts[-1] == 1_344_177 + 7
        inst.close()

    def test_an_immediate_timeout_goes_out_as_the_shortest_code_seen_on_the_wire(self, rm, adapter):
        # VI_TMO_IMMEDIATE has no device analogue. The table's shortest row, 10 us, goes into
        # the addressing as well, where no handshake can finish in it: every operation would
        # time out. 0xf9 (100 ms) is the shortest code captured and the shortest one timed.
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 0
        assert inst.write('*IDN?') == 7
        assert adapter.instructions(p.OP_COMMAND)[-1][3] == 0xF9
        assert adapter.instructions(p.OP_WRITE)[-1][3] == 0xF9
        inst.close()

    def test_host_wait_outlasts_the_expiry_of_the_code_sent(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 5000
        inst.write('*IDN?')
        assert adapter.instructions(p.OP_WRITE)[-1][3] == 0xFD
        # 20.0 s measured for 0xfd on the bench unit (§7.3; 16.778 s on the captured one) + 2 s, + 7 bytes
        assert adapter.bulk_in_timeouts[-1] == 22000 + 7
        inst.close()

    def test_no_response_is_a_visa_timeout(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 100
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            inst.read()
        assert info.value.error_code == StatusCode.error_timeout
        inst.close()

    def test_no_response_with_the_bench_units_stale_block_is_a_visa_timeout(self, rm, adapter):
        # The 10-byte reply of §5.2 (GPIB-USB-HS 01CEE482), through pyvisa: VI_ERROR_TMO with no
        # stop request and no re-attach, where sizing the data by the last-block byte gave
        # VI_ERROR_IO after a resync.
        adapter.stale_timeout_block = True
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 100
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            inst.read_bytes(10)
        assert info.value.error_code == StatusCode.error_timeout
        assert adapter.instructions(p.OP_READ)[-1][4:6] == h('f6 ff')   # the count the reply answers
        assert 0x20 not in adapter.control_requests
        assert len(adapter.instructions(p.OP_INTERFACE_CLEAR)) == 1   # the one attach; no re-attach
        inst.close()

    def test_read_stb_of_an_absent_device_is_a_timeout(self, rm, adapter):
        inst = rm.open_resource('GPIB0::5::INSTR')
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            inst.read_stb()
        assert info.value.error_code == StatusCode.error_timeout
        assert not adapter.serial_poll_mode  # SPD went out although the read failed
        inst.close()

    def test_read_stb_with_ni_instructions_on_is_one_0x10(self, rm, adapter, ni_instructions):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 1000
        adapter.instruments[24].status_byte = 0x40
        commands_before = len(adapter.instructions(p.OP_COMMAND))
        assert inst.read_stb() == 0x40
        # One 0x10 instruction (§10.5.4), no SPE / SPD command bytes and no read.
        assert adapter.instructions(p.OP_SERIAL_POLL)[-1] == h('10 01 00 00 18 00 fb 00 04 00 00 00')
        assert len(adapter.instructions(p.OP_COMMAND)) == commands_before
        assert adapter.instructions(p.OP_READ) == [] and adapter.instructions(p.OP_READ_RAW) == []
        absent = rm.open_resource('GPIB0::5::INSTR')
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            absent.read_stb()
        assert info.value.error_code == StatusCode.error_timeout
        absent.close()
        inst.close()

    def test_write_to_an_empty_address_reports_no_listeners(self, rm, adapter):
        inst = rm.open_resource('GPIB0::5::INSTR')
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            inst.write('*IDN?')
        assert info.value.error_code == StatusCode.error_no_listeners
        inst.close()

    def test_usb_fault_is_error_io_and_the_adapter_recovers(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        adapter.fail_next = TransportError('pipe stalled')
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            inst.write('*IDN?')
        assert info.value.error_code == StatusCode.error_io
        assert inst.query('*IDN?').startswith('KEITHLEY')
        assert adapter.control_requests.count(0x41) == 2  # attach ran again first
        inst.close()

    def test_clear_sends_selected_device_clear_with_the_session_timeout(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 300
        inst.clear()
        last = adapter.instructions(p.OP_COMMAND)[-1]
        assert last[4:7] == bytes((0x3F, 0x38, 0x04))
        assert last[3] == 0xFA
        assert adapter.instruments[24].cleared == 1
        inst.close()

    def test_gpib_attributes(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        assert inst.primary_address == 24
        assert inst.secondary_address == constants.VI_NO_SEC_ADDR
        assert inst.get_visa_attribute(constants.VI_ATTR_INTF_NUM) == 0
        assert inst.get_visa_attribute(constants.VI_ATTR_INTF_TYPE) == constants.InterfaceType.gpib
        assert inst.get_visa_attribute(constants.VI_ATTR_RSRC_CLASS) == 'INSTR'
        assert inst.get_visa_attribute(constants.VI_ATTR_RSRC_NAME) == 'GPIB0::24::INSTR'
        assert inst.get_visa_attribute(constants.VI_ATTR_GPIB_READDR_EN) is True
        inst.set_visa_attribute(constants.VI_ATTR_GPIB_READDR_EN, False)
        assert inst.get_visa_attribute(constants.VI_ATTR_GPIB_READDR_EN) is False
        assert inst.send_end is True
        inst.send_end = False
        inst.write('*IDN?')
        assert adapter.instructions(p.OP_WRITE)[-1][6] == 0x00
        inst.close()

    def test_address_attributes_can_be_changed(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.set_visa_attribute(constants.VI_ATTR_GPIB_PRIMARY_ADDR, 5)
        inst.set_visa_attribute(constants.VI_ATTR_GPIB_SECONDARY_ADDR, 3)
        assert inst.primary_address == 5 and inst.secondary_address == 3
        with pytest.raises(pyvisa.errors.VisaIOError):
            inst.write('*IDN?')  # nobody at 5
        assert adapter.instructions(p.OP_COMMAND)[-1][4:8] == bytes((0x3F, 0x40, 0x25, 0x63))
        inst.set_visa_attribute(constants.VI_ATTR_GPIB_SECONDARY_ADDR, constants.VI_NO_SEC_ADDR)
        assert inst.secondary_address == constants.VI_NO_SEC_ADDR
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            inst.set_visa_attribute(constants.VI_ATTR_GPIB_PRIMARY_ADDR, 31)
        assert info.value.error_code == StatusCode.error_nonsupported_attribute_state
        with pytest.raises(pyvisa.errors.VisaIOError):
            inst.set_visa_attribute(constants.VI_ATTR_GPIB_SECONDARY_ADDR, 40)
        inst.close()

    def test_suppress_end_true_is_rejected(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        assert inst.get_visa_attribute(constants.VI_ATTR_SUPPRESS_END_EN) is False
        inst.set_visa_attribute(constants.VI_ATTR_SUPPRESS_END_EN, False)
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            inst.set_visa_attribute(constants.VI_ATTR_SUPPRESS_END_EN, True)
        assert info.value.error_code == StatusCode.error_nonsupported_attribute_state
        inst.close()

    def test_secondary_address_in_the_resource_name(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::3::INSTR')
        assert inst.secondary_address == 3
        inst.write('*IDN?')
        assert adapter.instructions(p.OP_COMMAND)[-1][4:8] == bytes((0x3F, 0x40, 0x38, 0x63))
        inst.close()

    def test_board_is_shared_and_closed_with_the_last_session(self, rm, adapter):
        first = rm.open_resource('GPIB0::24::INSTR')
        second = rm.open_resource('GPIB0::24::INSTR')
        assert adapter.control_requests.count(0x41) == 1  # attached once
        first.close()
        assert not adapter.closed
        second.close()
        assert adapter.closed
        assert adapter.messages[-1] == p.register_write_message(t.SHUTDOWN_WRITES)

    def test_read_stb_and_trigger_carry_the_session_timeout(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 1000
        adapter.instruments[24].status_byte = 0x40
        assert inst.read_stb() == 0x40
        # The IEEE-488.1 sequence of §5.9, the poll that ran on the bench: SPE with the
        # addressing, a one-byte framed read, SPD UNT; the session's code in each.
        commands = adapter.instructions(p.OP_COMMAND)
        assert commands[-2][3:8] == bytes((0xFB, 0x3F, 0x20, 0x18, 0x58))
        assert commands[-1][4:6] == bytes((0x19, 0x5F)) and commands[-1][3] == 0xFB
        assert adapter.instructions(p.OP_READ)[-1][1:6] == h('00 00 fb ff ff')
        assert adapter.instructions(p.OP_SERIAL_POLL) == []
        assert not adapter.serial_poll_mode
        inst.assert_trigger()
        assert adapter.instructions(p.OP_COMMAND)[-1][4:7] == bytes((0x3F, 0x38, 0x08))
        assert adapter.instructions(p.OP_COMMAND)[-1][3] == 0xFB
        inst.close()

    def test_ifc_ren_and_raw_command(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 300
        # pyvisa puts send_ifc on GPIBInterface only; the INSTR session still answers it.
        assert inst.visalib.gpib_send_ifc(inst.session) == StatusCode.success
        assert adapter.messages[-1] == p.interface_clear_message()
        inst.control_ren(constants.RENLineOperation.deassert)
        assert adapter.messages[-1] == p.register_write_message([t.REN_OFF_WRITE])
        inst.control_ren(constants.RENLineOperation.address_gtl)
        assert adapter.instructions(p.OP_COMMAND)[-1][4:8] == bytes((0x40, 0x3F, 0x38, 0x01))
        assert adapter.instructions(p.OP_COMMAND)[-1][3] == 0xFA
        inst.visalib.gpib_command(inst.session, b'\x14')
        assert adapter.instructions(p.OP_COMMAND)[-1][4:5] == b'\x14'
        assert adapter.instructions(p.OP_COMMAND)[-1][3] == 0xFA
        inst.close()

    REN_ON = p.register_write_message([t.REN_ON_WRITE])     # 09 01 00 01 0a 1f ..
    REN_OFF = p.register_write_message([t.REN_OFF_WRITE])   # 09 01 00 01 0a 17 ..
    LLO = p.command_message(bytes((0x11,)), 0xFA)           # NI: 0c ff 00 fd 11 00 00 00
    LISTEN_24 = p.command_message(bytes((0x3F, 0x40, 0x38)), 0xFA)
    GTL_24 = p.command_message(bytes((0x40, 0x3F, 0x38, 0x01)), 0xFA)  # NI: 0c fc 00 fd 40 3f 38 01

    @pytest.mark.parametrize('mode, expected', [
        # ren_device.pcap, §10.7.4. NI's 0x0c timeout byte is always 0xfd; ours is the session's.
        (constants.RENLineOperation.asrt, ['REN_ON']),
        (constants.RENLineOperation.asrt_llo, ['REN_ON', 'LLO']),
        # NI's "address" step is the 0x02 probe; ours is the listen addressing (see ren_operation).
        (constants.RENLineOperation.asrt_address, ['REN_ON', 'LISTEN_24']),
        (constants.RENLineOperation.asrt_address_llo, ['REN_ON', 'LISTEN_24', 'LLO']),
        (constants.RENLineOperation.address_gtl, ['GTL_24']),
        (constants.RENLineOperation.deassert_gtl, ['GTL_24', 'REN_OFF']),
        (constants.RENLineOperation.deassert, ['REN_OFF']),
    ])
    def test_every_ren_mode_on_an_instrument_session(self, rm, adapter, mode, expected):
        inst = rm.open_resource('GPIB0::24::INSTR')
        inst.timeout = 300
        before = len(adapter.messages)
        inst.control_ren(mode)
        assert adapter.messages[before:] == [getattr(self, name) for name in expected]
        inst.close()

    def test_go_to_local_through_a_secondary_address(self, rm, adapter):
        inst = rm.open_resource('GPIB0::24::1::INSTR')
        inst.control_ren(constants.RENLineOperation.address_gtl)
        assert adapter.instructions(p.OP_COMMAND)[-1][4:9] == bytes((0x40, 0x3F, 0x38, 0x61, 0x01))
        inst.close()

    def test_unknown_board_is_not_found(self, rm):
        with pytest.raises(pyvisa.errors.VisaIOError):
            rm.open_resource('GPIB3::24::INSTR')
        assert Sentinel.calls == ['GPIB3::24::INSTR']


class TestDispatch:
    def test_other_boards_reach_the_previous_class(self, rm):
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            rm.open_resource('GPIB9::1::INSTR')
        assert info.value.error_code == StatusCode.error_resource_not_found
        assert Sentinel.calls == ['GPIB9::1::INSTR']

    def test_opening_our_board_does_not_re_enumerate_but_a_miss_looks_once(self, rm, enumeration):
        rm.open_resource('GPIB0::24::INSTR').close()
        rm.open_resource('GPIB0::24::INSTR').close()
        assert enumeration['calls'] == 1
        with pytest.raises(pyvisa.errors.VisaIOError):
            rm.open_resource('GPIB9::1::INSTR')
        assert enumeration['calls'] == 2
        assert Sentinel.calls == ['GPIB9::1::INSTR']

    def test_adapter_plugged_in_later_opens_without_a_listing(self, rm, adapter, enumeration):
        enumeration['adapters'].append(fake_adapter_info(serial='LATER', address=9))
        inst = rm.open_resource('GPIB1::24::INSTR')
        assert inst.get_visa_attribute(constants.VI_ATTR_INTF_NUM) == 1
        inst.close()
        assert Sentinel.calls == []

    def test_attach_failure_is_a_visa_error_not_a_crash(self, rm, monkeypatch, adapter):
        def broken(info):
            raise TransportError('no permission')
        monkeypatch.setattr(visa_session, '_REGISTRY', BoardRegistry(open_transport=broken, first_board=0))
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            rm.open_resource('GPIB0::24::INSTR')
        assert info.value.error_code == StatusCode.error_system_error

    def test_attach_failure_closes_the_transport(self, rm, monkeypatch, adapter):
        broken_sim = SimulatedAdapter({}, serial_reply=h('00 00 00 00 00'))
        monkeypatch.setattr(visa_session, '_REGISTRY',
                            BoardRegistry(open_transport=lambda i: broken_sim, first_board=0))
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            rm.open_resource('GPIB0::24::INSTR')
        assert info.value.error_code == StatusCode.error_system_error
        assert broken_sim.closed

    def test_unexpected_exception_while_opening_is_a_visa_error(self, rm, monkeypatch, adapter):
        def broken(info):
            raise RuntimeError('libusb exploded')
        monkeypatch.setattr(visa_session, '_REGISTRY', BoardRegistry(open_transport=broken, first_board=0))
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            rm.open_resource('GPIB0::24::INSTR')
        assert info.value.error_code == StatusCode.error_system_error


class TestBoardRegistry:
    def test_adapters_keep_their_numbers_across_refreshes(self, enumeration, monkeypatch):
        disposed: List[AdapterInfo] = []
        monkeypatch.setattr(transport, 'dispose_adapter', disposed.append)
        first = fake_adapter_info(serial='AAA', bus=1, address=9)
        registry = BoardRegistry(open_transport=lambda i: SimulatedAdapter({}), first_board=0)
        enumeration['adapters'] = [first]
        assert registry.owns('0')
        # A miss looks at the bus again, which hands board 0 a fresh handle.
        assert not registry.owns('1')
        # A second adapter appears at a lower USB address: it must not take GPIB0.
        first_again = fake_adapter_info(serial='AAA', bus=1, address=9)
        second = fake_adapter_info(serial='BBB', bus=1, address=2)
        enumeration['adapters'] = [second, first_again]
        registry.refresh()
        assert registry.board_names() == ['0', '1']
        assert registry.owns('1')
        # Every superseded handle of the re-enumerated adapter, disposed once:
        # one for the miss above, one for this refresh.
        assert [info.serial for info in disposed] == ['AAA', 'AAA']

    def test_a_board_in_use_keeps_its_controller_and_drops_the_duplicate(self, enumeration, monkeypatch):
        disposed: List[AdapterInfo] = []
        monkeypatch.setattr(transport, 'dispose_adapter', disposed.append)
        info = fake_adapter_info(serial='AAA')
        enumeration['adapters'] = [info]
        sims: List[SimulatedAdapter] = []

        def opener(i):
            sims.append(SimulatedAdapter({}))
            return sims[-1]

        registry = BoardRegistry(open_transport=opener, first_board=0)
        controller = registry.acquire('0')
        duplicate = fake_adapter_info(serial='AAA')
        enumeration['adapters'] = [duplicate]
        registry.refresh()
        assert registry.acquire('0') is controller
        assert disposed == [duplicate]
        assert len(sims) == 1
        registry.release('0')
        registry.release('0')
        assert sims[0].closed

    def test_vanished_adapters_are_disposed_and_unplugged_in_use_boards_survive(self, enumeration, monkeypatch):
        disposed: List[AdapterInfo] = []
        monkeypatch.setattr(transport, 'dispose_adapter', disposed.append)
        gone = fake_adapter_info(serial='GONE')
        kept = fake_adapter_info(serial='KEPT', address=6)
        enumeration['adapters'] = [gone, kept]
        registry = BoardRegistry(open_transport=lambda i: SimulatedAdapter({}), first_board=0)
        registry.acquire('1')
        enumeration['adapters'] = []
        registry.refresh()
        assert registry.board_names() == ['1']
        assert disposed == [gone]

    def test_location_identity_when_there_is_no_serial(self, enumeration):
        enumeration['adapters'] = [fake_adapter_info(serial=None, bus=3, address=4)]
        registry = BoardRegistry(open_transport=lambda i: SimulatedAdapter({}), first_board=2)
        assert registry.board_names() == ['2']
        enumeration['adapters'] = [fake_adapter_info(serial=None, bus=3, address=4)]
        registry.refresh()
        assert registry.board_names() == ['2']

    def test_owns_finds_an_adapter_that_appeared_after_the_first_enumeration(self, enumeration):
        enumeration['adapters'] = []
        registry = BoardRegistry(open_transport=lambda i: SimulatedAdapter({}), first_board=0)
        assert registry.owns('0') is False
        assert enumeration['calls'] == 1
        enumeration['adapters'] = [fake_adapter_info(serial='NEW')]
        assert registry.owns('0') is True
        assert enumeration['calls'] == 2
        # A hit does not enumerate; a miss for a board that does not exist does, once, and stays False.
        assert registry.owns('0') is True
        assert enumeration['calls'] == 2
        assert registry.owns('7') is False
        assert enumeration['calls'] == 3

    def test_failed_acquire_re_enumerates_next_time(self, enumeration):
        def broken(info):
            raise TransportError('busy')
        registry = BoardRegistry(open_transport=broken, first_board=0)
        with pytest.raises(TransportError):
            registry.acquire('0')
        calls = enumeration['calls']
        registry.owns('0')
        assert enumeration['calls'] == calls + 1


class SlowCloseAdapter(SimulatedAdapter):
    """The shutdown write blocks until the test lets it go, like a close behind an operation in flight."""

    def __init__(self) -> None:
        super().__init__({})
        self.closing = threading.Event()
        self.may_close = threading.Event()

    def bulk_out(self, data: bytes, timeout_ms: int) -> None:
        if data == p.register_write_message(t.SHUTDOWN_WRITES):
            self.closing.set()
            assert self.may_close.wait(10)
        super().bulk_out(data, timeout_ms)


class TestBoardRegistryLocking:
    @pytest.fixture
    def quiet(self, enumeration, monkeypatch):
        monkeypatch.setattr(controller_module, 'IFC_SETTLE_S', 0.0)
        monkeypatch.setattr(transport, 'dispose_adapter', lambda info: None)

    def test_a_slow_close_does_not_hold_up_the_rest_of_the_registry(self, quiet):
        slow = SlowCloseAdapter()
        registry = BoardRegistry(open_transport=lambda i: slow, first_board=0)
        registry.acquire('0')
        closer = threading.Thread(target=registry.release, args=('0',), daemon=True)
        closer.start()
        assert slow.closing.wait(5)
        names: List[List[str]] = []
        asker = threading.Thread(target=lambda: names.append(registry.board_names()), daemon=True)
        asker.start()
        asker.join(2)
        blocked = asker.is_alive()
        slow.may_close.set()
        closer.join(5)
        asker.join(5)
        assert not blocked and names == [['0']]
        assert slow.closed

    def test_opening_a_board_that_is_closing_waits_for_the_close(self, quiet):
        slow = SlowCloseAdapter()
        opened: List[SimulatedAdapter] = []

        def opener(info):
            if opened:
                assert slow.closed, 'the adapter was opened again before its close had finished'
            opened.append(slow if not opened else SimulatedAdapter({}))
            return opened[-1]

        registry = BoardRegistry(open_transport=opener, first_board=0)
        first = registry.acquire('0')
        closer = threading.Thread(target=registry.release, args=('0',), daemon=True)
        closer.start()
        assert slow.closing.wait(5)
        registry.refresh()               # must not hand the closing board a fresh, unlocked entry
        second: List[object] = []
        again = threading.Thread(target=lambda: second.append(registry.acquire('0')), daemon=True)
        again.start()
        again.join(0.3)
        assert again.is_alive()          # waiting for the close, not opening beside it
        slow.may_close.set()
        closer.join(5)
        again.join(5)
        assert len(opened) == 2 and second and second[0] is not first

    def test_a_release_run_by_the_garbage_collector_inside_an_open_does_not_deadlock(self, quiet, enumeration):
        # Resource.__del__ closes a forgotten session wherever the collector happens to run,
        # which can be on this thread in the middle of acquire().
        enumeration['adapters'] = [fake_adapter_info(serial='AAA'), fake_adapter_info(serial='BBB', address=6)]
        registry = BoardRegistry(open_transport=lambda i: SimulatedAdapter({}), first_board=0)
        registry.acquire('1')
        released: List[str] = []

        def opener(info):
            registry.release('1')            # as a finaliser would, under the registry's lock
            released.append(registry.board_names()[0])
            return SimulatedAdapter({})

        registry._open_transport = opener
        worker = threading.Thread(target=registry.acquire, args=('0',), daemon=True)
        worker.start()
        worker.join(5)
        assert not worker.is_alive() and released == ['0']

    def test_a_failed_open_leaves_no_session_behind(self, quiet):
        attempts: List[int] = []

        def opener(info):
            attempts.append(1)
            if len(attempts) == 1:
                raise TransportError('claimed by another process')
            return SimulatedAdapter({})

        registry = BoardRegistry(open_transport=opener, first_board=0)
        with pytest.raises(TransportError):
            registry.acquire('0')
        controller = registry.acquire('0')
        registry.release('0')
        assert controller._closed  # one session, so one release closes it


class TestAvailability:
    def test_unavailable_without_pyusb(self, monkeypatch):
        monkeypatch.setitem(sys.modules, 'usb', None)
        assert gpib_usb.available() is False
        assert gpib_usb.find_adapters() == []
        assert transport.libusb_backend() is None

    def test_open_transport_refuses_a_firmwareless_usb_b(self):
        info = fake_adapter_info()
        info.product_id = t.PID_USB_B_PRE_FIRMWARE
        info.needs_firmware = True
        with pytest.raises(gpib_usb.AdapterNotReady):
            transport.open_transport(info)

    def test_adapter_label(self):
        assert fake_adapter_info().label == 'GPIB-USB-HS (bus 20 address 5 serial 01234567)'

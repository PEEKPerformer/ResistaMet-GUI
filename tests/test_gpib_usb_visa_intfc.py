"""The ``GPIB<n>::INTFC`` session end to end: install, list, open, drive the bus.

Same behavioural fake adapter as ``test_gpib_usb_visa`` (one fake HS at GPIB0
with an instrument at address 24), driven through ``ResourceManager("@py")``
as a ``GPIBInterface``: IFC, REN, ATN, raw command bytes, data to and from
whoever the caller addressed, the bus line and controller state attributes,
and each operation the specification does not let the session offer.
"""
import re
from typing import List

import pytest

pytest.importorskip('pyvisa_py')
if not hasattr(pytest.importorskip('pyvisa_py.sessions'), 'OpenError'):
    pytest.skip('pyvisa-py predates the Session API this needs (0.8+)',
                allow_module_level=True)

import pyvisa  # noqa: E402
from pyvisa import constants  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402
from pyvisa_py.sessions import OpenError, Session  # noqa: E402

import resistamet_gui.gpib_usb as gpib_usb  # noqa: E402
from resistamet_gui.gpib_usb import controller as controller_module  # noqa: E402
from resistamet_gui.gpib_usb import protocol as p  # noqa: E402
from resistamet_gui.gpib_usb import tables as t  # noqa: E402
from resistamet_gui.gpib_usb import visa_intfc, visa_session  # noqa: E402
from resistamet_gui.gpib_usb.boards import BoardRegistry  # noqa: E402
from resistamet_gui.gpib_usb.transport import TransportError  # noqa: E402
from resistamet_gui.gpib_usb.visa_intfc import (GPIB_INTFC, NiUsbGpibIntfcDispatch,  # noqa: E402
                                                NiUsbGpibIntfcSession)
from resistamet_gui.gpib_usb.visa_session import GPIB_INSTR, NiUsbGpibDispatch  # noqa: E402
from tests.test_gpib_usb_visa import (FakeInstrument, Sentinel, SimulatedAdapter,  # noqa: E402,F401
                                      enumeration, h, session_registry)

UNL, MTA0, MLA0, LAD24, TAD24, UNT = 0x3F, 0x40, 0x20, 0x38, 0x58, 0x5F
REN, ATN = constants.RENLineOperation, constants.ATNLineOperation


class IntfcSentinel(Session):
    """Stands in for whatever pyvisa-py had registered for (gpib, INTFC)."""

    calls: List[str] = []

    def __init__(self, resource_manager_session, resource_name, parsed=None, open_timeout=None):
        IntfcSentinel.calls.append(resource_name)
        raise OpenError(StatusCode.error_resource_not_found)

    @staticmethod
    def list_resources() -> List[str]:
        return ['GPIB9::INTFC']

    def _get_attribute(self, attribute):
        raise NotImplementedError

    def _set_attribute(self, attribute, state):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


@pytest.fixture
def board(monkeypatch, session_registry, enumeration):
    """One fake HS at GPIB0 with an instrument at 24; sentinels in both GPIB slots; installed."""
    IntfcSentinel.calls = []
    sim = SimulatedAdapter({24: FakeInstrument('KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30')})
    monkeypatch.setattr(gpib_usb, 'available', lambda: True)
    monkeypatch.setattr(controller_module, 'IFC_SETTLE_S', 0.0)
    monkeypatch.setattr(visa_session, '_REGISTRY',
                        BoardRegistry(open_transport=lambda i: sim, first_board=0))
    session_registry[GPIB_INSTR] = Sentinel
    session_registry[GPIB_INTFC] = IntfcSentinel
    gpib_usb.install()
    return sim


@pytest.fixture
def rm(board):
    manager = pyvisa.highlevel.ResourceManager('@py')
    yield manager
    manager.close()


@pytest.fixture
def intf(rm):
    interface = rm.open_resource('GPIB0::INTFC')
    yield interface
    interface.close()


def last_command(sim: SimulatedAdapter) -> bytes:
    message = sim.instructions(p.OP_COMMAND)[-1]
    return message[4:4 + (0x100 - message[1])]


# ---------------------------------------------------------------------------
# install / list / dispatch
# ---------------------------------------------------------------------------

class TestInstall:
    def test_dispatcher_wraps_the_previous_class(self, board):
        assert Session._session_classes[GPIB_INTFC] is NiUsbGpibIntfcDispatch
        assert NiUsbGpibIntfcDispatch.previous is IntfcSentinel
        assert Session._session_classes[GPIB_INSTR] is NiUsbGpibDispatch

    def test_install_is_idempotent(self, board):
        gpib_usb.install()
        visa_intfc.install()
        assert Session._session_classes[GPIB_INTFC] is NiUsbGpibIntfcDispatch
        assert NiUsbGpibIntfcDispatch.previous is IntfcSentinel

    def test_install_is_a_no_op_without_libusb(self, monkeypatch, session_registry):
        monkeypatch.setattr(gpib_usb, 'available', lambda: False)
        session_registry[GPIB_INTFC] = IntfcSentinel
        gpib_usb.install()
        assert session_registry[GPIB_INTFC] is IntfcSentinel


class TestListResources:
    def test_lists_one_interface_per_board_without_touching_it(self, board):
        assert NiUsbGpibIntfcSession.list_resources() == ['GPIB0::INTFC']
        assert board.messages == [] and board.control_requests == []

    def test_merges_with_the_previous_class_and_the_instruments(self, rm):
        resources = rm.list_resources('?*')  # the default query keeps only ::INSTR
        assert 'GPIB0::INTFC' in resources
        assert 'GPIB9::INTFC' in resources
        assert 'GPIB0::24::INSTR' in resources

    def test_previous_listing_failure_does_not_hide_our_boards(self, board, monkeypatch):
        def boom():
            raise RuntimeError('linux-gpib exploded')
        monkeypatch.setattr(IntfcSentinel, 'list_resources', staticmethod(boom))
        assert NiUsbGpibIntfcDispatch.list_resources() == ['GPIB0::INTFC']

    def test_a_second_adapter_lists_as_the_next_board(self, board, enumeration):
        from tests.test_gpib_usb_visa import fake_adapter_info
        enumeration['adapters'].append(fake_adapter_info(serial='SECOND', address=9))
        assert NiUsbGpibIntfcSession.list_resources() == ['GPIB0::INTFC', 'GPIB1::INTFC']


class TestDispatch:
    def test_our_board_reaches_our_class(self, rm, board):
        intf = rm.open_resource('GPIB0::INTFC')
        assert isinstance(intf, pyvisa.resources.GPIBInterface)
        assert isinstance(intf.visalib.sessions[intf.session], NiUsbGpibIntfcSession)
        assert intf.get_visa_attribute(constants.VI_ATTR_INTF_NUM) == 0
        assert intf.get_visa_attribute(constants.VI_ATTR_RSRC_CLASS) == 'INTFC'
        assert intf.get_visa_attribute(constants.VI_ATTR_INTF_TYPE) == constants.InterfaceType.gpib
        assert board.control_requests[:2] == [0x41, 0x40]  # attached
        intf.close()
        assert board.closed
        assert IntfcSentinel.calls == []

    def test_other_boards_reach_the_previous_class(self, rm):
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            rm.open_resource('GPIB9::INTFC')
        assert info.value.error_code == StatusCode.error_resource_not_found
        assert IntfcSentinel.calls == ['GPIB9::INTFC']
        assert Sentinel.calls == []

    def test_without_a_previous_class_an_unknown_board_is_not_found(self, rm, monkeypatch):
        monkeypatch.setattr(NiUsbGpibIntfcDispatch, 'previous', None)
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            rm.open_resource('GPIB3::INTFC')
        assert info.value.error_code == StatusCode.error_resource_not_found

    def test_interface_and_instrument_share_the_attached_board(self, rm, board):
        intf = rm.open_resource('GPIB0::INTFC')
        inst = rm.open_resource('GPIB0::24::INSTR')
        assert board.control_requests.count(0x41) == 1
        assert inst.query('*IDN?').startswith('KEITHLEY')
        intf.close()
        assert not board.closed
        inst.close()
        assert board.closed

    def test_installed_over_pyvisa_py_unavailable_class(self, monkeypatch, session_registry, enumeration):
        # What pyvisa-py registers for (gpib, INTFC) on a machine without
        # linux-gpib: a class whose __init__ raises ValueError with the reason,
        # and no listing of its own.
        message = 'Please install linux-gpib (Linux) or gpib-ctypes (Windows, Linux)'
        Session.register_unavailable(constants.InterfaceType.gpib, 'INTFC', message)
        sim = SimulatedAdapter({})
        monkeypatch.setattr(gpib_usb, 'available', lambda: True)
        monkeypatch.setattr(controller_module, 'IFC_SETTLE_S', 0.0)
        monkeypatch.setattr(visa_session, '_REGISTRY',
                            BoardRegistry(open_transport=lambda i: sim, first_board=0))
        session_registry[GPIB_INSTR] = Sentinel
        gpib_usb.install()
        rm = pyvisa.highlevel.ResourceManager('@py')
        try:
            with pytest.raises(ValueError, match=re.escape(message)):
                rm.open_resource('GPIB9::INTFC')
            assert NiUsbGpibIntfcDispatch.list_resources() == ['GPIB0::INTFC']
            intf = rm.open_resource('GPIB0::INTFC')
            assert isinstance(intf.visalib.sessions[intf.session], NiUsbGpibIntfcSession)
            intf.close()
            # Adapter unplugged: the merged listing is empty and the board is
            # no longer ours, so it too falls through to pyvisa-py's class.
            enumeration['adapters'] = []
            assert NiUsbGpibIntfcDispatch.list_resources() == []
            with pytest.raises(ValueError, match=re.escape(message)):
                rm.open_resource('GPIB0::INTFC')
        finally:
            rm.close()

    def test_attach_failure_is_a_visa_error(self, rm, monkeypatch, board):
        def broken(info):
            raise TransportError('no permission')
        monkeypatch.setattr(visa_session, '_REGISTRY', BoardRegistry(open_transport=broken, first_board=0))
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            rm.open_resource('GPIB0::INTFC')
        assert info.value.error_code == StatusCode.error_system_error


# ---------------------------------------------------------------------------
# bus operations
# ---------------------------------------------------------------------------

class TestLines:
    def test_send_ifc(self, intf, board):
        assert intf.send_ifc() == StatusCode.success
        assert board.messages[-1] == p.interface_clear_message()

    def test_ren_assert_and_deassert_are_the_register_writes(self, intf, board):
        intf.control_ren(REN.deassert)
        assert board.messages[-1] == p.register_write_message([t.REN_OFF_WRITE])
        assert intf.get_visa_attribute(constants.VI_ATTR_GPIB_REN_STATE) == constants.LineState.unasserted
        intf.control_ren(REN.asrt)
        assert board.messages[-1] == p.register_write_message([t.REN_ON_WRITE])
        assert intf.get_visa_attribute(constants.VI_ATTR_GPIB_REN_STATE) == constants.LineState.asserted

    def test_ren_assert_llo_sends_llo_to_the_bus_with_the_session_timeout(self, intf, board):
        intf.timeout = 300
        intf.control_ren(REN.asrt_llo)
        writes = [m for m in board.messages if m[0] == p.OP_REGISTER_WRITE]
        assert writes[-1] == p.register_write_message([t.REN_ON_WRITE])
        assert last_command(board) == bytes((t.CMD_LLO,))
        assert board.instructions(p.OP_COMMAND)[-1][3] == 0xFA

    @pytest.mark.parametrize('mode', [REN.asrt_address, REN.asrt_address_llo,
                                      REN.deassert_gtl, REN.address_gtl])
    def test_ren_modes_that_need_a_device_are_not_supported(self, intf, board, mode):
        before = len(board.messages)
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            intf.control_ren(mode)
        assert info.value.error_code == StatusCode.error_nonsupported_operation
        assert len(board.messages) == before

    def test_atn_operations(self, intf, board):
        intf.control_atn(ATN.asrt)
        assert board.messages[-1] == p.take_control_message(synchronous=True)
        assert intf.atn_state == constants.LineState.asserted
        intf.control_atn(ATN.deassert)
        assert board.messages[-1] == p.go_to_standby_message()
        assert intf.atn_state == constants.LineState.unasserted
        intf.control_atn(ATN.asrt_immediate)
        assert board.messages[-1] == p.take_control_message(synchronous=False)
        assert intf.atn_state == constants.LineState.asserted

    def test_atn_deassert_handshake_is_not_supported(self, intf, board):
        before = len(board.messages)
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            intf.control_atn(ATN.deassert_handshake)
        assert info.value.error_code == StatusCode.error_nonsupported_operation
        assert len(board.messages) == before

    def test_pass_control_is_not_supported(self, intf, board):
        before = len(board.messages)
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            intf.pass_control(24, constants.VI_NO_SEC_ADDR)
        assert info.value.error_code == StatusCode.error_nonsupported_operation
        assert len(board.messages) == before

    def test_usb_fault_on_a_line_operation_is_error_io(self, intf, board):
        board.fail_next = TransportError('pipe stalled')
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            intf.send_ifc()
        assert info.value.error_code == StatusCode.error_io
        assert intf.send_ifc() == StatusCode.success  # re-attached first
        assert board.control_requests.count(0x41) == 2


class TestCommandBytes:
    def test_send_command_carries_the_session_timeout(self, intf, board):
        intf.timeout = 1000
        written, status = intf.send_command(bytes((UNL, MTA0, LAD24)))
        assert (written, status) == (3, StatusCode.success)
        assert board.instructions(p.OP_COMMAND)[-1] == p.command_message(bytes((UNL, MTA0, LAD24)), 0xFB)

    def test_long_command_sequences_go_out_in_16_byte_chunks(self, intf, board):
        command = bytes(range(0x20, 0x20 + 20))
        before = len(board.instructions(p.OP_COMMAND))
        written, _ = intf.send_command(command)
        assert written == 20
        chunks = board.instructions(p.OP_COMMAND)[before:]
        assert [c[4:4 + (0x100 - c[1])] for c in chunks] == [command[:16], command[16:]]

    def test_command_on_an_empty_bus_reports_no_listeners(self, intf, board):
        board.instruments.clear()
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            intf.send_command(bytes((UNL, LAD24)))
        assert info.value.error_code == StatusCode.error_no_listeners

    def test_group_execute_trigger_uses_send_command(self, rm, intf, board):
        inst = rm.open_resource('GPIB0::24::INSTR')
        intf.group_execute_trigger(inst)
        assert last_command(board) == bytes((MTA0, UNL, LAD24, t.CMD_GET))
        inst.close()


class TestData:
    def test_write_goes_to_whoever_listens_without_readdressing(self, intf, board):
        intf.send_command(bytes((UNL, MTA0, LAD24)))
        commands = len(board.instructions(p.OP_COMMAND))
        intf.timeout = 300
        assert intf.write('*IDN?') == 7
        assert len(board.instructions(p.OP_COMMAND)) == commands
        assert board.instructions(p.OP_WRITE)[-1] == p.write_message(b'*IDN?\r\n', 0xFA, send_eoi=True)
        assert board.instruments[24].received == [b'*IDN?\r\n']

    def test_send_end_false_drops_the_eoi_flag(self, intf, board):
        intf.send_command(bytes((UNL, MTA0, LAD24)))
        intf.send_end = False
        intf.write('*IDN?')
        assert board.instructions(p.OP_WRITE)[-1][6] == 0x00

    def test_write_with_nobody_listening_reports_no_listeners(self, intf, board):
        intf.send_command(bytes((UNL,)))
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            intf.write('*IDN?')
        assert info.value.error_code == StatusCode.error_no_listeners

    def test_read_from_the_addressed_talker_after_standby(self, intf, board):
        intf.send_command(bytes((UNL, MTA0, LAD24)))
        intf.write('*IDN?')
        intf.send_command(bytes((UNL, MLA0, TAD24)))
        commands = len(board.instructions(p.OP_COMMAND))
        intf.timeout = 1000
        assert intf.read() == 'KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30\n'
        assert len(board.instructions(p.OP_COMMAND)) == commands
        opcodes = [m[0] for m in board.messages[-2:]]
        assert opcodes == [p.OP_GO_TO_STANDBY, p.OP_READ]
        read = board.instructions(p.OP_READ)[-1]
        assert read[1:4] == h('00 00 fb')

    def test_read_termination_selects_eos(self, rm, board):
        intf = rm.open_resource('GPIB0::INTFC', read_termination='\n')
        intf.send_command(bytes((UNL, MTA0, LAD24)))
        intf.write('*IDN?')
        intf.send_command(bytes((UNL, MLA0, TAD24)))
        assert intf.read() == 'KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30'
        assert board.instructions(p.OP_READ)[-1][1:3] == h('14 0a')
        intf.close()

    def test_read_with_nothing_to_say_is_a_timeout(self, intf, board):
        intf.send_command(bytes((UNL, MLA0, TAD24)))
        intf.timeout = 100
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            intf.read()
        assert info.value.error_code == StatusCode.error_timeout

    def test_clear_is_a_universal_device_clear(self, intf, board):
        intf.timeout = 300
        board.instruments[24].pending = b'stale'
        intf.clear()
        assert last_command(board) == bytes((t.CMD_DCL,))
        assert board.instructions(p.OP_COMMAND)[-1][3] == 0xFA
        assert board.instruments[24].cleared == 1
        assert board.instruments[24].pending == b''

    def test_flush_and_read_stb(self, intf):
        assert intf.flush(constants.BufferOperation.discard_read_buffer) is None
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            intf.read_stb()
        assert info.value.error_code == StatusCode.error_nonsupported_operation

    def test_timeout_is_rounded_to_the_device_table(self, intf):
        intf.timeout = 5000
        assert intf.timeout == 10000
        intf.timeout = 2_000_000
        assert intf.timeout == 1_000_000


# ---------------------------------------------------------------------------
# attributes
# ---------------------------------------------------------------------------

class TestAttributes:
    def test_controller_state(self, intf):
        assert intf.is_controller_in_charge is True
        assert intf.is_system_controller is True
        assert intf.primary_address == 0
        assert intf.secondary_address == constants.VI_NO_SEC_ADDR

    def test_ndac_follows_a_listener_once_atn_drops(self, intf, board):
        intf.send_command(bytes((UNL, LAD24)))
        assert intf.ndac_state == constants.LineState.unasserted  # ATN still true
        intf.control_atn(ATN.deassert)
        assert intf.ndac_state == constants.LineState.asserted
        intf.control_atn(ATN.asrt)
        intf.send_command(bytes((UNL,)))
        intf.control_atn(ATN.deassert)
        assert intf.ndac_state == constants.LineState.unasserted

    def test_srq_line(self, intf, board):
        assert intf.get_visa_attribute(constants.VI_ATTR_GPIB_SRQ_STATE) == constants.LineState.unasserted
        board.srq = True
        assert intf.get_visa_attribute(constants.VI_ATTR_GPIB_SRQ_STATE) == constants.LineState.asserted

    def test_line_reads_come_from_the_bus_line_register(self, intf, board):
        intf.atn_state
        assert board.messages[-1] == p.register_read_message([t.BSR_REGISTER])

    def test_address_state(self, intf, board):
        assert intf.address_state == constants.AddressState.unaddressed
        intf.send_command(bytes((UNL, MTA0, LAD24)))
        assert intf.address_state == constants.AddressState.talker
        intf.send_command(bytes((UNL, MLA0, TAD24)))
        assert intf.address_state == constants.AddressState.listenr
        intf.send_command(bytes((UNL, UNT)))
        assert intf.address_state == constants.AddressState.unaddressed
        assert board.control_requests[-1] == 0x21  # the status query, not a bus operation

    def test_line_state_is_unknown_when_the_adapter_faults(self, intf, board):
        board.fail_next = TransportError('pipe stalled')
        assert intf.atn_state == constants.LineState.unknown
        assert intf.atn_state == constants.LineState.asserted  # recovered

    def test_controller_state_reports_the_fault(self, intf, board):
        board.fail_next_control = TransportError('device gone')
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            intf.is_controller_in_charge
        assert info.value.error_code == StatusCode.error_io
        board.fail_next_control = TransportError('device gone')
        with pytest.raises(pyvisa.errors.VisaIOError):
            intf.address_state

    def test_hs488_cable_length_is_not_supported(self, intf):
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            intf.get_visa_attribute(constants.VI_ATTR_GPIB_HS488_CBL_LEN)
        assert info.value.error_code == StatusCode.error_nonsupported_attribute
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            intf.set_visa_attribute(constants.VI_ATTR_GPIB_HS488_CBL_LEN, 2)
        assert info.value.error_code == StatusCode.error_nonsupported_attribute

    @pytest.mark.parametrize('attribute, value', [
        (constants.VI_ATTR_GPIB_PRIMARY_ADDR, 5),
        (constants.VI_ATTR_GPIB_SECONDARY_ADDR, 3),
        (constants.VI_ATTR_GPIB_SYS_CNTRL_STATE, False),
    ])
    def test_adapter_configuration_is_read_only(self, intf, board, attribute, value):
        before = len(board.messages)
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            intf.set_visa_attribute(attribute, value)
        assert info.value.error_code == StatusCode.error_attribute_read_only
        assert len(board.messages) == before

    def test_instrument_only_attributes_are_rejected(self, intf):
        with pytest.raises(pyvisa.errors.VisaIOError) as info:
            intf.get_visa_attribute(constants.VI_ATTR_GPIB_READDR_EN)
        assert info.value.error_code == StatusCode.error_nonsupported_attribute

"""pyvisa-py session for ``GPIB<n>::INTFC`` over an NI USB adapter.

The board itself as a resource: ``pyvisa.ResourceManager('@py')
.open_resource('GPIB0::INTFC')`` gives the bus-level operations (IFC, REN,
ATN, raw command bytes, data to and from whoever is addressed) and the bus
line states. It sits on the same shared ``Controller`` as the INSTR
sessions of that board, through the same registry, and is registered the
same way: a dispatcher in front of whatever pyvisa-py had for
``(gpib, "INTFC")`` that takes over only for boards that are ours.

What the interface session does with data: ``write`` is one 0x0d to
whatever is currently addressed to listen and ``read`` is one 0x0a from
whatever is addressed to talk; neither sends addressing command bytes
(§5.1, §5.2). The caller addresses the bus with ``send_command`` first.
``read`` sends go-to-standby before the read instruction, which is the ATN
rule at the top of §5: after command bytes ATN is true, a read with ATN
true is error 2, and the 0x06 is harmless when ATN is already false.

Interleaving: each call is atomic on the board (the controller's lock),
but a sequence is not. An INSTR session on the same board re-addresses
the bus for its own transfers, so between an interface ``send_command``
and the ``write`` or ``read`` that relies on it another thread's INSTR
call can change who is addressed. ``Session.lock`` is not implemented, so
callers that mix the two on one board serialise them themselves.

Not supported, each with the section that stops short: ``gpib_pass_control``
(§5.17: command bytes known, the adapter's report of the hand-over not);
``ATNLineOperation.deassert_handshake`` (§5.4 has no shadow-handshake
state); the REN operations that address a device (an interface has none);
``VI_ATTR_GPIB_HS488_CBL_LEN`` (nothing in the specification speaks of
HS488).

Scope decisions, not protocol limits: the adapter's own primary address
and its system-controller role read back as attach configured them
(§2.6 rows 16 and 18) and refuse to change. The protocol can change both
(§5.14, §5.7), but the board is shared with INSTR sessions that assume
the attach-time values, and neither write has been bench-tested, so
setting them answers read-only, which VISA allows for an interface
configured outside the session. ``clear`` is a universal device clear;
neither the specification nor pyvisa-py (whose own interface class has
no ``clear``) prescribes what an interface clear means, and DCL is the
bus-wide form of the INSTR session's selected device clear.

Once installed, ``pyvisa-info`` no longer lists the missing-linux-gpib
issue for ``(gpib, INTFC)``: the dispatcher is a live class, as for INSTR.
The message still reaches anyone who opens a board that is not ours,
because the dispatcher hands such boards to the class it replaced.
"""
import logging
from typing import Any, ClassVar, List, Optional, Tuple, Type

from pyvisa import constants, rname
from pyvisa.constants import ResourceAttribute, StatusCode
from pyvisa_py.sessions import OpenError, Session

from . import tables as t
from .protocol import GpibError, GpibTimeout, StatusBlock
from .transport import TransportError
from .visa_session import NiUsbGpibSession, registry, status_for

logger = logging.getLogger(__name__)

GPIB_INTFC = (constants.InterfaceType.gpib, 'INTFC')

#: The adapter's own configuration, fixed by the attach sequence (§2.6).
_FIXED_AT_ATTACH = (ResourceAttribute.gpib_primary_address,
                    ResourceAttribute.gpib_secondary_address,
                    ResourceAttribute.gpib_system_controller)


class NiUsbGpibIntfcSession(NiUsbGpibSession):
    """A GPIB INTFC session: the NI USB adapter itself as the resource."""

    session_type = GPIB_INTFC
    parsed: rname.GPIBIntfc

    @staticmethod
    def list_resources() -> List[str]:
        return registry().list_interfaces()

    # ------------------------------------------------------------------
    # data: the bus as the caller addressed it
    # ------------------------------------------------------------------

    def read(self, count: int) -> Tuple[bytes, StatusCode]:
        controller = self._controller()
        termchar_enabled, _ = self.get_attribute(ResourceAttribute.termchar_enabled)
        termchar, _ = self.get_attribute(ResourceAttribute.termchar)
        if termchar_enabled and not 0 <= termchar <= 0xFF:
            return b'', StatusCode.error_nonsupported_attribute_state
        eos = termchar if termchar_enabled else None
        try:
            with controller.lock:
                # ATN rule (§5): a 0x06 before the 0x0a; a no-op if ATN is already false.
                controller.go_to_standby()
                data, ended = controller.read_raw(count, self._device_timeout(), eos=eos, eos_8bit=True)
        except GpibTimeout as exc:
            return exc.partial, StatusCode.error_timeout
        except (GpibError, TransportError) as exc:
            logger.debug('%s read: %s', self._label(), exc)
            return b'', status_for(exc)
        if ended:
            # END covers EOI and the EOS match alike (§5.2), as on the INSTR session.
            if termchar_enabled and data.endswith(bytes((termchar,))):
                return data, StatusCode.success_termination_character_read
            return data, StatusCode.success
        return data, StatusCode.success_max_count_read

    def write(self, data: bytes) -> Tuple[int, StatusCode]:
        controller = self._controller()
        send_end, _ = self.get_attribute(ResourceAttribute.send_end_enabled)
        try:
            written = controller.write_raw(data, send_eoi=bool(send_end),
                                           timeout_s=self._device_timeout(), eos_char=self._termchar_byte())
        except (GpibError, TransportError) as exc:
            logger.debug('%s write: %s', self._label(), exc)
            return 0, status_for(exc)
        return written, StatusCode.success

    # ------------------------------------------------------------------
    # bus operations
    # ------------------------------------------------------------------

    def clear(self) -> StatusCode:
        # This driver's choice (see the module docstring): the bus-wide form of
        # the INSTR session's selected device clear, one DCL to every device (§5.8).
        return self._bus_operation(lambda c: c.command(bytes((t.CMD_DCL,)), self._device_timeout()))

    def gpib_pass_control(self, primary_address: int, secondary_address: int) -> StatusCode:
        # §5.17 gives the command bytes (TAD N, TCT) but records the operation
        # as never exercised and the adapter's report of losing CIC as
        # uncertain; everything else here assumes the adapter stays CIC.
        return StatusCode.error_nonsupported_operation

    def _status(self) -> StatusBlock:
        """The current ibsta (§5.12), for the bits §4.2 calls reliable as bus state."""
        return self._controller().status()

    # ------------------------------------------------------------------
    # attributes not held in self.attrs
    # ------------------------------------------------------------------

    def _get_attribute(self, attribute: ResourceAttribute) -> Tuple[Any, StatusCode]:
        if attribute == ResourceAttribute.gpib_atn_state:
            return self._line_state(t.BSR_ATN), StatusCode.success
        if attribute == ResourceAttribute.gpib_ndac_state:
            return self._line_state(t.BSR_NDAC), StatusCode.success
        if attribute == ResourceAttribute.gpib_srq_state:
            return self._line_state(t.BSR_SRQ), StatusCode.success
        if attribute == ResourceAttribute.gpib_primary_address:
            return self._controller().own_address, StatusCode.success
        if attribute == ResourceAttribute.gpib_secondary_address:
            # Own secondary addressing stays disabled (§2.6 rows 20-22).
            return constants.VI_NO_SEC_ADDR, StatusCode.success
        if attribute == ResourceAttribute.gpib_system_controller:
            return self._controller().system_controller, StatusCode.success
        if attribute == ResourceAttribute.gpib_cic_state:
            try:
                return self._status().cic, StatusCode.success
            except (GpibError, TransportError) as exc:
                return False, status_for(exc)
        if attribute == ResourceAttribute.gpib_address_state:
            try:
                status = self._status()
            except (GpibError, TransportError) as exc:
                return constants.AddressState.unaddressed, status_for(exc)
            if status.tacs:
                return constants.AddressState.talker, StatusCode.success
            if status.lacs:
                return constants.AddressState.listenr, StatusCode.success
            return constants.AddressState.unaddressed, StatusCode.success
        if attribute == ResourceAttribute.gpib_hs488_cable_length:
            return 0, StatusCode.error_nonsupported_attribute
        return super()._get_attribute(attribute)

    def _set_attribute(self, attribute: ResourceAttribute, attribute_state: Any) -> StatusCode:
        if attribute in _FIXED_AT_ATTACH:
            # §5.14 and §5.7 could change these; left to attach by choice (module docstring).
            return StatusCode.error_attribute_read_only
        if attribute == ResourceAttribute.gpib_hs488_cable_length:
            return StatusCode.error_nonsupported_attribute
        return super()._set_attribute(attribute, attribute_state)


class NiUsbGpibIntfcDispatch(Session):
    """Routes ``GPIB<n>::INTFC`` to our session for our boards, else onward.

    Like ``visa_session.NiUsbGpibDispatch``: ``__new__`` returns an instance
    of another class, so this is never a live session.
    """

    session_type = GPIB_INTFC
    #: The class that held (gpib, INTFC) before ``install()``.
    previous: ClassVar[Optional[Type[Session]]] = None

    def __new__(cls, resource_manager_session: Any, resource_name: str,
                parsed: Optional[rname.ResourceName] = None,
                open_timeout: Optional[int] = None) -> Session:  # type: ignore[misc]
        if parsed is None:
            parsed = rname.parse_resource_name(resource_name)
        if isinstance(parsed, rname.GPIBIntfc) and registry().owns(parsed.board):
            return NiUsbGpibIntfcSession(resource_manager_session, resource_name, parsed,
                                         open_timeout)
        if cls.previous is None:
            raise OpenError(StatusCode.error_resource_not_found)
        return cls.previous(resource_manager_session, resource_name, parsed, open_timeout)

    @staticmethod
    def list_resources() -> List[str]:
        names = NiUsbGpibIntfcSession.list_resources()
        previous = NiUsbGpibIntfcDispatch.previous
        if previous is not None:
            try:
                names.extend(previous.list_resources())
            except Exception as exc:  # noqa: BLE001 - third-party code; its failure must not hide our boards
                logger.debug('%s.list_resources failed: %s', previous.__name__, exc)
        return names


def install() -> None:
    """Put the dispatcher in front of pyvisa-py's ``(gpib, INTFC)`` class. Idempotent."""
    current = Session._session_classes.get(GPIB_INTFC)
    if current is NiUsbGpibIntfcDispatch:
        return
    NiUsbGpibIntfcDispatch.previous = current
    Session._session_classes[GPIB_INTFC] = NiUsbGpibIntfcDispatch
    logger.debug('NI GPIB-USB interface session installed in front of %s',
                 current.__name__ if current is not None else 'nothing')

"""pyvisa-py session for ``GPIB<n>::<pad>::INSTR`` over an NI USB adapter.

pyvisa-py already owns the ``(gpib, "INSTR")`` slot with a class that hands
resources to Prologix or linux-gpib. This module does not replace that: it
puts a dispatcher in front of it that routes boards belonging to NI USB
adapters here and everything else to whatever was registered before. Its
``list_resources`` merges both.

Boards: ``boards.BoardRegistry`` decides which adapter is ``GPIB<n>``
(by USB serial, numbered after any linux-gpib boards) and shares one
attached ``Controller`` between every session on a board, closing it when
the last of them closes.

``list_resources`` is not side-effect free: it attaches each adapter
(IFC pulse, REN on, take control), addresses every primary address in turn
with ATN toggling to look for a listener, and shuts the adapter down again
unless a session holds it. That is what a GPIB bus scan is; linux-gpib's
listing does the same.

Not registered: ``(gpib, "INTFC")``. Everything an INSTR session needs
(IFC, REN, raw command bytes, trigger, serial poll) is reachable on the
INSTR session itself. Not supported: ``gpib_pass_control`` (§5.17 leaves
the adapter's report of the hand-over uncertain) and
``VI_ATTR_SUPPRESS_END_EN`` set to True.

Python: this module is 3.10+ by dependency (pyvisa-py 0.8.1 requires it);
the rest of the package stays 3.9.
"""
import logging
from typing import Any, Callable, ClassVar, List, Optional, Tuple, Type

from pyvisa import attributes, constants, errors, rname
from pyvisa.constants import ResourceAttribute, StatusCode
from pyvisa_py.sessions import OpenError, Session, UnknownAttribute

from . import device_ops as ops
from . import protocol as p
from . import tables as t
from .boards import BoardRegistry
from .controller import Controller
from .protocol import GpibError, GpibTimeout, NoListener
from .transport import TransportError

logger = logging.getLogger(__name__)

GPIB_INSTR = (constants.InterfaceType.gpib, 'INSTR')
_REGISTRY = BoardRegistry()


def _status_for(exc: Exception) -> StatusCode:
    if isinstance(exc, GpibTimeout):
        return StatusCode.error_timeout
    if isinstance(exc, NoListener):
        return StatusCode.error_no_listeners
    return StatusCode.error_io


class NiUsbGpibInstrSession(Session):
    """A GPIB INSTR session whose bus is an NI USB adapter."""

    # Registered through the dispatcher, not Session.register(), so the
    # session type the attribute machinery checks is set by hand.
    session_type = GPIB_INSTR
    parsed: rname.GPIBInstr
    interface: Optional[Controller]

    @staticmethod
    def list_resources() -> List[str]:
        return _REGISTRY.list_instruments()

    @classmethod
    def get_low_level_info(cls) -> str:
        return 'via resistamet_gui.gpib_usb (pyusb)'

    def after_parsing(self) -> None:
        try:
            self.interface = _REGISTRY.acquire(self.parsed.board)
        except KeyError:
            raise OpenError(StatusCode.error_resource_not_found)
        except Exception as exc:  # noqa: BLE001 - pyvisa expects an OpenError, whatever the USB stack threw
            logger.warning('GPIB%s: cannot open adapter: %s', self.parsed.board, exc)
            raise OpenError(StatusCode.error_system_error)
        self._pad = int(self.parsed.primary_address)
        sad = self.parsed.secondary_address
        self._sad: Optional[int] = None if sad is None else int(sad)
        for attribute in (ResourceAttribute.send_end_enabled,
                          ResourceAttribute.termchar,
                          ResourceAttribute.termchar_enabled,
                          ResourceAttribute.gpib_readdress_enabled):
            self.attrs[attribute] = attributes.AttributesByID[attribute].default
        self.attrs[ResourceAttribute.interface_number] = int(self.parsed.board)

    def close(self) -> StatusCode:
        if self.interface is not None:
            _REGISTRY.release(self.parsed.board)
            self.interface = None
        return StatusCode.success

    def _controller(self) -> Controller:
        if self.interface is None:
            raise errors.InvalidSession()
        return self.interface

    def _set_timeout(self, attribute: ResourceAttribute, value: int) -> StatusCode:
        status = super()._set_timeout(attribute, value)
        if self.timeout:
            # Round to the device's table (§7.1) so pyvisa's own deadline agrees
            # with what the adapter enforces; above the table, the longest row.
            _, limit = p.effective_timeout(min(self.timeout, t.TIMEOUT_MAX_S))
            self.timeout = limit
        return status

    def _device_timeout(self) -> Optional[float]:
        # VISA "immediate" (0) has no device analogue; the shortest device
        # timeout, 10 us, is the honest reading. None stays infinite.
        if self.timeout == 0:
            return 10e-6
        return self.timeout

    def _readdress(self) -> bool:
        value, _ = self.get_attribute(ResourceAttribute.gpib_readdress_enabled)
        return bool(value)

    # ------------------------------------------------------------------
    # data
    # ------------------------------------------------------------------

    def read(self, count: int) -> Tuple[bytes, StatusCode]:
        controller = self._controller()
        termchar_enabled, _ = self.get_attribute(ResourceAttribute.termchar_enabled)
        termchar, _ = self.get_attribute(ResourceAttribute.termchar)
        if termchar_enabled and not 0 <= termchar <= 0xFF:
            return b'', StatusCode.error_nonsupported_attribute_state
        eos = termchar if termchar_enabled else None
        try:
            data, ended = controller.read(self._pad, sad=self._sad, max_bytes=count,
                                          timeout_s=self._device_timeout(), eos=eos,
                                          eos_8bit=True, readdress=self._readdress())
        except GpibTimeout as exc:
            return exc.partial, StatusCode.error_timeout
        except (GpibError, TransportError) as exc:
            logger.debug('GPIB%s::%d read: %s', self.parsed.board, self._pad, exc)
            return b'', _status_for(exc)
        if ended:
            # The adapter's END covers both EOI and the EOS match (§5.2); a
            # last byte equal to the enabled termchar is reported as the latter.
            if termchar_enabled and data.endswith(bytes((termchar,))):
                return data, StatusCode.success_termination_character_read
            return data, StatusCode.success
        return data, StatusCode.success_max_count_read

    def write(self, data: bytes) -> Tuple[int, StatusCode]:
        controller = self._controller()
        send_end, _ = self.get_attribute(ResourceAttribute.send_end_enabled)
        try:
            written = controller.write(self._pad, data, sad=self._sad, send_eoi=bool(send_end),
                                       timeout_s=self._device_timeout(),
                                       readdress=self._readdress())
        except (GpibError, TransportError) as exc:
            logger.debug('GPIB%s::%d write: %s', self.parsed.board, self._pad, exc)
            return 0, _status_for(exc)
        return written, StatusCode.success

    def flush(self, mask: constants.BufferOperation) -> StatusCode:
        # Nothing is buffered on the host side; every write goes to the bus.
        return StatusCode.success

    # ------------------------------------------------------------------
    # device and bus operations; the session timeout goes into each (§5.18)
    # ------------------------------------------------------------------

    def clear(self) -> StatusCode:
        return self._bus_operation(
            lambda c: ops.device_clear(c, self._pad, self._sad, self._device_timeout()))

    def read_stb(self) -> Tuple[int, StatusCode]:
        controller = self._controller()
        try:
            return ops.serial_poll(controller, self._pad, self._sad,
                                   self._device_timeout()), StatusCode.success
        except (GpibError, TransportError) as exc:
            logger.debug('GPIB%s::%d serial poll: %s', self.parsed.board, self._pad, exc)
            return 0, _status_for(exc)

    def assert_trigger(self, protocol: constants.TriggerProtocol) -> StatusCode:
        if protocol != constants.TriggerProtocol.default:
            return StatusCode.error_nonsupported_operation
        return self._bus_operation(
            lambda c: ops.trigger(c, self._pad, self._sad, self._device_timeout()))

    def gpib_command(self, command_byte: bytes) -> Tuple[int, StatusCode]:
        controller = self._controller()
        try:
            return controller.command(command_byte, self._device_timeout()), StatusCode.success
        except (GpibError, TransportError) as exc:
            return 0, _status_for(exc)

    def gpib_send_ifc(self) -> StatusCode:
        return self._bus_operation(lambda c: c.interface_clear())

    def gpib_control_ren(self, mode: constants.RENLineOperation) -> StatusCode:
        pad, sad, timeout = self._pad, self._sad, self._device_timeout()

        def run(c: Controller) -> None:
            if mode == constants.RENLineOperation.asrt:
                c.remote_enable(True)
            elif mode == constants.RENLineOperation.asrt_address:
                c.remote_enable(True)
                c.command(t.address_listener_command(c.own_address, pad, sad), timeout)
            elif mode == constants.RENLineOperation.asrt_llo:
                c.remote_enable(True)
                ops.local_lockout(c, timeout_s=timeout)
            elif mode == constants.RENLineOperation.asrt_address_llo:
                c.remote_enable(True)
                ops.local_lockout(c, pad, sad, timeout)
            elif mode == constants.RENLineOperation.deassert:
                c.remote_enable(False)
            elif mode == constants.RENLineOperation.deassert_gtl:
                ops.go_to_local(c, pad, sad, timeout)
                c.remote_enable(False)
            elif mode == constants.RENLineOperation.address_gtl:
                ops.go_to_local(c, pad, sad, timeout)
            else:
                raise ValueError(mode)

        try:
            return self._bus_operation(run)
        except ValueError:
            return StatusCode.error_nonsupported_operation

    def gpib_control_atn(self, mode: constants.ATNLineOperation) -> StatusCode:
        def run(c: Controller) -> None:
            if mode == constants.ATNLineOperation.asrt:
                c.take_control(synchronous=True)
            elif mode == constants.ATNLineOperation.asrt_immediate:
                c.take_control(synchronous=False)
            elif mode == constants.ATNLineOperation.deassert:
                c.go_to_standby()
            else:
                raise ValueError(mode)  # deassert_handshake: no protocol equivalent

        try:
            return self._bus_operation(run)
        except ValueError:
            return StatusCode.error_nonsupported_operation

    def _bus_operation(self, operation: Callable[[Controller], Any]) -> StatusCode:
        controller = self._controller()
        try:
            operation(controller)
        except (GpibError, TransportError) as exc:
            logger.debug('GPIB%s::%d: %s', self.parsed.board, self._pad, exc)
            return _status_for(exc)
        return StatusCode.success

    # ------------------------------------------------------------------
    # attributes not held in self.attrs
    # ------------------------------------------------------------------

    def _get_attribute(self, attribute: ResourceAttribute) -> Tuple[Any, StatusCode]:
        if attribute == ResourceAttribute.gpib_primary_address:
            return self._pad, StatusCode.success
        if attribute == ResourceAttribute.gpib_secondary_address:
            value = constants.VI_NO_SEC_ADDR if self._sad is None else self._sad
            return value, StatusCode.success
        if attribute == ResourceAttribute.suppress_end_enabled:
            return False, StatusCode.success
        if attribute == ResourceAttribute.gpib_ren_state:
            try:
                lines = self._controller().bus_lines()
            except (GpibError, TransportError):
                return constants.LineState.unknown, StatusCode.success
            state = constants.LineState.asserted if lines & t.BSR_REN else constants.LineState.unasserted
            return state, StatusCode.success
        if attribute == ResourceAttribute.interface_type:
            return constants.InterfaceType.gpib, StatusCode.success
        raise UnknownAttribute(attribute)

    def _set_attribute(self, attribute: ResourceAttribute, attribute_state: Any) -> StatusCode:
        if attribute == ResourceAttribute.gpib_primary_address:
            if isinstance(attribute_state, int) and 0 <= attribute_state <= 30:
                self._pad = attribute_state
                return StatusCode.success
            return StatusCode.error_nonsupported_attribute_state
        if attribute == ResourceAttribute.gpib_secondary_address:
            if attribute_state == constants.VI_NO_SEC_ADDR:
                self._sad = None
                return StatusCode.success
            if isinstance(attribute_state, int) and 0 <= attribute_state <= 31:
                self._sad = attribute_state
                return StatusCode.success
            return StatusCode.error_nonsupported_attribute_state
        if attribute == ResourceAttribute.suppress_end_enabled:
            # Reading past END would mean waiting on a talker that has stopped;
            # the adapter ends every read at END, so only False is honest.
            if attribute_state:
                return StatusCode.error_nonsupported_attribute_state
            return StatusCode.success
        raise UnknownAttribute(attribute)


class NiUsbGpibDispatch(Session):
    """Routes ``GPIB<n>::...::INSTR`` to our session for our boards, else onward.

    ``__new__`` returns an instance of another class, so ``__init__`` of this
    one never runs and it is never a live session itself.
    """

    session_type = GPIB_INSTR
    #: The class that held (gpib, INSTR) before ``install()``.
    previous: ClassVar[Optional[Type[Session]]] = None

    def __new__(cls, resource_manager_session: Any, resource_name: str,
                parsed: Optional[rname.ResourceName] = None,
                open_timeout: Optional[int] = None) -> Session:  # type: ignore[misc]
        if parsed is None:
            parsed = rname.parse_resource_name(resource_name)
        if isinstance(parsed, rname.GPIBInstr) and _REGISTRY.owns(parsed.board):
            return NiUsbGpibInstrSession(resource_manager_session, resource_name, parsed,
                                         open_timeout)
        if cls.previous is None:
            raise OpenError(StatusCode.error_resource_not_found)
        return cls.previous(resource_manager_session, resource_name, parsed, open_timeout)

    @staticmethod
    def list_resources() -> List[str]:
        names = NiUsbGpibInstrSession.list_resources()
        previous = NiUsbGpibDispatch.previous
        if previous is not None:
            try:
                names.extend(previous.list_resources())
            except Exception as exc:  # noqa: BLE001 - third-party code; its failure must not hide our boards
                logger.debug('%s.list_resources failed: %s', previous.__name__, exc)
        return names


def install() -> None:
    """Put the dispatcher in front of pyvisa-py's ``(gpib, INSTR)`` class. Idempotent."""
    import pyvisa_py  # noqa: F401 - registers pyvisa-py's own session classes first
    current = Session._session_classes.get(GPIB_INSTR)
    if current is NiUsbGpibDispatch:
        return
    NiUsbGpibDispatch.previous = current
    # Assigned directly: Session.register() logs a warning about overwriting
    # the existing class, and overwriting it is exactly what this does.
    Session._session_classes[GPIB_INSTR] = NiUsbGpibDispatch
    logger.debug('NI GPIB-USB session installed in front of %s',
                 current.__name__ if current is not None else 'nothing')

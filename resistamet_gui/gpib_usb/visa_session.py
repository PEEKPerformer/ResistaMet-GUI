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

``NiUsbGpibSession`` holds what every session on one of our boards shares
(the board handle, timeouts, IFC, raw command bytes, the REN and ATN line
operations); ``NiUsbGpibInstrSession`` adds the addressed device on top.

The board itself, ``GPIB<n>::INTFC``, is ``visa_intfc``; ``install()``
here installs both. Not supported on the INSTR session:
``gpib_pass_control`` (§5.17 leaves the adapter's report of the hand-over
uncertain) and ``VI_ATTR_SUPPRESS_END_EN`` set to True.

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

#: A device address as the REN operations need it: (primary, secondary or None).
DeviceAddress = Tuple[int, Optional[int]]


def registry() -> BoardRegistry:
    """The one board registry every session class and dispatcher shares."""
    return _REGISTRY


def status_for(exc: Exception) -> StatusCode:
    if isinstance(exc, GpibTimeout):
        return StatusCode.error_timeout
    if isinstance(exc, NoListener):
        return StatusCode.error_no_listeners
    return StatusCode.error_io


# ----------------------------------------------------------------------
# the VISA line operations in protocol terms (§5.4, §5.6, §5.18)
# ----------------------------------------------------------------------

def ren_operation(controller: Controller, mode: constants.RENLineOperation,
                  timeout_s: Optional[float], device: Optional[DeviceAddress]) -> None:
    """One ``RENLineOperation`` on the bus; ``ValueError`` when it needs a device and has none.

    The shape of each mode is what NI's driver sends on an instrument
    session (§10.7.4): the REN register write (§5.6) even when REN is
    already asserted, then the "address" step, then LLO as a lone
    universal command byte; the go-to-local modes are command bytes to the
    addressed device, and for ``deassert_gtl`` REN goes off after them. The
    modes that involve the device need ``device``; an interface session
    has none to give, and NI refuses those modes there too (§10.6.4).

    One step differs. NI's "address" step is its presence-probe
    instruction 0x02, whose effect on the bus §10.6.1 and §10.7.4 call not
    established: that it addresses the device to listen is inferred, not
    seen. This driver does not use 0x02 anywhere, so the step stays the
    explicit listen addressing of §6, which is known to put a device with
    REN true into remote state.
    """
    if mode == constants.RENLineOperation.asrt:
        controller.remote_enable(True)
    elif mode == constants.RENLineOperation.deassert:
        controller.remote_enable(False)
    elif mode == constants.RENLineOperation.asrt_llo:
        controller.remote_enable(True)
        ops.local_lockout(controller, timeout_s=timeout_s)
    elif device is None:
        raise ValueError(mode)
    elif mode == constants.RENLineOperation.asrt_address:
        controller.remote_enable(True)
        _address_to_listen(controller, device, timeout_s)
    elif mode == constants.RENLineOperation.asrt_address_llo:
        controller.remote_enable(True)
        _address_to_listen(controller, device, timeout_s)
        ops.local_lockout(controller, timeout_s=timeout_s)
    elif mode == constants.RENLineOperation.deassert_gtl:
        pad, sad = device
        ops.go_to_local(controller, pad, sad, timeout_s)
        controller.remote_enable(False)
    elif mode == constants.RENLineOperation.address_gtl:
        pad, sad = device
        ops.go_to_local(controller, pad, sad, timeout_s)
    else:
        raise ValueError(mode)


def _address_to_listen(controller: Controller, device: DeviceAddress, timeout_s: Optional[float]) -> None:
    """The "address" step of the REN modes; see ``ren_operation`` for why it is not NI's 0x02."""
    pad, sad = device
    controller.command(t.address_listener_command(controller.own_address, pad, sad), timeout_s)


def atn_operation(controller: Controller, mode: constants.ATNLineOperation) -> None:
    """One ``ATNLineOperation`` (§5.4); ``ValueError`` for the mode the protocol lacks."""
    if mode == constants.ATNLineOperation.asrt:
        controller.take_control(synchronous=True)
    elif mode == constants.ATNLineOperation.asrt_immediate:
        controller.take_control(synchronous=False)
    elif mode == constants.ATNLineOperation.deassert:
        controller.go_to_standby()
    else:
        # deassert_handshake (shadow handshake): §5.4 offers only take control
        # and go to standby, nothing that keeps the adapter in the handshake.
        raise ValueError(mode)


# ----------------------------------------------------------------------
# sessions
# ----------------------------------------------------------------------

class NiUsbGpibSession(Session):
    """What every session on one of our boards shares. Not registered itself.

    Subclasses set ``session_type`` by hand (they are reached through a
    dispatcher, not ``Session.register()``), so the attribute machinery
    checks the right resource class.
    """

    interface: Optional[Controller]

    @classmethod
    def get_low_level_info(cls) -> str:
        return 'via resistamet_gui.gpib_usb (pyusb)'

    def after_parsing(self) -> None:
        try:
            self.interface = registry().acquire(self.parsed.board)
        except KeyError:
            raise OpenError(StatusCode.error_resource_not_found)
        except Exception as exc:  # noqa: BLE001 - pyvisa expects an OpenError, whatever the USB stack threw
            logger.warning('GPIB%s: cannot open adapter: %s', self.parsed.board, exc)
            raise OpenError(StatusCode.error_system_error)
        for attribute in (ResourceAttribute.send_end_enabled,
                          ResourceAttribute.termchar,
                          ResourceAttribute.termchar_enabled):
            self.attrs[attribute] = attributes.AttributesByID[attribute].default
        self.attrs[ResourceAttribute.interface_number] = int(self.parsed.board)

    def close(self) -> StatusCode:
        if self.interface is not None:
            registry().release(self.parsed.board)
            self.interface = None
        return StatusCode.success

    def _controller(self) -> Controller:
        if self.interface is None:
            raise errors.InvalidSession()
        return self.interface

    def _set_timeout(self, attribute: ResourceAttribute, value: int) -> StatusCode:
        status = super()._set_timeout(attribute, value)
        if self.timeout:
            # Round to the device's table (§7.1) so the attribute reads back as
            # the row whose code goes out; above the table, the longest row.
            # That is the nominal limit. What the adapter then waits is the
            # expiry of §7.3 (16.78 s for the 10 s row), and the controller
            # derives the host wait from that, not from this value.
            _, limit = p.effective_timeout(min(self.timeout, t.TIMEOUT_MAX_S))
            self.timeout = limit
        return status

    def _device_timeout(self) -> Optional[float]:
        # VISA "immediate" (0) has no device analogue; the shortest device
        # timeout, 10 us, is the honest reading. None stays infinite.
        if self.timeout == 0:
            return 10e-6
        return self.timeout

    def _device(self) -> Optional[DeviceAddress]:
        """The addressed device, for the REN modes that need one. None for an interface."""
        return None

    def _termchar_byte(self) -> Optional[int]:
        """VI_ATTR_TERMCHAR as a byte when VI_ATTR_TERMCHAR_EN is on, else None.

        NI fills the ``e`` byte of every read and write with the character
        even with the compare off (§10.1.6, §10.5.1), but that was seen only
        under NI's AUXRA 0x99 initialisation; ours is 0x81 (§2.6 row 3), under
        which §5.2 still says error 4. With the compare off the byte does
        nothing useful, so the bench-proven 0x00 is sent until hardware says
        otherwise; the codec can send either.
        """
        enabled, _ = self.get_attribute(ResourceAttribute.termchar_enabled)
        if not enabled:
            return None
        termchar, _ = self.get_attribute(ResourceAttribute.termchar)
        return termchar if isinstance(termchar, int) and 0 <= termchar <= 0xFF else None

    def _label(self) -> str:
        return 'GPIB%s' % self.parsed.board

    def flush(self, mask: constants.BufferOperation) -> StatusCode:
        # Nothing is buffered on the host side; every write goes to the bus.
        return StatusCode.success

    # ------------------------------------------------------------------
    # bus operations; the session timeout goes into each (§5.18)
    # ------------------------------------------------------------------

    def gpib_command(self, command_byte: bytes) -> Tuple[int, StatusCode]:
        controller = self._controller()
        try:
            return controller.command(command_byte, self._device_timeout()), StatusCode.success
        except (GpibError, TransportError) as exc:
            return 0, status_for(exc)

    def gpib_send_ifc(self) -> StatusCode:
        return self._bus_operation(lambda c: c.interface_clear())

    def gpib_control_ren(self, mode: constants.RENLineOperation) -> StatusCode:
        timeout, device = self._device_timeout(), self._device()
        try:
            return self._bus_operation(lambda c: ren_operation(c, mode, timeout, device))
        except ValueError:
            return StatusCode.error_nonsupported_operation

    def gpib_control_atn(self, mode: constants.ATNLineOperation) -> StatusCode:
        try:
            return self._bus_operation(lambda c: atn_operation(c, mode))
        except ValueError:
            return StatusCode.error_nonsupported_operation

    def _bus_operation(self, operation: Callable[[Controller], Any]) -> StatusCode:
        controller = self._controller()
        try:
            operation(controller)
        except (GpibError, TransportError) as exc:
            logger.debug('%s: %s', self._label(), exc)
            return status_for(exc)
        return StatusCode.success

    def _line_state(self, bit: int) -> constants.LineState:
        """One bus line from the BSR (§5.13); unknown when the adapter cannot be asked."""
        try:
            lines = self._controller().bus_lines()
        except (GpibError, TransportError) as exc:
            logger.debug('%s line state: %s', self._label(), exc)
            return constants.LineState.unknown
        return constants.LineState.asserted if lines & bit else constants.LineState.unasserted

    # ------------------------------------------------------------------
    # attributes not held in self.attrs
    # ------------------------------------------------------------------

    def _get_attribute(self, attribute: ResourceAttribute) -> Tuple[Any, StatusCode]:
        if attribute == ResourceAttribute.gpib_ren_state:
            return self._line_state(t.BSR_REN), StatusCode.success
        if attribute == ResourceAttribute.interface_type:
            return constants.InterfaceType.gpib, StatusCode.success
        raise UnknownAttribute(attribute)

    def _set_attribute(self, attribute: ResourceAttribute, attribute_state: Any) -> StatusCode:
        raise UnknownAttribute(attribute)


class NiUsbGpibInstrSession(NiUsbGpibSession):
    """A GPIB INSTR session whose bus is an NI USB adapter."""

    session_type = GPIB_INSTR
    parsed: rname.GPIBInstr

    @staticmethod
    def list_resources() -> List[str]:
        return registry().list_instruments()

    def after_parsing(self) -> None:
        super().after_parsing()
        self._pad = int(self.parsed.primary_address)
        sad = self.parsed.secondary_address
        self._sad: Optional[int] = None if sad is None else int(sad)
        readdress = ResourceAttribute.gpib_readdress_enabled
        self.attrs[readdress] = attributes.AttributesByID[readdress].default

    def _device(self) -> Optional[DeviceAddress]:
        return self._pad, self._sad

    def _label(self) -> str:
        return 'GPIB%s::%d' % (self.parsed.board, self._pad)

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
        # With the character enabled the instruction compares on it (m = 0x14,
        # §10.1.6); disabled, the proven ``00 00`` goes (see _termchar_byte).
        eos = termchar if termchar_enabled else None
        try:
            data, ended = controller.read(self._pad, sad=self._sad, max_bytes=count,
                                          timeout_s=self._device_timeout(), eos=eos,
                                          eos_8bit=True, termchar=self._termchar_byte(),
                                          readdress=self._readdress())
        except GpibTimeout as exc:
            return exc.partial, StatusCode.error_timeout
        except (GpibError, TransportError) as exc:
            logger.debug('%s read: %s', self._label(), exc)
            return b'', status_for(exc)
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
                                       timeout_s=self._device_timeout(), eos_char=self._termchar_byte(),
                                       readdress=self._readdress())
        except (GpibError, TransportError) as exc:
            logger.debug('%s write: %s', self._label(), exc)
            return 0, status_for(exc)
        return written, StatusCode.success

    # ------------------------------------------------------------------
    # device operations; the session timeout goes into each (§5.18)
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
            logger.debug('%s serial poll: %s', self._label(), exc)
            return 0, status_for(exc)

    def assert_trigger(self, protocol: constants.TriggerProtocol) -> StatusCode:
        if protocol != constants.TriggerProtocol.default:
            return StatusCode.error_nonsupported_operation
        return self._bus_operation(
            lambda c: ops.trigger(c, self._pad, self._sad, self._device_timeout()))

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
        return super()._get_attribute(attribute)

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
        return super()._set_attribute(attribute, attribute_state)


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
        if isinstance(parsed, rname.GPIBInstr) and registry().owns(parsed.board):
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
    """Put our dispatchers in front of pyvisa-py's ``(gpib, INSTR)`` and ``(gpib, INTFC)``. Idempotent."""
    import pyvisa_py  # noqa: F401 - registers pyvisa-py's own session classes first
    from . import visa_intfc  # here, not at the top: visa_intfc subclasses this module's session
    current = Session._session_classes.get(GPIB_INSTR)
    if current is not NiUsbGpibDispatch:
        NiUsbGpibDispatch.previous = current
        # Assigned directly: Session.register() logs a warning about overwriting
        # the existing class, and overwriting it is exactly what this does.
        Session._session_classes[GPIB_INSTR] = NiUsbGpibDispatch
        logger.debug('NI GPIB-USB session installed in front of %s',
                     current.__name__ if current is not None else 'nothing')
    visa_intfc.install()

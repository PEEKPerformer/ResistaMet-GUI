"""The model-specific steps 2 and 3 of the attach sequence (§2.8), part of ``Controller``.

The readiness poll and serial-number query of the HS family, the extra
requests of the HS+, and the USB-B's serial number read from registers.
``Controller.attach`` calls them in order; ``Controller`` inherits them
from ``_AttachMixin``, which has no state of its own.
"""
import logging

from . import protocol as p
from . import tables as t
from .protocol import AdapterNotReady, ProtocolError
from .transport import TransportTimeout

#: The controller's logger: these are its methods, and they log as it.
logger = logging.getLogger(__name__.rpartition('.')[0] + '.controller')


class _AttachMixin:
    """Steps 2 and 3 of ``Controller.attach`` (see the module docstring)."""

    def _readiness_poll(self) -> None:
        reply = self._control(t.SERIAL_NUMBER_QUERY)
        try:
            self.serial_number = p.parse_serial_number(reply)
        except ProtocolError as exc:
            raise AdapterNotReady(str(exc)) from exc
        for attempt in range(t.READINESS_ATTEMPTS):
            try:
                reply = self._control(t.READINESS_QUERY, timeout_ms=t.READINESS_USB_TIMEOUT_MS)
            except TransportTimeout:
                reply = b''
            if reply and p.readiness_reported(reply):
                return
            if attempt + 1 < t.READINESS_ATTEMPTS:
                self._sleep(t.READINESS_INTERVAL_S)
        raise AdapterNotReady('%s did not report ready after %d polls'
                              % (self._model.name, t.READINESS_ATTEMPTS))

    def _hs_plus_extras(self) -> None:
        for request, expected in t.HS_PLUS_INIT_REQUESTS:
            reply = self._control(request)
            if reply != expected:
                # spec gap: only the LED effect of these requests is known, so an
                # unexpected reply is recorded rather than treated as fatal.
                logger.warning('HS+ init request 0x%02x answered %s, expected %s',
                               request.request, reply.hex(), expected.hex())

    def _usb_b_serial(self) -> int:
        values = self._register_read(t.USB_B_SERIAL_REGISTERS)
        return int.from_bytes(bytes(values), 'little')

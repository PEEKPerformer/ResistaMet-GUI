"""The wait for a service request (§10.4), part of ``Controller``.

The adapter answers an SRQ by serial-polling the requesting device itself
and pushing the status byte on the interrupt endpoint (§10.4.2);
``wait_srq`` reads that endpoint on demand, with the controller lock
released while it blocks. ``Controller`` inherits it from ``_SrqMixin``,
which has no state of its own. See the note at the top of the class for
what is and is not established about it.
"""
from typing import Optional

from . import protocol as p
from . import tables as t
from .protocol import AdapterNotReady, GpibError, GpibTimeout
from .transport import Transport, TransportError, TransportGone, TransportTimeout

#: The interrupt read of ``wait_srq`` is issued in slices of this length so a
#: ``close`` is noticed between them; the only cost is one extra interrupt
#: read per slice while nothing is pending.
SRQ_WAIT_SLICE_S = 1.0


class _SrqMixin:
    """``Controller.wait_srq`` (see the module docstring)."""

    # ------------------------------------------------------------------
    # service request
    #
    # Status of this section: kept, not proven, and not reachable from the
    # application. pyvisa-py 0.8.1 has no enable_event / wait_on_event, so
    # nothing above the controller calls wait_srq; only tests do. It has never
    # run on hardware. And it rests on a premise the captures do not
    # establish: every capture with a push began after NI's driver already
    # owned the adapter, and in each NI had written its bank-2 session
    # configuration (0x04, 0x05 := PAD, 0x06 := SAD, §10.2.4) before the push
    # was seen -- the only way shown for the adapter to know which device to
    # poll. This driver writes neither those registers nor the monitor mask
    # §2.5 calls for, so whether the adapter pushes anything for it is open.
    # ------------------------------------------------------------------

    def wait_srq(self, timeout_s: Optional[float]) -> int:
        """Block until an instrument requests service; return its status byte (§10.4.2).

        Unproven: no production caller (pyvisa-py 0.8.1 offers no way to
        reach it), never run on hardware, and the pushes NI's driver got
        were all preceded by its bank-2 session configuration, which this
        driver does not write. Treat a timeout here as "no push", not as
        "no service request".

        The adapter answers an SRQ by polling the requesting device itself
        and pushing ``30 18 00 sb ..`` on the interrupt endpoint, ``sb``
        being the status byte with RQS set; a later explicit poll finds RQS
        clear (§10.4.2, §10.9). NI's driver then sends control request 0x3b
        and re-arms its interrupt read; this does the same, the re-arm being
        the next call.

        The interrupt read is issued only here, and the controller lock is
        released while it blocks: a permanently pending read would need a
        thread of its own, and every other operation would have to wait for
        this one otherwise. The read runs in slices of ``SRQ_WAIT_SLICE_S``
        so a ``close`` from another thread is noticed between them. The cost
        is that a push arriving while nobody waits sits in the adapter until
        the next call (which then returns at once); whether the adapter
        keeps more than one is not established. One wait at a time.
        ``timeout_s`` None waits the controller's infinite wait; it must
        otherwise be positive (``ValueError``): a zero wait would mean "is
        a push already queued?", which the interrupt endpoint cannot be
        asked without blocking -- libusb reads a timeout of 0 as no timeout
        at all -- so pretending to answer it would be wrong either way. A
        ``GpibTimeout`` means no request arrived in time.
        """
        if timeout_s is not None and not timeout_s > 0:
            raise ValueError('wait_srq needs a positive timeout or None, not %r' % (timeout_s,))
        with self._lock:
            self._ensure_attached()
            if not self._srq_idle.is_set():
                raise GpibError('a wait for a service request is already in progress')
            self._srq_idle.clear()
            transport = self._link.transport
        try:
            push = self._interrupt_read_in_slices(transport, timeout_s)
        finally:
            self._srq_idle.set()
        parsed = p.parse_srq_push(push)  # a malformed push does not put the bulk pipes out of step
        with self._guard():
            if self._closed:
                raise AdapterNotReady('controller is closed')
            transport.control_out(t.SRQ_ACKNOWLEDGE.request, t.SRQ_ACKNOWLEDGE.value,
                                  t.SRQ_ACKNOWLEDGE.index, b'', t.CONTROL_TIMEOUT_MS,
                                  request_type=t.SRQ_ACKNOWLEDGE.request_type)
        return parsed.status_byte

    def _interrupt_read_in_slices(self, transport: Transport, timeout_s: Optional[float]) -> bytes:
        """The next interrupt push, waited for in slices; called with the lock released.

        Counted in whole milliseconds, the unit the transport takes, and no
        slice is ever 0 ms: libusb reads that as no timeout at all, the read
        would never return, and ``close`` would give up waiting for it and
        release the transport under it. A remainder under a millisecond ends
        the wait instead.
        """
        total_s = self._link.infinite_wait_s if timeout_s is None else timeout_s
        remaining_ms = max(1, int(round(total_s * 1000)))
        slice_limit_ms = max(1, int(round(SRQ_WAIT_SLICE_S * 1000)))
        while True:
            if self._closed:
                raise AdapterNotReady('controller is closed')
            slice_ms = min(slice_limit_ms, remaining_ms)
            try:
                return transport.interrupt_in(t.INTERRUPT_READ_LENGTH, slice_ms)
            except TransportTimeout as exc:
                remaining_ms -= slice_ms
                if remaining_ms <= 0:
                    raise GpibTimeout('no service request within %.3g s' % total_s) from exc
            except TransportGone as gone:
                with self._lock:
                    raise self._adapter_gone(gone) from gone
            except TransportError:
                with self._lock:
                    self._link.resync_pending = True
                raise

"""The wait for a service request (§10.4), part of ``Controller``.

The adapter answers an SRQ by serial-polling the requesting device itself
and pushing the status byte on the interrupt endpoint (§10.4.2), but only
once it has been armed: with no 12-byte bank-2 0x03 write sent, no push
comes, and one write arms one push (§10.11). ``wait_srq`` sends that write
when it starts and every 15 ms while it waits, as NI does (§10.4.1,
§10.4.3), and reads the interrupt endpoint between the writes with the
controller lock released. ``Controller`` inherits it from ``_SrqMixin``,
which has no state of its own. See the note at the top of the class for
what has and has not run on hardware.
"""
import logging
from typing import Optional

from . import protocol as p
from . import tables as t
from .protocol import AdapterNotReady, GpibError, GpibTimeout
from .transport import Transport, TransportError, TransportGone, TransportTimeout

#: The controller's logger: these are its methods, and they log as it.
logger = logging.getLogger(__name__.rpartition('.')[0] + '.controller')

#: The interrupt read of ``wait_srq`` is issued in slices of this length, and
#: the arming write goes out again before each: NI sends it every 15 ms while
#: a wait has nothing pending (§10.4.3, about 65 writes in 1 s). A ``close``
#: is noticed between two slices.
SRQ_WAIT_SLICE_S = 0.015
#: How long the push is waited for once a write's reply has carried SRQI. On
#: the bench it "followed at once" (§10.11); this is a driver choice, with
#: room to spare, not a measured value.
SRQ_PUSH_AFTER_SRQI_S = 0.25


class _SrqMixin:
    """``Controller.wait_srq`` (see the module docstring)."""

    # ------------------------------------------------------------------
    # service request
    #
    # Status of this section. On the bench (01CEE482 with a 2400, 2026-09-23,
    # §10.11) the 12-byte bank-2 0x03 write sent once before *OPC made the
    # push arrive, `30 03 00 60 ..` with the status byte at byte 3, the 0x3b
    # request after it completed, and a write sent while SRQ was asserted
    # replied with SRQI in its ibsta and the push followed at once. Without
    # the write no push came in 5 s. The wait as written here -- the write
    # at the start and every 15 ms, SRQI taken as the request -- has not run
    # on an adapter as a whole. Nothing in the application calls it:
    # pyvisa-py 0.8.1 has no enable_event / wait_on_event.
    # ------------------------------------------------------------------

    def wait_srq(self, timeout_s: Optional[float]) -> Optional[int]:
        """Block until an instrument requests service; its status byte, or None (§10.4).

        The adapter pushes nothing on the interrupt endpoint until the
        12-byte bank-2 0x03 write arms it, one push per write (§10.11). This
        sends the write when the wait starts and again before every slice of
        ``SRQ_WAIT_SLICE_S`` (15 ms), which is what NI sends on
        viEnableEvent and while viWaitOnEvent waits (§10.4.1, §10.4.3), and
        reads the endpoint in between. Either of two things is the service
        request:

        - the push, ``30 ss ss sb ..``: the adapter has serial-polled the
          device itself and ``sb`` is its status byte with RQS set, which is
          returned; a later explicit poll finds RQS clear (§10.4.2, §10.11).
          Control request 0x3b follows it, as NI sends it (§10.4.2).
        - SRQI in the reply to a write, which the bench saw when the write
          went out with SRQ already asserted, the push following at once
          (§10.11). No more writes are sent then, and the push is waited for
          ``SRQ_PUSH_AFTER_SRQI_S`` more, the caller's timeout aside, since
          the request has been seen. If it comes, its status byte is
          returned as above. If it does not, the return is None: a request
          was made, and its status byte is not known here. Whether the
          adapter polled the device anyway is not established, so a serial
          poll then may or may not find RQS still set.

        No write is sent while no wait runs. (The same write is the last
        block of NI's raw messages, §10.2.5; whether it arms the push there
        as well has not been checked.) A push armed by a wait that timed
        out, for an SRQ after it, stays in the adapter until the next call,
        which then returns it at once; whether the adapter keeps more than
        one is not established.

        The interrupt read is issued only here, and the controller lock is
        released while it blocks: a permanently pending read would need a
        thread of its own, and every other operation would have to wait for
        this one otherwise. Each write takes the lock like any operation,
        re-attaching first after another thread's fault; a ``close`` or an
        adapter found gone ends the wait at the next slice. One wait at a
        time. ``timeout_s`` None waits the controller's infinite wait; it
        must otherwise be positive (``ValueError``): a zero wait would mean
        "is a push already queued?", which the interrupt endpoint cannot be
        asked without blocking -- libusb reads a timeout of 0 as no timeout
        at all -- so pretending to answer it would be wrong either way. The
        timeout counts the interrupt reads alone, so it is the least time
        waited, the writes adding their round trips. A ``GpibTimeout``
        means no request arrived in time.
        """
        if timeout_s is not None and not timeout_s > 0:
            raise ValueError('wait_srq needs a positive timeout or None, not %r' % (timeout_s,))
        with self._guard():
            self._ensure_attached()
            if not self._srq_idle.is_set():
                raise GpibError('a wait for a service request is already in progress')
            self._srq_idle.clear()
            transport = self._link.transport
        try:
            push = self._armed_interrupt_read(transport, timeout_s)
        finally:
            self._srq_idle.set()
        if push is None:
            return None
        parsed = p.parse_srq_push(push)  # a malformed push does not put the bulk pipes out of step
        with self._guard():
            if self._closed:
                raise AdapterNotReady('controller is closed')
            transport.control_out(t.SRQ_ACKNOWLEDGE.request, t.SRQ_ACKNOWLEDGE.value,
                                  t.SRQ_ACKNOWLEDGE.index, b'', t.CONTROL_TIMEOUT_MS,
                                  request_type=t.SRQ_ACKNOWLEDGE.request_type)
        return parsed.status_byte

    def _arm_srq(self) -> bool:
        """Send the 12-byte bank-2 0x03 write (§10.2.5, §10.11); whether its reply carried SRQI.

        Under the lock and the fault rule, as any operation: closed, gone
        or with a fault left by another thread, it raises or re-attaches
        before anything is sent.
        """
        with self._guard():
            self._ensure_attached()
            status = self._link.register_write((t.BANK2_SESSION_MARK_WRITE,), 'arm the service-request push')
        return bool(status.ibsta & t.IBSTA_SRQI)

    def _armed_interrupt_read(self, transport: Transport, timeout_s: Optional[float]) -> Optional[bytes]:
        """The next interrupt push, a write before each slice; None when SRQI came and no push.

        Called with the lock released; each write takes it. Counted in
        whole milliseconds, the unit the transport takes, and no slice is
        ever 0 ms: libusb reads that as no timeout at all, the read would
        never return, and ``close`` would give up waiting for it and release
        the transport under it. A remainder under a millisecond ends the
        wait instead.
        """
        total_s = self._link.infinite_wait_s if timeout_s is None else timeout_s
        remaining_ms = max(1, int(round(total_s * 1000)))
        slice_limit_ms = max(1, int(round(SRQ_WAIT_SLICE_S * 1000)))
        #: Once SRQI has been seen: what is left of the wait for its push.
        after_srqi_ms: Optional[int] = None
        while True:
            if after_srqi_ms is None:
                if self._arm_srq():
                    after_srqi_ms = max(1, int(round(SRQ_PUSH_AFTER_SRQI_S * 1000)))
            else:
                if self._closed:
                    raise AdapterNotReady('controller is closed')
                self._refuse_when_gone()  # another thread's operation may have found it gone
            slice_ms = min(slice_limit_ms, remaining_ms if after_srqi_ms is None else after_srqi_ms)
            try:
                return transport.interrupt_in(t.INTERRUPT_READ_LENGTH, slice_ms)
            except TransportTimeout as exc:
                if after_srqi_ms is not None:
                    after_srqi_ms -= slice_ms
                    if after_srqi_ms <= 0:
                        logger.info('%s: SRQI was set, and no push followed within %.3g s; the status '
                                    'byte is not known', self._link.model.name, SRQ_PUSH_AFTER_SRQI_S)
                        return None
                else:
                    remaining_ms -= slice_ms
                    if remaining_ms <= 0:
                        raise GpibTimeout('no service request within %.3g s' % total_s) from exc
            except TransportGone as gone:
                with self._lock:
                    raise self._adapter_gone(gone) from gone
            except TransportError as exc:
                with self._lock:
                    gone = self._link.gone_instead(exc)  # macOS: the error does not say (§10.11)
                    if gone is not None:
                        raise self._adapter_gone(gone) from gone
                    self._link.resync_pending = True
                raise

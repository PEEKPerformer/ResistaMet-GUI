"""Drive NI-VISA through one operation so the USB traffic can be captured.

Read-only toward the 2420 except the status-register scenarios, which restore
*SRE/*ESE to 0 before leaving.  Never touches output, source or measure setup.
"""
import sys, time
import pyvisa
from pyvisa import constants as c

ADDR = 'GPIB0::24::INSTR'

def stamp(msg):
    print('%.3f %s' % (time.perf_counter(), msg), flush=True)

def open_inst(rm, timeout_ms=20000):
    k = rm.open_resource(ADDR)
    k.timeout = timeout_ms
    return k

def sc_idn(rm):
    k = open_inst(rm)
    time.sleep(0.5); stamp('query *IDN?')
    print(repr(k.query('*IDN?')))
    time.sleep(0.5); k.close()

def sc_trac(rm):
    k = open_inst(rm)
    time.sleep(0.5); stamp('query :TRAC:DATA?')
    t = time.perf_counter(); d = k.query(':TRAC:DATA?'); dt = time.perf_counter() - t
    print('len', len(d), 'took %.3f s' % dt, 'head', repr(d[:60]), 'tail', repr(d[-30:]))
    time.sleep(0.5); stamp('query :TRAC:POIN:ACT?'); print(repr(k.query(':TRAC:POIN:ACT?')))
    time.sleep(0.5); k.close()

def sc_eos(rm):
    # character-terminated read: ask for the IDN with termchar enabled, then
    # the same with termchar disabled, then a read with a count smaller than
    # the reply so the library has to come back for the rest
    k = open_inst(rm)
    time.sleep(0.5)
    k.read_termination = '\n'; k.write_termination = '\n'
    stamp('termchar enabled query'); print(repr(k.query('*IDN?')))
    time.sleep(0.5)
    k.read_termination = None
    stamp('termchar disabled query'); print(repr(k.query('*IDN?')))
    time.sleep(0.5)
    stamp('write then read_raw(10) x3')
    k.write('*IDN?')
    for _ in range(3):
        try:
            print(repr(k.read_raw(10)))
        except Exception as e:
            print('read_raw ->', e); break
    time.sleep(0.5); k.close()

def sc_srq(rm):
    k = open_inst(rm)
    time.sleep(0.5)
    try:
        stamp('enable SRQ on OPC: *CLS; *ESE 1; *SRE 32')
        k.write('*CLS'); k.write('*ESE 1'); k.write('*SRE 32')
        time.sleep(0.5)
        stamp('viEnableEvent(SRQ, QUEUE)')
        k.enable_event(c.EventType.service_request, c.EventMechanism.queue)
        time.sleep(0.5)
        stamp('write *OPC (raises SRQ)')
        k.write('*OPC')
        stamp('wait_on_event 5 s')
        r = k.wait_on_event(c.EventType.service_request, 5000)
        stamp('event: %r timed_out=%r' % (getattr(getattr(r,'event',None),'event_type',None), r.timed_out))
        time.sleep(0.5)
        stamp('read_stb'); print('STB', k.read_stb())
        time.sleep(0.5)
        stamp('*ESR? (clears)'); print(repr(k.query('*ESR?')))
        stamp('disable_event'); k.disable_event(c.EventType.service_request, c.EventMechanism.queue)
        stamp('*STB?'); print(repr(k.query('*STB?')))
        time.sleep(0.5)
        stamp('wait_on_event with nothing pending, 1 s (expect timeout)')
        k.enable_event(c.EventType.service_request, c.EventMechanism.queue)
        try:
            k.wait_on_event(c.EventType.service_request, 1000)
            print('unexpected event')
        except Exception as e:
            print('timeout as expected:', type(e).__name__)
        k.disable_event(c.EventType.service_request, c.EventMechanism.queue)
    finally:
        stamp('restore *SRE 0; *ESE 0; *CLS')
        k.write('*SRE 0'); k.write('*ESE 0'); k.write('*CLS')
        print('SRE', k.query('*SRE?').strip(), 'ESE', k.query('*ESE?').strip(), 'STB', k.query('*STB?').strip())
        time.sleep(0.5); k.close()

def sc_stb(rm):
    k = open_inst(rm)
    time.sleep(0.5); stamp('read_stb x3')
    for _ in range(3):
        print('STB', k.read_stb()); time.sleep(0.2)
    time.sleep(0.5); k.close()

def sc_clear(rm):
    k = open_inst(rm)
    time.sleep(0.5); stamp('viClear (device clear)')
    k.clear()
    time.sleep(0.5); stamp('query *IDN? after clear'); print(repr(k.query('*IDN?')))
    time.sleep(0.5); k.close()

def sc_intfc(rm):
    b = rm.open_resource('GPIB0::INTFC'); time.sleep(0.5)
    stamp('send_ifc'); b.send_ifc(); time.sleep(0.5)
    stamp('gpib_control_ren ASSERT'); b.control_ren(c.RENLineOperation.asrt); time.sleep(0.5)
    stamp('gpib_control_atn ASSERT'); b.control_atn(c.ATNLineOperation.asrt); time.sleep(0.5)
    stamp('gpib_control_atn DEASSERT'); b.control_atn(c.ATNLineOperation.deassert); time.sleep(0.5)
    stamp('send_command UNL UNT'); b.send_command(b'\x3f\x5f'); time.sleep(0.5)
    stamp('VI_ATTR_GPIB_*'); 
    for a in ('VI_ATTR_GPIB_ATN_STATE','VI_ATTR_GPIB_REN_STATE','VI_ATTR_GPIB_NDAC_STATE','VI_ATTR_GPIB_SRQ_STATE','VI_ATTR_GPIB_CIC_STATE','VI_ATTR_GPIB_SYS_CNTRL_STATE','VI_ATTR_GPIB_HS488_CBL_LEN','VI_ATTR_GPIB_PRIMARY_ADDR'):
        try: print(a, b.get_visa_attribute(getattr(c, a)))
        except Exception as e: print(a, '->', e)
    time.sleep(0.5); b.close()

def sc_open(rm):
    stamp('open'); k = open_inst(rm); time.sleep(1.0); stamp('close'); k.close(); time.sleep(0.5)

SCENARIOS = {n[3:]: f for n, f in globals().items() if n.startswith('sc_')}



# ---- second batch (payload captures) ------------------------------------

def sc_write(rm):
    k = open_inst(rm)
    time.sleep(0.5); stamp('write *CLS (no read)'); k.write('*CLS')
    time.sleep(0.5); stamp('send_end False, write *CLS'); k.send_end = False; k.write('*CLS')
    time.sleep(0.5); stamp('send_end True, write_termination None, write_raw(b"*CLS")'); k.send_end = True; k.write_termination = None; k.write_raw(b'*CLS')
    time.sleep(0.5); stamp('write_termination "\\r\\n"'); k.write_termination = '\r\n'; k.write('*CLS')
    time.sleep(0.5); k.close()

def sc_trigger(rm):
    k = open_inst(rm)
    time.sleep(0.5); stamp('assert_trigger (GET)'); k.assert_trigger()
    time.sleep(0.5); stamp('*CLS'); k.write('*CLS')
    time.sleep(0.5); k.close()

def sc_nolistener(rm):
    stamp('open GPIB0::5::INSTR (nothing there)')
    try:
        k = rm.open_resource('GPIB0::5::INSTR'); k.timeout = 2000
        stamp('opened; write *IDN?')
        try:
            k.write('*IDN?'); print('write ok?!')
        except Exception as e:
            print('write ->', e)
        time.sleep(0.5); stamp('read')
        try:
            print(repr(k.read()))
        except Exception as e:
            print('read ->', e)
        time.sleep(0.5); k.close()
    except Exception as e:
        print('open ->', e)
    time.sleep(0.5)
    stamp('open GPIB0::24::1::INSTR (secondary address on the 2420)')
    try:
        k = rm.open_resource('GPIB0::24::1::INSTR'); k.timeout = 2000
        stamp('opened; query *IDN?')
        try:
            print(repr(k.query('*IDN?')))
        except Exception as e:
            print('query ->', e)
        time.sleep(0.5); k.close()
    except Exception as e:
        print('open ->', e)
    time.sleep(0.5)

def sc_timeouts(rm):
    k = open_inst(rm)
    for tmo in (100, 300, 1000, 3000, 10000, 30000, 100000, 300000, 1000000, float('+inf')):
        time.sleep(0.4)
        k.timeout = tmo
        stamp('timeout %r -> attr %r; query *IDN?' % (tmo, k.timeout))
        print(len(k.query('*IDN?')))
    time.sleep(0.5); k.close()

def sc_counts(rm):
    # which read mode does NI pick for which count?  ask for the IDN, then
    # read with an exact count; device-clear between rounds flushes the rest
    k = open_inst(rm, 3000)
    lib, s = k.visalib, k.session
    for n in (1, 2, 8, 15, 16, 30, 31, 32, 60, 63, 64, 65, 100, 127, 128, 255, 256, 511, 512, 1023, 1024, 4096, 20480):
        time.sleep(0.4)
        k.write('*IDN?')
        stamp('viRead count %d' % n)
        try:
            data, st = lib.read(s, n)
            print(n, '->', len(data), 'bytes, status', st)
        except Exception as e:
            print(n, '->', e)
        time.sleep(0.2); k.clear()
    time.sleep(0.5); k.close()

def sc_partial(rm):
    # a count smaller than the message, then the rest, without a clear in between
    k = open_inst(rm, 3000)
    lib, s = k.visalib, k.session
    time.sleep(0.5); k.write('*IDN?')
    stamp('viRead 10 then viRead 200')
    d1, st1 = lib.read(s, 10); print(repr(d1), st1)
    d2, st2 = lib.read(s, 200); print(repr(d2), st2)
    time.sleep(0.5); stamp('viRead 200 with nothing pending (timeout)')
    try:
        print(lib.read(s, 200))
    except Exception as e:
        print('->', e)
    time.sleep(0.5); k.close()

def sc_eosmodes(rm):
    k = open_inst(rm, 3000)
    lib, s = k.visalib, k.session
    for termchar_en, termchar in ((False, 10), (True, 10), (True, 44), (True, 13)):
        time.sleep(0.4)
        k.set_visa_attribute(c.ResourceAttribute.termchar_enabled, termchar_en)
        k.set_visa_attribute(c.ResourceAttribute.termchar, termchar)
        k.write('*IDN?')
        stamp('TERMCHAR_EN=%r TERMCHAR=%d viRead 200' % (termchar_en, termchar))
        try:
            d, st = lib.read(s, 200); print(len(d), repr(d[-12:]), st)
        except Exception as e:
            print('->', e)
        time.sleep(0.2); k.clear()
    time.sleep(0.5); k.close()

def sc_ren(rm):
    b = rm.open_resource('GPIB0::INTFC'); time.sleep(0.5)
    ops = ('deassert', 'asrt', 'deassert_gtl', 'asrt_address', 'address_gtl', 'asrt_llo', 'asrt_address_llo', 'address_gtl', 'deassert', 'asrt')
    for op in ops:
        stamp('control_ren ' + op)
        try:
            b.control_ren(getattr(c.RENLineOperation, op))
        except Exception as e:
            print('->', e)
        time.sleep(0.4)
    for op in ('asrt', 'asrt_immediate', 'deassert', 'deassert_handshake'):
        stamp('control_atn ' + op)
        try:
            b.control_atn(getattr(c.ATNLineOperation, op))
        except Exception as e:
            print('->', e)
        time.sleep(0.4)
    time.sleep(0.5); b.close()

def sc_board_io(rm):
    # board-level write and read after addressing by hand
    b = rm.open_resource('GPIB0::INTFC'); b.timeout = 3000; time.sleep(0.5)
    stamp('send_ifc (a fresh INTFC session is not CIC until then)'); b.send_ifc(); time.sleep(0.5)
    stamp('send_command UNL LAD24 MTA0'); b.send_command(bytes([0x3f, 0x20 + 24, 0x40 + 0])); time.sleep(0.3)
    stamp('board write *IDN?\\n'); b.write_raw(b'*IDN?\n'); time.sleep(0.3)
    stamp('send_command UNL TAD24 MLA0'); b.send_command(bytes([0x3f, 0x40 + 24, 0x20 + 0])); time.sleep(0.3)
    stamp('board read'); print(repr(b.read_raw(200))); time.sleep(0.3)
    stamp('send_command UNL UNT'); b.send_command(bytes([0x3f, 0x5f])); time.sleep(0.3)
    stamp('attributes')
    for a in ('VI_ATTR_GPIB_ATN_STATE', 'VI_ATTR_GPIB_REN_STATE', 'VI_ATTR_GPIB_NDAC_STATE', 'VI_ATTR_GPIB_SRQ_STATE', 'VI_ATTR_GPIB_CIC_STATE', 'VI_ATTR_GPIB_SYS_CNTRL_STATE', 'VI_ATTR_GPIB_ADDR_STATE'):
        try: print(a, b.get_visa_attribute(getattr(c, a)))
        except Exception as e: print(a, '->', e)
    time.sleep(0.5); b.close()

def sc_srq_poll(rm):
    # the same SRQ, but found by serial poll instead of the event queue
    k = open_inst(rm)
    time.sleep(0.5)
    try:
        stamp('*CLS; *ESE 1; *SRE 32'); k.write('*CLS'); k.write('*ESE 1'); k.write('*SRE 32'); time.sleep(0.5)
        stamp('*OPC'); k.write('*OPC'); time.sleep(0.5)
        stamp('VI_ATTR_GPIB_SRQ_STATE via INTFC')
        b = rm.open_resource('GPIB0::INTFC'); print('SRQ line', b.get_visa_attribute(c.VI_ATTR_GPIB_SRQ_STATE)); b.close(); time.sleep(0.5)
        stamp('read_stb'); print('STB', k.read_stb()); time.sleep(0.5)
        stamp('read_stb again'); print('STB', k.read_stb()); time.sleep(0.5)
    finally:
        stamp('restore'); k.write('*SRE 0'); k.write('*ESE 0'); k.write('*CLS')
        print('SRE', k.query('*SRE?').strip(), 'ESE', k.query('*ESE?').strip(), 'STB', k.query('*STB?').strip())
        time.sleep(0.5); k.close()

def sc_two_sessions(rm):
    # two VISA sessions to the same instrument in one process
    a = open_inst(rm); time.sleep(0.3); b = open_inst(rm); time.sleep(0.3)
    stamp('a: *IDN?'); print(len(a.query('*IDN?'))); time.sleep(0.3)
    stamp('b: *IDN?'); print(len(b.query('*IDN?'))); time.sleep(0.3)
    stamp('close a'); a.close(); time.sleep(0.3)
    stamp('b: *IDN?'); print(len(b.query('*IDN?'))); time.sleep(0.3)
    stamp('close b'); b.close(); time.sleep(0.5)

SCENARIOS = {n[3:]: f for n, f in globals().items() if n.startswith('sc_')}



def sc_longwrite(rm):
    # a write far longer than one bulk message: 2048 bytes of harmless *CLS;
    k = open_inst(rm)
    time.sleep(0.5); payload = ('*CLS;' * 410)[:2048]
    stamp('write %d bytes' % len(payload)); k.write(payload)
    time.sleep(0.5); stamp('*IDN?'); print(len(k.query('*IDN?')))
    time.sleep(0.5); k.close()

def sc_readtimeout_long(rm):
    # a 61 kB read with a 2 s timeout: how NI abandons a transfer in progress
    k = open_inst(rm, 2000)
    time.sleep(0.5); stamp('query :TRAC:DATA? with 2 s timeout')
    try:
        d = k.query(':TRAC:DATA?'); print('unexpected success', len(d))
    except Exception as e:
        print('->', e)
    time.sleep(1.0); stamp('clear'); k.clear()
    time.sleep(0.5); stamp('*IDN?'); print(len(k.query('*IDN?')))
    time.sleep(0.5); k.close()

def sc_terminate(rm):
    # viTerminate from another thread while a long read is in flight
    import threading
    k = open_inst(rm, 20000)
    time.sleep(0.5)
    lib, s = k.visalib, k.session
    def killer():
        time.sleep(1.5); stamp('viTerminate'); 
        try: lib.terminate(s, 0, 0)
        except Exception as e: print('terminate ->', e)
    threading.Thread(target=killer).start()
    stamp('query :TRAC:DATA?')
    try:
        d = k.query(':TRAC:DATA?'); print('read returned', len(d))
    except Exception as e:
        print('->', e)
    time.sleep(1.0); stamp('clear'); k.clear()
    time.sleep(0.5); stamp('*IDN?'); print(len(k.query('*IDN?')))
    time.sleep(0.5); k.close()

SCENARIOS = {n[3:]: f for n, f in globals().items() if n.startswith('sc_')}



# ---- third batch: thresholds, raw error paths, device start ---------------

def sc_read_thresholds(rm):
    # where does NI switch from the framed 0x0a read to the raw 0x0b read?
    k = open_inst(rm, 3000)
    lib, s = k.visalib, k.session
    for n in (1025, 1500, 2000, 2047, 2048, 2049, 3000, 4095, 4096):
        time.sleep(0.3); k.write('*IDN?')
        stamp('viRead count %d' % n)
        try:
            d, st = lib.read(s, n); print(n, '->', len(d), st)
        except Exception as e:
            print(n, '->', e)
        time.sleep(0.2); k.clear()
    time.sleep(0.5); k.close()

def sc_write_thresholds(rm):
    # where does NI switch from the framed 0x0d write to the raw 0x0e write?
    k = open_inst(rm, 3000)
    k.write_termination = None
    for n in (18, 24, 32, 48, 63, 64, 65, 100, 128, 255, 256, 257, 512, 1024, 1025, 2048, 2049):
        time.sleep(0.3)
        payload = ('*CLS;' * ((n // 5) + 2))[:n - 1] + '\n'
        assert len(payload) == n
        stamp('write %d bytes' % n); k.write_raw(payload.encode())
    time.sleep(0.5); stamp('*IDN?'); print(len(k.query('*IDN?')))
    time.sleep(0.5); k.close()

def sc_raw_errors(rm):
    # the raw instructions against an address nobody is at
    try:
        k = rm.open_resource('GPIB0::5::INSTR'); k.timeout = 2000
    except Exception as e:
        print('open GPIB0::5 ->', e); return
    time.sleep(0.5)
    stamp('long write (2500 B) to absent address')
    try:
        k.write('*CLS;' * 500); print('accepted?!')
    except Exception as e:
        print('->', e)
    time.sleep(0.5); stamp('viRead 20480 from absent address (raw read path)')
    try:
        print(k.visalib.read(k.session, 20480))
    except Exception as e:
        print('->', e)
    time.sleep(0.5); stamp('read_stb at absent address')
    try:
        print('STB', k.read_stb())
    except Exception as e:
        print('->', e)
    time.sleep(0.5); k.close()
    time.sleep(0.5); stamp('then a normal query on the 2420')
    k = open_inst(rm); print(len(k.query('*IDN?'))); time.sleep(0.5); k.close()

def sc_sad_poll(rm):
    # serial poll and clear through a secondary address
    k = rm.open_resource('GPIB0::24::1::INSTR'); k.timeout = 3000; time.sleep(0.5)
    stamp('read_stb via SAD 1'); print('STB', k.read_stb()); time.sleep(0.4)
    stamp('clear via SAD 1'); k.clear(); time.sleep(0.4)
    stamp('trigger via SAD 1'); k.assert_trigger(); time.sleep(0.4)
    stamp('*CLS'); k.write('*CLS'); time.sleep(0.5); k.close()

def sc_ren_device(rm):
    # the REN modes that need a device, on the instrument session
    k = open_inst(rm); time.sleep(0.5)
    for op in ('asrt_address', 'asrt_llo', 'asrt_address_llo', 'address_gtl', 'deassert_gtl', 'asrt'):
        stamp('control_ren ' + op)
        try:
            k.control_ren(getattr(c.RENLineOperation, op))
        except Exception as e:
            print('->', e)
        time.sleep(0.4)
    stamp('*IDN? afterwards'); print(len(k.query('*IDN?')))
    time.sleep(0.5); k.close()

def sc_first_open(rm):
    # run right after the adapter was restarted under capture: the first
    # session open since device start
    stamp('list_resources'); print(rm.list_resources())
    time.sleep(0.5); k = open_inst(rm); time.sleep(0.5)
    stamp('*IDN?'); print(repr(k.query('*IDN?')))
    time.sleep(0.5); k.close()

SCENARIOS = {n[3:]: f for n, f in globals().items() if n.startswith('sc_')}



def sc_timeout_expiry(rm):
    # how long does the adapter really wait under each timeout code?  a read
    # with nothing pending, at each VISA timeout; the capture times the
    # instruction to its reply
    k = open_inst(rm)
    lib, s = k.visalib, k.session
    for tmo in (100, 300, 1000, 3000, 10000, 30000):
        time.sleep(0.4)
        k.timeout = tmo
        stamp('timeout %d ms: viRead 100 with nothing pending' % tmo)
        t = time.perf_counter()
        try:
            print(lib.read(s, 100))
        except Exception as e:
            print('-> %s after %.3f s' % (type(e).__name__, time.perf_counter() - t))
    time.sleep(0.4); k.timeout = 3000
    stamp('*CLS; *IDN?'); k.write('*CLS'); print(len(k.query('*IDN?')))
    time.sleep(0.5); k.close()

SCENARIOS = {n[3:]: f for n, f in globals().items() if n.startswith('sc_')}


# ---- fourth batch (2026-09-22): the timeout code map, what the code bounds,
# ---- the count field above 0xffff, REN deassert on an instrument session

def sc_timeout_map(rm):
    # which timeout code NI sends for each VISA timeout, and how long the
    # adapter waits under it: a read with nothing pending at each value,
    # including the short ones below 100 ms that no earlier capture used
    k = open_inst(rm)
    lib, s = k.visalib, k.session
    for tmo in (1, 2, 3, 5, 10, 20, 30, 50, 60, 150, 200, 250, 500, 2000, 5000, 20000):
        time.sleep(0.4)
        k.timeout = tmo
        stamp('timeout %d ms: viRead 100 with nothing pending' % tmo)
        t = time.perf_counter()
        try:
            print(lib.read(s, 100))
        except Exception as e:
            print('-> %s after %.3f s' % (type(e).__name__, time.perf_counter() - t))
    time.sleep(0.4); k.timeout = 3000
    stamp('*CLS; *IDN?'); k.write('*CLS'); print(len(k.query('*IDN?')))
    time.sleep(0.5); k.close()

def sc_timeout_bound(rm):
    # does the timeout code bound the whole instruction or an interval inside
    # it?  :TRAC:DATA? (about 61 kB, read in 20480-byte chunks of several
    # seconds each) under VISA timeouts far shorter than one chunk takes
    for tmo in (1000, 300):
        k = open_inst(rm, tmo)
        time.sleep(0.5); stamp('query :TRAC:DATA? with %d ms timeout' % tmo)
        t = time.perf_counter()
        try:
            d = k.query(':TRAC:DATA?'); print('complete', len(d), 'in %.3f s' % (time.perf_counter() - t))
        except Exception as e:
            print('-> %s after %.3f s' % (e, time.perf_counter() - t))
        time.sleep(1.0); stamp('clear'); k.clear()
        time.sleep(0.5); k.timeout = 3000; stamp('*IDN?'); print(len(k.query('*IDN?')))
        time.sleep(0.5); k.close()

def sc_count_width(rm):
    # a raw read whose count does not fit in 16 bits
    k = open_inst(rm, 3000)
    lib, s = k.visalib, k.session
    for n in (0xffff, 0x10000, 70000, 200000):
        time.sleep(0.3); k.write('*IDN?')
        stamp('viRead count %d' % n)
        try:
            d, st = lib.read(s, n); print(n, '->', len(d), st)
        except Exception as e:
            print(n, '->', e)
        time.sleep(0.2); k.clear()
    time.sleep(0.5); k.close()

def sc_ren_deassert(rm):
    # VI_GPIB_REN_DEASSERT on an instrument session, the one REN mode the
    # earlier captures left out; then a query, which has to reassert REN
    k = open_inst(rm); time.sleep(0.5)
    stamp('control_ren deassert')
    try:
        k.control_ren(c.RENLineOperation.deassert)
    except Exception as e:
        print('->', e)
    time.sleep(0.5); stamp('*IDN? afterwards'); print(len(k.query('*IDN?')))
    time.sleep(0.5); k.close()

SCENARIOS = {n[3:]: f for n, f in globals().items() if n.startswith('sc_')}


if __name__ == '__main__':
    name = sys.argv[1]
    rm = pyvisa.ResourceManager()
    stamp('RM open, scenario ' + name)
    SCENARIOS[name](rm)
    stamp('done')

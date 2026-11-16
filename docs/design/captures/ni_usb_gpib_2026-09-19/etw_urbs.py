"""Timeline of the GPIB-USB-HS's URBs from a tracerpt XML of a USBPORT trace."""
import re, sys, collections
x = open(sys.argv[1], encoding='utf-8', errors='replace').read()
evs = re.findall(r'<Event[^>]*>.*?</Event>', x, re.S)
g = [e for e in evs if 'idVendor">0x3923' in e]
def f(e, n):
    m = re.search(r'<Data Name="%s">([^<]*)</Data>' % re.escape(n), e); return m.group(1).strip() if m else None
rows = []
for e in g:
    ts = re.search(r'SystemTime="([^"]+)"', e).group(1)
    h, m_, s = ts[11:13], ts[14:16], float(ts[17:29].rstrip('Z-+0123456789:') or ts[17:29].split('-')[0])
    t = int(h)*3600 + int(m_)*60 + float(re.match(r'[\d.]+', ts[17:]).group(0))
    rows.append(dict(t=t, task=re.search(r'<Task>([^<]*)</Task>', e).group(1), op=int(re.search(r'<Opcode>([^<]*)</Opcode>', e).group(1)),
        ep=f(e,'fid_bEndpointAddress'), ln=f(e,'fid_URB_TransferBufferLength'), st=f(e,'fid_URB_Hdr_Status'), fl=f(e,'fid_URB_TransferFlags'),
        setup=[f(e,k) for k in ('fid_URB_Setup_bmRequestType','fid_URB_Setup_bRequest','fid_URB_Setup_wValue','fid_URB_Setup_wIndex','fid_URB_Setup_wLength')],
        pid=re.search(r'ProcessID="(\d+)"', e).group(1)))
if not rows: print('no adapter events'); sys.exit()
t0 = rows[0]['t']
summary = sys.argv[2:] and sys.argv[2] == 'summary'
opname = {25: 'DISPATCH', 26: 'COMPLETE'}
counts = collections.Counter()
for r in rows:
    if r['op'] not in opname: continue
    ln = int(r['ln'], 16) if r['ln'] else 0
    counts[(r['ep'], opname[r['op']])] += 1
    if summary: continue
    setup = ' setup=' + ' '.join(str(s) for s in r['setup']) if r['setup'][1] else ''
    print('%9.3f %-8s ep %-4s len %6d%s%s' % (r['t'] - t0, opname[r['op']], r['ep'], ln, ' status=' + r['st'] if r['st'] not in ('0x0', None) else '', setup))
print('---', collections.OrderedDict(sorted(counts.items(), key=lambda kv: str(kv[0]))))

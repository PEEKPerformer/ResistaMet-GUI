"""Dump a USBPcap pcap (linktype 249) as a readable USB transaction log."""
import struct, sys

def packets(path):
    b = open(path, 'rb').read()
    magic, = struct.unpack_from('<I', b, 0)
    assert magic in (0xa1b2c3d4, 0xa1b23c4d), hex(magic)
    nano = magic == 0xa1b23c4d
    linktype, = struct.unpack_from('<I', b, 20)
    assert linktype == 249, linktype
    off = 24
    while off + 16 <= len(b):
        ts_s, ts_u, incl, orig = struct.unpack_from('<IIII', b, off); off += 16
        yield ts_s + ts_u / (1e9 if nano else 1e6), b[off:off + incl]; off += incl

FUNC = {0x08: 'CONTROL', 0x09: 'BULK/INTR', 0x0b: 'ISOCH', 0x00: 'SELECT_CONF', 0x01: 'SELECT_IF', 0x02: 'ABORT_PIPE', 0x07: 'GET_DESCRIPTOR', 0x1e: 'RESET_PIPE', 0x20: 'CLASS_IF', 0x1f: 'CLASS_DEV', 0x17: 'VENDOR_DEV', 0x18: 'VENDOR_IF'}
TRANSFER = {0: 'ISOCH', 1: 'INTR', 2: 'CONTROL', 3: 'BULK', 254: 'IRP_INFO', 255: 'UNKNOWN'}

def dump(path, out=sys.stdout, want_device=None, full=False):
    t0 = None
    for ts, pkt in packets(path):
        hdr_len, irp_id, status, function, info, bus, device, endpoint, transfer, data_len = struct.unpack_from('<HQIHBHHBBI', pkt, 0)
        if want_device is not None and device != want_device:
            continue
        if t0 is None: t0 = ts
        payload = pkt[hdr_len:hdr_len + data_len]
        direction = 'IN ' if endpoint & 0x80 else 'OUT'
        pdo = 'PDO->' if info & 1 else 'FDO->'   # info bit0: 1 = from PDO (completion), 0 = to PDO (request)
        stage = ''
        if transfer == 2 and hdr_len > 27:
            stage = {0: 'SETUP', 1: 'DATA', 2: 'STATUS', 3: 'COMPLETE'}.get(pkt[27], '?')
        line = '%9.4f dev%-2d ep%02x %s %-6s %-5s %-8s len %5d status %08x' % (ts - t0, device, endpoint, direction, TRANSFER.get(transfer, transfer), 'cmpl' if info & 1 else 'req ', stage, data_len, status)
        if payload:
            hx = payload.hex(' ')
            if len(payload) > 96 and not full:
                hx = payload[:64].hex(' ') + ' ... ' + payload[-32:].hex(' ')
            line += '\n            ' + hx
            printable = bytes(c if 32 <= c < 127 else 46 for c in (payload if full else payload[:96])).decode()
            line += '\n            |' + printable + '|'
        print(line, file=out)

if __name__ == '__main__':
    # usage: usbpcap_dump.py <file.pcap> [device-address] [--full]
    #   --full prints whole payloads instead of a 64-byte head and 32-byte tail
    args = [a for a in sys.argv[1:] if a != '--full']
    dump(args[0], want_device=int(args[1]) if len(args) > 1 else None, full='--full' in sys.argv)

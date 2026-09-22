# USB captures of NI-488.2 driving a GPIB-USB-HS — 2026-09-22

A fourth batch beside `../ni_usb_gpib_2026-09-19/`, on the same PC, adapter
and instrument and with the same method (see that README for the setup
table and how to read a pcap). `scenario.py` here is the earlier file with
the four new scenarios appended; `capture.ps1` and `usbpcap_dump.py` are
unchanged copies. `SHA256SUMS` covers the pcaps.

| | |
|---|---|
| PC | lab desktop, Windows 10, NI-488.2 / NI-VISA (`visa32.dll`), pyvisa on the vendor library |
| Adapter | NI GPIB-USB-HS, serial 013CC9DF, USB device address 2 |
| Instrument | Keithley 2420, GPIB primary address 24, output off throughout; the trace buffer held 1103 readings from earlier use |

## Scenarios

| pcap | VISA operations |
|---|---|
| `timeout_map` | `viRead` 100 with nothing pending at `VI_ATTR_TMO_VALUE` = 1, 2, 3, 5, 10, 20, 30, 50, 60, 150, 200, 250, 500, 2000, 5000, 20000 ms; then `*CLS`, `*IDN?` |
| `timeout_bound` | `:TRAC:DATA?` (about 61 kB) at 1000 ms, then `viClear` and `*IDN?`; the same at 300 ms |
| `count_width` | `*IDN?` then `viRead` with count 65535, 65536, 70000, 200000 (`viClear` between rounds) |
| `ren_deassert` | `viGpibControlREN(VI_GPIB_REN_DEASSERT)` on the instrument session, then `*IDN?` in the same session (20 s timeout) |

## What stdout shows, before any decoding

- `timeout_map`: the elapsed time to `VI_ERROR_TMO` falls into steps: 1 ms →
  0.002 s; 2, 3 → 0.006; 5, 10 → 0.018; 20, 30 → 0.035; 50, 60 → 0.133;
  150, 200, 250 → 0.264; 500 → 1.050; 2000 → 4.196; 5000 → 16.779;
  20000 → 33.556 s.
- `timeout_bound`: both long reads end in `VI_ERROR_TMO`, after 1.053 s
  and 0.267 s; the `*IDN?` after each `viClear` answers.
- `count_width`: every count returns the 82-byte message.
- `ren_deassert`: the query after the deassert times out; a fresh session
  opened afterwards answered normally.

Which timeout code went out for each value, how many bytes of the trace
crossed before each timeout, and what the adapter was sent for each count
are for the protocol specification (§7.3, §10, §11), decoded from the
pcaps.

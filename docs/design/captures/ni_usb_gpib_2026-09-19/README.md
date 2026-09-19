# USB captures of NI-488.2 driving a GPIB-USB-HS — 2026-09-19

Ground truth for `docs/design/ni_usb_gpib_protocol.md`: the bytes between our
own PC and our own adapter while NI's driver stack does one VISA operation at a
time. Nothing here comes from any program's source code.

| | |
|---|---|
| PC | lab desktop, Windows 10 19045, NI-488.2 / NI-VISA 22.5 (`visa32.dll`), pyvisa 1.16.2 on the vendor library |
| Adapter | NI GPIB-USB-HS, USB `VID_3923 PID_709B`, serial 013CC9DF, USB device address 2 on the Intel 8-series EHCI #1 root hub |
| Instrument | Keithley 2420, serial 1230523, GPIB primary address 24, output off throughout |
| Capture | USBPcap 1.5.4.0 (`USBPcapCMD -d \\.\USBPcap1 --devices 2 -s 65535`), pcap link type 249 |
| Driver | `scenario.py <name>` (one VISA operation per scenario, timestamps on stdout → `<name>.stdout.txt`); `capture.ps1 -Scenario <name>` wraps it in USBPcapCMD |
| Reading them | `python usbpcap_dump.py <name>.pcap 2` prints every transaction with payload hex and ASCII (no Wireshark needed). `SHA256SUMS` covers the pcaps. |

Also here: `trace.bat` + `etw_urbs.py`, the earlier header-only method
(Windows USB ETW, no payloads), kept because it needs nothing installed.

## Scenarios

| pcap | VISA operations |
|---|---|
| `open` | `viOpen` on `GPIB0::24::INSTR`, 1 s idle, `viClose` |
| `idn` | `*IDN?` query |
| `stb` | three `viReadSTB` |
| `clear` | `viClear`, then `*IDN?` |
| `write` | `*CLS` writes: default; `VI_ATTR_SEND_END_EN` false; no termination via `viWrite` raw; `\r\n` termination |
| `trigger` | `viAssertTrigger` (GET), then `*CLS` |
| `nolistener` | open `GPIB0::5::INSTR` (nothing at 5): write → `VI_ERROR_NLISTENERS`, read → `VI_ERROR_TMO`; then open `GPIB0::24::1::INSTR` (secondary address 1 on the 2420) and `*IDN?` — answered |
| `timeouts` | `*IDN?` with `VI_ATTR_TMO_VALUE` = 100, 300, 1000, 3000, 10000, 30000, 100000, 300000, 1000000 ms and `VI_TMO_INFINITE` |
| `counts` | `*IDN?` then `viRead` with count 1, 2, 8, 15, 16, 30, 31, 32, 60, 63, 64, 65, 100, 127, 128, 255, 256, 511, 512, 1023, 1024, 4096, 20480 (`viClear` between rounds); counts ≤ 65 returned exactly the count with `VI_SUCCESS_MAX_CNT`, ≥ 100 returned the whole 82-byte message |
| `partial` | `viRead` 10 then `viRead` 200 of the same message; then `viRead` 200 with nothing pending (timeout, 3 s) |
| `eosmodes` | `viRead` 200 with `TERMCHAR_EN` false; true with `\n`; true with `,` (returned 26 bytes); true with `\r` |
| `eos` | query with and without termination character, then `viRead` count 10 repeated until `VI_ERROR_TMO` (20 s timeout) |
| `trac` | `:TRAC:DATA?` → 61 768 bytes in four `viRead` of 20 480 (pyvisa chunk size); `:TRAC:POIN:ACT?` |
| `srq` | `*CLS; *ESE 1; *SRE 32`, `viEnableEvent(SRQ, QUEUE)`, `*OPC`, `viWaitOnEvent` (fired in 8 ms), `viReadSTB` (96), `*ESR?`, `viDisableEvent`, `*STB?`, a second wait with nothing pending (timeout), restore `*SRE 0; *ESE 0; *CLS` |
| `srq_poll` | same SRQ set-up, read `VI_ATTR_GPIB_SRQ_STATE` on `GPIB0::INTFC` (0 — see stdout), `viReadSTB` twice (96 then 32), restore |
| `intfc` | on `GPIB0::INTFC`: `viGpibSendIFC`, REN assert, ATN assert, ATN deassert, `viGpibCommand(UNL UNT)`, `VI_ATTR_GPIB_*` reads |
| `ren` | every `RENLineOperation` and `ATNLineOperation` on `GPIB0::INTFC` without a prior IFC: only `deassert` and `asrt` succeed; the addressed modes → `VI_ERROR_INV_MODE`; LLO and all ATN modes → `VI_ERROR_NCIC` (a fresh INTFC session is not controller-in-charge) |
| `board_io` | `viGpibSendIFC`, `viGpibCommand(UNL LAD24 MTA0)`, board-level `viWrite("*IDN?\n")`, `viGpibCommand(UNL TAD24 MLA0)`, board-level `viRead` → `VI_ERROR_TMO` |
| `two_sessions` | two sessions to the same instrument in one process: `*IDN?` on each, close one, `*IDN?` on the other |
| `timeout_expiry` | `viRead` with nothing pending at `VI_ATTR_TMO_VALUE` = 100, 300, 1000, 3000, 10000, 30000 ms — `VI_ERROR_TMO` after 0.133, 0.264, 1.050, 4.196, 16.780 and 33.556 s: how long the adapter really waits under each timeout code |
| `read_thresholds` | `*IDN?` then `viRead` with count 1025, 1500, 2000, 2047, 2048, 2049, 3000, 4095, 4096 — where the framed read gives way to the raw one |
| `write_thresholds` | `viWrite` of 18, 24, 32, 48, 63, 64, 65, 100, 128, 255, 256, 257, 512, 1024, 1025, 2048, 2049 bytes (`*CLS;` repeated, `\n`-terminated) — where the framed write gives way to the raw one |
| `raw_errors` | at `GPIB0::5::INSTR` (nothing there): a 2500-byte write → `VI_ERROR_NLISTENERS`; `viRead` 20480 → `VI_ERROR_TMO`; `viReadSTB` → `VI_ERROR_TMO`; then a normal `*IDN?` on the 2420 |
| `sad_poll` | through `GPIB0::24::1::INSTR`: `viReadSTB`, `viClear`, `viAssertTrigger`, `*CLS` — the secondary-address forms |
| `ren_device` | on the instrument session: `viGpibControlREN` with `asrt_address`, `asrt_llo`, `asrt_address_llo`, `address_gtl`, `deassert_gtl`, `asrt`, then `*IDN?` |
| `longwrite` | a 2048-byte write (`*CLS;` repeated), then `*IDN?` |
| `readtimeout_long` | `:TRAC:DATA?` (61 768 bytes, ~12 s) with `VI_ATTR_TMO_VALUE` = 2000 ms — completed anyway; the timeout is not a total-transfer limit |
| `terminate` | `viTerminate` from another thread 1.5 s into the same read — no effect on the synchronous read, which completed |

Only `srq` and `srq_poll` change instrument state (`*ESE`, `*SRE`); both
restore `*SRE 0; *ESE 0` and the stdout files show the read-back. `trigger`
sends GET with the output off.

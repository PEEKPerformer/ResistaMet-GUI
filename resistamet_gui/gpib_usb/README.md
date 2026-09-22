# NI GPIB-USB adapters for pyvisa-py

A user-space driver for National Instruments GPIB-USB adapters. It talks to
the adapter over libusb through pyusb and registers itself with
[pyvisa-py](https://github.com/pyvisa/pyvisa-py), so an instrument behind the
adapter opens as `GPIB0::24::INSTR` and the adapter itself as `GPIB0::INTFC`.
No NI software and no kernel driver are needed. MIT licence.

It was written because NI stopped shipping a GPIB driver for macOS: the last
one, NI-488.2 21.5.1, has a kernel extension that does not load on macOS 13
or later.

The package lives inside [ResistaMet](../../README.md) at
`resistamet_gui/gpib_usb/` and imports nothing else from it. Importing it
loads `resistamet_gui/__init__.py`, which reads the version constants and
nothing more. `tests/test_gpib_usb_self_contained.py` fails if the package
imports anything but itself, the standard library, `usb`, `pyvisa` and
`pyvisa_py`. How ResistaMet uses it is in [`docs/gpib.md`](../../docs/gpib.md).

## What has run on hardware

Two GPIB-USB-HS units have been used. This driver has run on one of them.

| | Status |
|---|---|
| Attach, addressing, the framed read and write instructions (0x0a, 0x0d), command bytes, the presence probe, shutdown | Run on GPIB-USB-HS serial 01CEE482 with a Keithley 2400, macOS, pyusb and libusb, 2026-09-18: identify, resistance runs, stop, restart, shutdown mid-run, compliance detection. **This is what the driver does by default.** |
| Timeout expiries, long answers read as framed pieces of 1024 bytes, writes of a multiple of 512 bytes, unplug and replug mid-run | The same unit, 2026-09-21 (spec §7.3, §11.2) |
| NI's own instructions: raw reads and writes on the second endpoint pair (0x0b, 0x0e), the serial poll as one instruction (0x10) | Written from USB captures of NI-488.2 on Windows driving the second unit, serial 013CC9DF, with a Keithley 2420 (spec §10). A 0x0b and a 0x10 were tried on 01CEE482 on 2026-09-21 (spec §11.2). The raw read and write have since been changed to send NI's messages byte for byte, and have not run on an adapter in that form. **Off by default.** |
| The `GPIB0::INTFC` board resource | Not run on an adapter |
| The service-request wait | Not run on an adapter, and not reachable through pyvisa-py 0.8.1, which has no event API |

## What it supports

**Adapters.** The device table (`tables.MODELS`, spec §1) lists, under
vendor id 0x3923:

| Product id | Model | Notes |
|---|---|---|
| 0x709b | GPIB-USB-HS | The only model connected so far. The only one that takes NI's instructions. |
| 0x7618 | GPIB-USB-HS+ | Never connected. Framed instructions only. |
| 0x702a | GPIB-USB-B | Never connected. Framed instructions only. |
| 0x702b | GPIB-USB-B before its firmware is loaded | Listed with `needs_firmware` set; cannot be driven |
| 0x725c | Keithley KUSB-488A | Never connected. Framed instructions only. |
| 0x725d | Measurement Computing USB-488 | Never connected. Framed instructions only. |

The other models' entries come from the GPL sources the specification was
written from, and what their second endpoint pair carries is not
established (spec §1.2, §11.4). NI's instructions are therefore used on the
GPIB-USB-HS alone; asking for them on another model logs a warning and
keeps the framed paths.

**Operating systems.**

| | Status |
|---|---|
| macOS | Run on hardware (above) |
| Linux | The same code. Never run on hardware. See [Linux](#linux). |
| Windows | Out of scope. NI-488.2 owns the device there; use NI-VISA. |

**Operations.** On `GPIB<n>::<pad>[::<sad>]::INSTR`: read, write, device
clear, trigger, serial poll, the REN line operations, timeouts and the
termination character. On `GPIB<n>::INTFC`: IFC, REN and ATN, command bytes,
data to and from whoever is addressed, and the bus line states.

**Not supported.** Pass control, on both resources (spec §5.17 leaves the
adapter's report of the hand-over uncertain). `VI_ATTR_SUPPRESS_END_EN` set
to true. On the interface: `ATNLineOperation.deassert_handshake`, the REN
operations that address a device, `VI_ATTR_GPIB_HS488_CBL_LEN`, and changing
the adapter's own address or system-controller role, which read back as the
attach set them. `Session.lock` is not implemented. Parallel poll is not
offered.

The full list of what the specification leaves open is its §11.

## Requirements

- Python 3.10 or later, for the pyvisa-py sessions. The layers below them
  (protocol, transport, controller) run on 3.9.
- pyvisa-py 0.8 or later, whose `Session` API the sessions are written
  against. pyvisa-py 0.8 requires Python 3.10. pyvisa at whatever version
  that pyvisa-py requires.
- pyusb 1.2 or later.
- A libusb-1.0 shared library. macOS: `brew install libusb`. Linux: the
  distribution's `libusb-1.0` package. A `libusb-1.0` inside a PyInstaller
  bundle, or beside the Python executable, is used before the system's.

The tests were last run with pyvisa 1.16.2, pyvisa-py 0.8.1 and pyusb 1.3.1.

If pyusb, libusb or a new enough pyvisa-py is missing, `install()` registers
nothing and pyvisa-py behaves as shipped. `available()` says whether pyusb
imports and libusb loads.

## Use

```python
import pyvisa
from resistamet_gui import gpib_usb

gpib_usb.install()                      # once, before opening resources

rm = pyvisa.ResourceManager('@py')
print(rm.list_resources())              # a bus scan; see below
inst = rm.open_resource('GPIB0::24::INSTR')
print(inst.query('*IDN?'))
inst.close()

board = rm.open_resource('GPIB0::INTFC')
```

`install()` puts a dispatcher in front of pyvisa-py's own `(gpib, INSTR)` and
`(gpib, INTFC)` sessions. Boards that belong to an NI USB adapter go to this
package; every other board goes to whatever pyvisa-py had before (linux-gpib
or a Prologix adapter), and `list_resources()` merges the two. It is
idempotent.

`gpib_usb.find_adapters()` lists the adapters on USB by enumeration alone,
without sending them anything.

- **Board numbers.** The first adapter is `GPIB0`, unless linux-gpib is
  installed and already has boards, in which case NI adapters are numbered
  after them. An adapter keeps its number, by USB serial, for the life of
  the process, including across an unplug and replug.
- **Listing scans the bus.** `list_resources()` attaches each adapter
  (interface clear, remote enable, take control) and addresses each of the
  31 primary addresses in turn to see who listens. Other instruments on the
  bus see it.
- **Sessions share a board.** Every session on one adapter shares one
  attached controller, opened with the first session and closed with the
  last. Each call is atomic on the board; a sequence of calls is not.
- **An unplugged adapter** is `VI_ERROR_CONN_LOST` on that operation and
  every later one on the session. After a replug the next open finds it
  again with no restart.

## NI's instructions

By default every transfer uses the framed instructions that ran on the
bench, in pieces of at most 1024 bytes, and the serial poll is the
IEEE-488.1 command sequence (spec §5.9). To use the instructions NI's driver
was captured sending instead (0x0b and 0x0e for large transfers, 0x10 for
the serial poll), set

```bash
export NI_GPIB_USB_INSTRUCTIONS=1
```

`1`, `true`, `yes` or `on` switch them on; unset, `0` or anything else keeps
the framed paths. The variable is read each time a board is opened, and the
attach log line (logger `resistamet_gui.gpib_usb.boards`, INFO) names the
mode in force.

Two older names are read as aliases, with the same values:
`RESISTAMET_GPIB_NI_INSTRUCTIONS` and `RESISTAMET_GPIB_RAW_TRANSFERS`. The
first of `NI_GPIB_USB_INSTRUCTIONS`, `RESISTAMET_GPIB_NI_INSTRUCTIONS`,
`RESISTAMET_GPIB_RAW_TRANSFERS` that is set decides, whatever its value.

Which read instruction goes out depends on how many bytes the caller asks
for, not on how many arrive, and pyvisa asks for 20 480 at a time. With the
switch on, every read, a one-line reply included, takes the raw path that
has not run on an adapter.

## What a timeout means

`VI_ATTR_TMO_VALUE` is the least time to wait before reporting a timeout,
as in VISA. It reads back as set, capped at 1000 s.

The adapter takes no timeout in seconds. Each instruction carries a
one-byte code from a fixed table (spec §7.1), and the adapter ends the
instruction when that code's time runs out. A timeout goes out as NI's
code, the smallest nominal limit not below it, except where that code ends
sooner than the timeout on a unit that was timed: 264 to 300 ms go out as
0xfb, and 268 to 300 s as 0x02.

What the adapter then waits differs by unit (spec §7.3):

| Code | Nominal limit | 013CC9DF under NI's driver | 01CEE482 under this driver |
|---|---|---|---|
| 0xf9 | 100 ms | 0.132 s | 0.127 s (a session total; the wire was not logged) |
| 0xfb | 1 s | 1.050 s | 1.250 s |
| 0xfc | 3 s | 4.196 s | 3.750 s |
| 0xfd | 10 s | 16.778 s | 20.0 s |
| 0xfe | 30 s | 33.556 s | 41.25 s |

The codes below 0xf9 were timed on 013CC9DF only, the codes above 0xfe on
neither. The full table is in the `controller` docstring.

- A read is bounded as a whole, as NI's single instruction is, by the
  longer of the two units' expiries for its code, not by the value set: a
  5 s timeout can take 20 s to report. A read that runs out returns the
  bytes read so far with `VI_ERROR_TMO`.
- A framed read in pieces gives each piece the timeout's own code, so the
  last piece may run up to one piece past the bound. Split writes likewise.
- `VI_TMO_IMMEDIATE` goes out as 100 ms, code 0xf9. What NI sends for it
  was not captured.
- `VI_TMO_INFINITE` is code 0xf0, under which the adapter never ends an
  instruction. The driver stops the instruction after 600 s and reports a
  timeout.
- The host waits for the adapter to report: the longer unit's expiry plus
  2 s, plus a second per 1000 bytes of the transfer, plus 20 s on NI's raw
  messages, whose addressing block carries 0xfd. Only then does it send the
  stop request.

## A hung adapter

Seen once on the bench: the adapter's firmware stops answering on its data
endpoints while still answering USB control requests. The driver reports it
as `AdapterNotReady` with the advice to unplug the adapter and plug it back
in, which is the only cure found; a USB bus reset does not help. The trigger
observed was an addressed command within about a millisecond of the adapter
taking control of the bus, and the driver now waits 100 ms there (spec
§8.17, §8.18).

## Linux

Untested on hardware.

If a kernel driver has claimed the adapter's interface 0 (linux-gpib's, for
example), the transport detaches it when it opens the adapter. It does not
reattach it on close; unplugging and replugging the adapter gives the device
back to the kernel.

Opening the device as a user other than root needs permission on its USB
device node. An example udev rule, also untested, in
`/etc/udev/rules.d/60-ni-gpib-usb.rules`:

```
# NI GPIB-USB adapters (vendor 3923) for the user-space driver
SUBSYSTEM=="usb", ATTR{idVendor}=="3923", ATTR{idProduct}=="709b", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="3923", ATTR{idProduct}=="7618", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="3923", ATTR{idProduct}=="702a", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="3923", ATTR{idProduct}=="702b", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="3923", ATTR{idProduct}=="725c", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="3923", ATTR{idProduct}=="725d", MODE="0660", GROUP="plugdev"
```

Then `sudo udevadm control --reload-rules`, replug the adapter, and make
sure your user is in the group named (`plugdev` on Debian and Ubuntu; other
distributions use other groups, or `TAG+="uaccess"` for the logged-in user).

## Layout

Bottom up: `tables` (device ids, request codes, the timeout table),
`protocol` (message builders and reply parsers, no I/O), `transport`
(pyusb), `controller` (the operations over one adapter, with its parts in
`link`, `transfers`, `srq` and `attach`), `device_ops` (device clear,
trigger, serial poll, presence probe), `boards` (which adapter is
`GPIB<n>`), `visa_session` and `visa_intfc` (pyvisa-py). Nothing imports
Qt.

The tests are in the repository's `tests/`: `test_gpib_usb_*.py` and
`test_property_gpib_usb_protocol.py`, over scripted and fake transports,
with the fakes in `tests/fakes/gpib_usb.py`. `test_gpib_usb_captures.py`
replays the NI captures through the codec.

## How it was written

In a clean room. Two GPL-2.0 drivers for these adapters exist. One agent
read them and wrote a protocol specification of facts only,
[`docs/design/ni_usb_gpib_protocol.md`](../../docs/design/ni_usb_gpib_protocol.md);
another checked it for expression and had it removed. The implementer never
saw the GPL sources and wrote this package from the specification, the
repository, and the MIT-licensed pyvisa-py and pyusb. Later rounds added USB
captures of NI's own driver as a source of facts (spec §10). The record of
who saw what, and how that was checked, is
[`docs/design/ni_usb_gpib_clean_room.md`](../../docs/design/ni_usb_gpib_clean_room.md).

## Licence

MIT, as the rest of ResistaMet; see [`LICENSE.md`](../../LICENSE.md).

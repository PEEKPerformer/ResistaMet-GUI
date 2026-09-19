# GPIB and VISA backends

ResistaMet talks to the instrument through [PyVISA](https://pyvisa.readthedocs.io/). PyVISA in turn sits on one of two things, and which one decides what hardware you can reach:

| Backend | `visa_library` | What it is | Reaches |
|---|---|---|---|
| Vendor VISA | `@ivi` | NI-VISA (or Keysight, R&S) installed system-wide | Whatever the vendor's drivers support. On Windows with NI-488.2 that includes NI GPIB adapters. |
| pyvisa-py | `@py` | A pure-Python VISA, installed with ResistaMet | Serial (`ASRL…`), Ethernet (`TCPIP…`), USB-TMC, Prologix-style GPIB adapters, and, through ResistaMet's built-in driver, NI GPIB-USB adapters |
| Automatic | `""` (default) | PyVISA's own choice: the vendor library if one is installed, otherwise pyvisa-py | |

The choice is the machine-local [`visa_library`](settings.md#machine-local-settings) setting. A file path to a specific VISA library is also accepted.

**When Automatic is wrong.** A vendor library can be installed and still have no GPIB driver behind it. That is the normal state of a Mac with NI-VISA: NI's last macOS GPIB driver (NI-488.2 21.5.1) has a kernel extension that does not load on macOS 13 or later, so NI-VISA opens, lists serial ports, and never shows a GPIB instrument. Automatic picks that library because it is there. Set the backend to **pyvisa-py**.

On a Windows PC with NI-VISA and NI-488.2, leave it on Automatic.

## Diagnosing with `--check-visa`

`--check-visa` prints one JSON line saying what this machine has, and exits. It needs no GUI and no development tools, so it works on a lab PC with only the installed app. It is the first thing to run when the app sees no instruments.

```bash
python -m resistamet_gui.api --check-visa                  # source install
resistamet-api --check-visa                                # frozen backend inside the desktop app
resistamet-api --check-visa bus                            # also enumerate resources
resistamet-api --check-visa bus --visa-library @py         # try a backend without saving it
```

Without `bus` nothing touches an instrument. With `bus` the resources are enumerated, which puts traffic on the GPIB bus and can disturb a run in another program; it is also the only form that opens a configured GPIB interface. `--visa-library` and `--gpib-interface` override this machine's stored settings for the one call. Exit status is 0 when a VISA implementation opened and 1 when none could.

Real output from the Mac this page was written on (NI-VISA 23.5 installed, Homebrew libusb, no adapter plugged in), reformatted:

```json
{
  "requested": "",
  "ni_usb": {"available": true, "libusb": null, "adapters": []},
  "gpib_interface": {"configured": ""},
  "ok": true,
  "backend": {"requested": "", "kind": "ivi",
              "library": "/Library/Frameworks/visa.framework/visa", "version": "23.5.0"}
}
```

Automatic resolved to the vendor library, which on this Mac cannot see GPIB. The same machine with `--check-visa bus --visa-library @py` (one Bluetooth serial port removed from the list):

```json
{
  "requested": "@py",
  "ni_usb": {"available": true, "libusb": null, "adapters": []},
  "gpib_interface": {"configured": ""},
  "ok": true,
  "backend": {"requested": "@py", "kind": "py", "library": "pyvisa-py", "version": "0.8.1"},
  "resources": ["ASRL/dev/cu.debug-console::INSTR", "ASRL/dev/cu.Bluetooth-Incoming-Port::INSTR"]
}
```

| Field | Meaning |
|---|---|
| `requested` | The `visa_library` value used |
| `ok` | A ResourceManager opened. `false` comes with an `error` string and exit status 1. |
| `backend.kind` | `ivi`, `py`, or `unknown` |
| `backend.library`, `backend.version` | The vendor library's path and version, or `pyvisa-py` and its version |
| `ni_usb.available` | pyusb imports and a libusb library loads, so the built-in NI driver can work |
| `ni_usb.libusb` | Path of the libusb bundled with a frozen app, when that is the one in use; `null` when the system's libusb is used (a source install) |
| `ni_usb.adapters` | NI adapters found on USB: `model`, `serial`, `bus`, `address`, `needs_firmware`. Found by USB enumeration only; nothing is sent to them. |
| `gpib_interface.configured` | The `gpib_interface` value used |
| `gpib_interface.opened`, `.error` | With `bus` only: whether the interface opened, and why not |
| `resources` | With `bus` only: what `list_resources()` returned (`resources_error` if it failed) |

Reading it: `ni_usb.available: false` means libusb or pyusb is missing. `adapters: []` with the adapter plugged in means the operating system does not show it to libusb. An adapter listed but no `GPIB0::…` under `resources` means nothing answered on the bus (instrument off, cable, address).

## NI GPIB-USB-HS on macOS and Linux

ResistaMet contains its own user-space driver for the NI GPIB-USB-HS family (`resistamet_gui/gpib_usb`). It talks to the adapter over libusb and registers itself with pyvisa-py, so with the **pyvisa-py** backend the instrument appears under the usual name, `GPIB0::24::INSTR`, and the board as `GPIB0::INTFC`. No NI software and no kernel driver are involved. The driver is MIT-licensed and was written from a protocol description, not from the GPL drivers.

### Requirements

- The pyvisa-py backend (`visa_library` = `@py`, or Automatic on a machine with no vendor VISA).
- pyusb: `pip install -e ".[usb]"`.
- A libusb-1.0 shared library. macOS: `brew install libusb`. Linux: your distribution's `libusb-1.0` package. The frozen macOS backend inside the desktop app bundles its own copy.
- Python 3.10 or later. The pyvisa-py release the driver is written against (0.8) requires it. On Python 3.9 the rest of ResistaMet works and the driver is skipped.

If any of these is missing the driver does not register, nothing else breaks, and `--check-visa` shows `ni_usb.available: false`.

On Windows the route is NI-VISA with NI-488.2, not this driver. The frozen Windows backend reported the driver as unavailable when it was checked on a lab PC (2026-09-18), which is the intended state there.

### What has and has not met hardware

| | Status |
|---|---|
| Attach sequence, identify, resistance runs, stop during settling, immediate restart, shutdown mid-run with the output confirmed off, compliance detection | Run on a GPIB-USB-HS with a Keithley 2400 on macOS, 2026-09-18. These use the *framed* transfer instructions. |
| Read requests of more than 1024 bytes and writes of more than 2048 bytes, which go through the adapter's *raw* transfer instructions; serial poll as its own instruction, service-request wait, the `GPIB0::INTFC` board resource | Written on 2026-09-19 from USB captures of NI's own driver. **Not yet run on an adapter.** |
| GPIB-USB-HS+, GPIB-USB-B, KUSB-488A, Measurement Computing USB-488 | In the device table; never connected. A GPIB-USB-B that has not had its firmware loaded is listed with `needs_firmware: true` and cannot be driven. |
| Linux | The same code; never run on hardware. The driver detaches a kernel driver that has claimed the adapter, and your user needs permission to open the USB device. |

!!! warning "With default settings every read takes the path that has not met hardware"
    Which read instruction the driver sends depends on how many bytes the caller *asks* for, not on how many arrive: a request of 1025 bytes or more goes out as a raw read. PyVISA asks for 20 480 bytes on every read, so with raw transfers enabled, which is the default, every reply, including a single `:READ?`, comes in through the raw path. The runs of 2026-09-18 were made before that path existed. Until it has been checked on an adapter, the configuration that matches what ran on the bench is the one with raw transfers switched off.

To switch them off, set

```bash
export RESISTAMET_GPIB_RAW_TRANSFERS=0
```

before starting ResistaMet (`0`, `false`, `no` or `off`). Every transfer then uses the framed instructions, in chunks. The variable is read each time a board is opened. Writes are not affected in practice: only a write of 2049 bytes or more uses the raw path, and ResistaMet's SCPI commands are far shorter.

### Behavior to know about

- **Every connection scans the bus.** ResistaMet checks that the address is in `list_resources()` before it opens it. With this driver a listing is a real bus scan: interface clear, remote enable, then each of the 31 primary addresses is addressed in turn to see who listens. It takes a moment and other instruments on the bus see it.
- **Board numbers.** The first NI adapter is `GPIB0`, unless linux-gpib is installed and already has boards, in which case NI adapters are numbered after them.
- **Timeouts are coarse.** The adapter has a fixed table of timeouts and rounds yours up; see [Troubleshooting](troubleshooting.md#a-timeout-takes-about-17-s-although-5-s-or-10-s-was-asked).

### A hung adapter

Symptom: connecting fails with

```
GPIB-USB-HS accepted the initialisation message but never replied: the adapter is hung. Unplug it and plug it back in.
```

or the first command after connecting never answers, even after the timeout. Seen once on the bench (2026-09-18): the adapter's firmware stops replying on its data endpoints while still answering USB control requests. Nothing the host can send recovers it, including a USB bus reset. **Unplug the adapter and plug it back in**; that cured it at once. The trigger observed was an addressed command sent within about a millisecond of the adapter taking control of the bus; the driver now waits 100 ms there, and the hang did not recur in the bench runs that followed.

## Prologix and AR488 adapters

A Prologix GPIB-USB, a Prologix GPIB-ETHERNET, or an AR488 is a serial or TCP device that speaks a text protocol. pyvisa-py supports them, but only routes `GPIB<board>::<address>::INSTR` to the adapter while the adapter's own *interface resource* is open. The machine-local [`gpib_interface`](settings.md#machine-local-settings) setting names that resource; ResistaMet opens it together with the ResourceManager and keeps it open.

Resource-name grammar (`INTFC` in capitals; `[board]` defaults to 0 and is the `<board>` of the instrument address you then use):

| Adapter | Operating system | `gpib_interface` |
|---|---|---|
| Serial (USB) | macOS | `PRLGX-ASRL[board]::/dev/cu.usbserial-XXXXXXXX::INTFC` |
| Serial (USB) | Linux | `PRLGX-ASRL[board]::/dev/ttyUSB0::INTFC` |
| Serial (USB) | Windows | `PRLGX-ASRL[board]::5::INTFC` for COM5: the port number alone, not `COM5` |
| Ethernet | any | `PRLGX-TCPIP[board]::<host>[::port]::INTFC`, port 1234 by default |

For example `PRLGX-ASRL::/dev/cu.usbserial-PX12345::INTFC` with the instrument at `GPIB0::24::INSTR`. A value that does not match `PRLGX-ASRL…::INTFC` or `PRLGX-TCPIP…::INTFC` is rejected when the setting is saved through the API.

- It needs the **pyvisa-py** backend. Under a vendor library the setting is ignored and a warning is logged, because NI-VISA has no such resource class.
- If the interface cannot be opened, the error names it (`Could not open GPIB interface PRLGX-ASRL…`) instead of a later "instrument not found". A scan or identify through the API answers 503 with that text.
- It is not opened under `--simulate`.

### Current limitation: a run does not connect through it yet

At this commit the interface opens, and `--check-visa bus`, the desktop app's scan and `GET /instruments/resources` report that it opened. **Connecting to the instrument through it fails.** Before opening an address, `VisaInstrument.connect` (`resistamet_gui/instrument.py`) requires the address to appear in `list_resources()` and otherwise raises

```
Instrument at 'GPIB0::24::INSTR' not found. Available: …
```

pyvisa-py cannot enumerate the instruments behind a Prologix adapter (its listing for them is empty), so the address is never in the list and every run, identify and connection test is refused. A strict expected-failure test, `test_a_keithley_connects_through_a_prologix_interface` in `tests/test_visa_backend.py`, holds the place and will start failing the moment the check is relaxed.

Also open: everything about this route has been tested against a scripted stand-in on a socket, never against a real Prologix or AR488 adapter; and on these sessions pyvisa-py rejects the read-termination setting ResistaMet applies at connect, so PyVISA's default terminations stay in force. Whether a Keithley accepts that has not been checked.

## Other routes pyvisa-py provides

A Keithley over RS-232 (`ASRL/dev/cu.…::INSTR`, `ASRL5::INSTR`) or a 2450 over Ethernet (`TCPIP::<host>::INSTR`) needs only the pyvisa-py backend and the address. Neither was bench-checked in this round of work.

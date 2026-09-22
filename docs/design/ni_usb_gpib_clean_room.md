# NI GPIB-USB user-space driver — clean-room record

**Status:** implemented and reviewed. The attach sequence and the framed
paths ran on a real adapter on 2026-09-18; the instructions taken from NI's
captures (0x0b, 0x0e, 0x10) and the SRQ wait have not, and are off by default
**Date:** 2026-09-18, second round 2026-09-19
**Result:** `resistamet_gui/gpib_usb/` (MIT, like the rest of ResistaMet)

## Why a clean room

National Instruments stopped shipping a GPIB driver for macOS with NI-488.2
21.5.1 (2022); its kernel extension does not load on macOS 13 or later. The
adapter itself is simple to drive from user space over libusb, and its USB
protocol is public knowledge in the sense that two GPL-2.0 implementations
exist: the linux-gpib kernel driver and the `ni-gpib-usb-hs` Python package.

ResistaMet is MIT. Copyright protects the expression in those sources, not
the facts of how the adapter behaves, and reimplementing a protocol from a
specification is the established way to keep a new implementation
independent. To make that independence demonstrable rather than asserted,
the work was split between agents that never shared context:

| Role | Saw the GPL sources | Wrote |
|---|---|---|
| Reader | yes | `ni_usb_gpib_protocol.md` — protocol facts only, no code, no identifiers, no comments |
| Wall reviewer | yes, plus the specification | a list of anything in the specification that was expression rather than fact; the Reader removed it |
| Implementer | **no** — forbidden to fetch or read any GPIB driver source, given only the specification, this repository, and the MIT-licensed pyvisa-py and pyusb it integrates with | `resistamet_gui/gpib_usb/` and its tests |
| Code reviewer | yes, plus the implementation | correctness review against the specification; similarity check against the sources; no edits |
| Orchestrator (this session's main agent) | a one-paragraph summary of one source, no source text | the backend selector, packaging, and this record; no protocol code |

Each agent ran in a fresh context. The transcripts are kept with the
session (id `e57e16fd-5335-4f49-869d-e56fc2e35919`) and show every tool call
each agent made, so "the implementer never read the sources" is checkable,
not a claim.

## Sources the Reader used

Recorded in the provenance section of `ni_usb_gpib_protocol.md` with the
sha256 of each file as downloaded on 2026-09-18. The downloads were kept
outside the repository and deleted before the Implementer started.

## What the specification may contain

Numbers and behaviour: USB identifiers, endpoints, request codes, message
layouts, register addresses and values, timeout tables, status and error
codes, operation sequences, limits. Registers are named by the TNT4882 /
NEC7210 datasheet mnemonics, which are the chip vendor's names.

Not: identifiers, code, pseudo-code that mirrors control flow, comment text,
or narration of the sources' structure.

## How it went

1. **Reader** downloaded the sources to a scratch directory, confirmed the
   register map against NI's public TNT4882 Programmer Reference Manual,
   and wrote the specification (904 lines).
2. **Wall reviewer** scanned it against 945 identifier-like tokens and every
   comment phrase from the sources. Verdict: clean after fixes. Four items
   were expression: a section narrating the two programs' attach flows,
   a paraphrase of a docstring's scope paragraph, a reproduced error-name
   table, and an implementation's internal buffer size. Eleven minor
   wording and ordering overlaps and twelve completeness gaps were listed.
3. **Reader** applied all of it (946 lines). **Wall reviewer** re-scanned:
   clean. The scratch directory was deleted. The specification as handed
   to the Implementer has SHA-256
   `f13471a4baab43ee771ca3b5300aa988654c97a7fba04e1d2db8a0ed8c10244e`.
4. **Implementer** built the package and its tests. Its transcript was
   checked mechanically after each round: no WebFetch or WebSearch, no
   curl/wget/clone, nothing read under /tmp, no path outside the repository
   and the venv's pyvisa, pyvisa-py and pyusb packages, and only the
   intended files written.
5. **Code reviewer** checked every operation against the specification at
   the byte level (no wrong bytes found), then re-downloaded the sources,
   verified their hashes against the provenance table, and compared:
   **independent** — the only shared shapes are ones the specification's
   tables and reply layouts dictate, plus public API names. It found one
   blocker (a pyusb handle leaked per enumeration, which on macOS locks
   other processes out of the adapter) and four majors (host wait computed
   from the requested rather than the effective device timeout; no
   resynchronisation after a malformed reply; session timeout not applied
   to housekeeping operations; a latent interface leak on a failed open),
   and named tests that would have passed with a broken implementation.
6. **Implementer** fixed all of it, split the package into the modules
   listed below, and added the missing tests, including a fake `usb` module
   that catches the handle leak. The split met the project's 400-line
   guideline then; it no longer does (see "What exists").
7. **Code reviewer** verified the fixes and found one more: a failed
   re-attach after a fault left the controller refusing every later
   operation until the resource was reopened. Fixed, with tests that assert
   the retry reaches the wire.

The Orchestrator wrote the settings, packaging, diagnostic and documentation
around the package. Inside it, three things: one test expectation in
`test_gpib_usb_visa.py` (how many USB handles a re-enumeration supersedes),
the version guard that skips those tests on a pyvisa-py too old for the
Session API, and `transport.libusb_library_path`, a four-line accessor that
reports which libusb loaded. None of the three carries a protocol fact.
Everything that speaks to the adapter is the Implementer's.

## What exists

`tables.py` (device ids, control requests, status bits, error codes, the
register initialisation, IEEE-488 command bytes, the timeout table),
`protocol.py` (message builders, reply parsers, exceptions),
`transport.py` (pyusb), `controller.py` (the operations over one adapter),
which owns an `AdapterLink` from `link.py` (exchanges, faults, resync)
and takes its transfer paths from `transfers.py`, its service-request
wait from `srq.py` and its model-specific attach steps from `attach.py`;
`device_ops.py` (device clear, trigger, serial poll, presence probe),
`boards.py` (board registry), `visa_session.py` (pyvisa-py instrument
session and dispatcher), `visa_intfc.py` (the board as `GPIB<n>::INTFC`).
About 750 driver tests over scripted and fake transports (the fakes in
`tests/fakes/gpib_usb.py`); every worked hex example in the specification
is asserted byte for byte in both directions, `test_gpib_usb_captures.py`
replays the NI captures (below) through the codec, and
`test_property_gpib_usb_protocol.py` checks the codec's invariants with
seeded random input.

Size: the split planned after the bench was done on 2026-09-22 as pure
moves (one commit per module, the tests unchanged), then one commit that
turned the moved mixin into an object the controller owns. `controller.py`
is 480 lines and `protocol.py` 854; the other modules are under or near
the project's 400-line guideline.

## The second round: NI's driver as the oracle (2026-09-19)

The first bench day (`tauri_ui_status.md`) found
two specification errors on the wire. To find the rest without touching
GPL text again, the lab desktop — NI-488.2 driving the same adapter model —
was captured with USBPcap, one VISA operation per scenario, 28 scenarios,
stored with checksums in `captures/ni_usb_gpib_2026-09-19/`. Before the
capture filter could be attached, Windows USB ETW gave URB headers without
payloads; that method (`trace.bat`, `etw_urbs.py`) is kept beside the
captures. Observing the bytes between one's own PC and one's own adapter is
the classic interoperability path; the captures are facts, not anyone's
expression.

Roles, same wall:

1. **Reader** (a fresh agent, no access to the GPL sources) decoded the
   captures into §10 of the specification and 35 "Observed 2026-09-19"
   notes in §§1–9, every fact with its packet as provenance. Its transcript
   was checked: 54 shell commands, all inside the repository, nothing
   fetched, the implementer's code never opened.
2. **Implementer** (fresh agent, specification and captures only) added the
   0x0b raw read on the alternate bulk IN endpoint, the 0x0e raw write on
   the alternate bulk OUT, the 0x10 serial poll, the termination character
   in reads, and a service-request wait on the interrupt endpoint, plus the
   `INTFC` resource earlier the same day. Transcripts checked as before.
3. **Code reviewer** reviewed each batch against the specification and the
   captures.

What the captures corrected: reads over 1024 bytes do not use the framed
path at all (0x0b, data raw, one instruction per chunk); writes over 2048
bytes likewise (0x0e); serial poll is its own instruction; NI never sends
go-to-standby in an instrument session, and never the stop request; the
"error 4 on `e` without `m`" rule was wrong; the timeout table was right;
the initialisation
matched ours except two register values. A second batch of five captures
settled the two thresholds exactly and the error paths: a long write nobody
listens to is refused with a STALL on the alternate OUT pipe, answered with
two pipe resets; a raw read that times out ends itself with a zero-length
packet; a serial poll that times out replies without its status-byte block.
The complete list is §10 of the specification.

This record said at first that the timeout bounds a handshake rather than
a transfer. That was an inference from reads that ended inside a code's
expiry, and the fourth batch (2026-09-22, `captures/ni_usb_gpib_2026-09-22/`)
showed the opposite: a long read under a short code is cut off at the
code's expiry with bytes still arriving, so the code bounds the whole
instruction (spec §7.1, §10.10.2). The same batch captured NI's mapping
from VISA timeouts to codes down to 1 ms and the 32-bit count of the raw
read. It was decoded by a fresh Reader under the same rules; its
transcript shows only the repository, its own `mktemp` directory, and no
network access.

## What it has not had

The new paths on a real adapter. The framed paths and the attach sequence
ran on a GPIB-USB-HS with a Keithley 2400 on 2026-09-18 (identify, runs,
stop, restart, shutdown, compliance). The 0x0b/0x0e/0x10 paths and the SRQ
wait were written on 2026-09-19 against the captures alone. The first
thing to run when the adapter is back on the Mac: reads of 1024 and 1025
bytes (the two instructions either side of the threshold), a chunked
`:TRAC:DATA?`, writes of 2048 and 2049 bytes, a long write to an empty
address followed by a normal query with no replug, a read and a serial poll
that time out, the REN modes from the front panel's point of view, and SRQ
on `*OPC`.

Because of that, the new instructions are opt-in. By default every
transfer, of any size, is a framed 0x0a / 0x0d and the serial poll is the
IEEE-488.1 command sequence of §5.9: the instructions that ran on the
bench. pyvisa reads in 20480-byte chunks, so with 0x0b on by default the
application's first `*IDN?` would have been the first 0x0b ever sent to
our adapter, in a message sequence no capture shows (0x0c, 0x06 and 0x0b
as separate messages, under AUXRA 0x81, without NI's bank-2 session
configuration). `RESISTAMET_GPIB_NI_INSTRUCTIONS=1` switches on 0x0b and
0x0e for large transfers and 0x10 for the serial poll
(`RESISTAMET_GPIB_RAW_TRANSFERS`, the switch's first name, is still read);
the attach log line says which set a board uses. Plain reads send the
bench-proven `m e` = `00 00` on both read instructions; the termination
character goes out only when the session enables it.

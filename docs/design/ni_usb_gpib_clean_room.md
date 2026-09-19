# NI GPIB-USB user-space driver — clean-room record

**Status:** implemented and reviewed; not yet run against a real adapter
**Date:** 2026-09-18
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
6. **Implementer** fixed all of it, split the modules to the project's size
   rule, and added the missing tests, including a fake `usb` module that
   catches the handle leak.
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
`transport.py` (pyusb), `controller.py` (sequencing over one adapter),
`device_ops.py` (device clear, trigger, serial poll, presence probe),
`boards.py` (board registry), `visa_session.py` (pyvisa-py instrument
session and dispatcher), `visa_intfc.py` (the board as `GPIB<n>::INTFC`).
547 driver tests over scripted and fake transports; every worked hex
example in the specification is asserted byte for byte in both directions,
and `test_gpib_usb_captures.py` replays the NI captures (below) through the
codec.

## The second round: NI's driver as the oracle (2026-09-19)

The first bench day (`tauri_ui_status.md`) found
two specification errors on the wire. To find the rest without touching
GPL text again, the lab desktop — NI-488.2 driving the same adapter model —
was captured with USBPcap, one VISA operation per scenario, 27 scenarios,
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
"error 4 on `e` without `m`" rule was wrong; the timeout table was right
and the timeout bounds a handshake, not a transfer; the initialisation
matched ours except two register values. A second batch of five captures
settled the two thresholds exactly and the error paths: a long write nobody
listens to is refused with a STALL on the alternate OUT pipe, answered with
two pipe resets; a raw read that times out ends itself with a zero-length
packet; a serial poll that times out replies without its status-byte block.
The complete list is §10 of the specification.

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
on `*OPC`. Plain reads send the bench-proven bytes; the termination
character goes out only when the session enables it.
`RESISTAMET_GPIB_RAW_TRANSFERS=0` turns the new transfer paths off for a
one-flag comparison.

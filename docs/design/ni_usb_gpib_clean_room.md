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
`boards.py` (board registry), `visa_session.py` (pyvisa-py session and
dispatcher). 236 tests over scripted and fake transports; every worked hex
example in the specification is asserted byte for byte in both directions.

## What it has not had

A real adapter. The lab's GPIB-USB-HS (USB id 3923:709b, the model the
specification is best evidenced for) is on laptop2 in the lab. The first
run on a Mac decides whether the six `# spec gap:` points in the code are
right; they are listed in the specification's "uncertain" notes and in the
code where each conservative choice was made.

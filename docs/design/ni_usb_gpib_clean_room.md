# NI GPIB-USB user-space driver — clean-room record

**Status:** in progress
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

## Outcome

_(filled in when the work lands)_

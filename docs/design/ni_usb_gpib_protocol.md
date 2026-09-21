# NI GPIB-USB adapter wire protocol (GPIB-USB-HS family)

Clean-room protocol specification. Facts only. Written for an implementer
who will build a user-space libusb (pyusb) driver on macOS for the
National Instruments GPIB-USB-HS (USB ID 3923:709b), with best-effort
notes for the GPIB-USB-HS+ and the older GPIB-USB-B, and a pyvisa-py
session on top. Nothing in this document is code or text from the sources
listed in section 9; it records observed wire behaviour and public
standards only.

Conventions used throughout:

- All byte values are hex unless suffixed otherwise. Multi-byte fields
  state their byte order explicitly.
- "Bank" means the register-bank selector byte inside register-access
  instructions (section 3.4). Bank 1 is the TNT4882 GPIB controller chip.
- Register names are the TNT4882 / NEC 7210 datasheet mnemonics (AUXMR,
  ADR, ADMR, IMR0..IMR3, CMDR, HSSEL, KEYREG, BSR, SPMR, EOSR). Registers
  with no datasheet name are given by address.
- "ibsta" is the NI-488.2 16-bit status word (section 4.2).
- Timeouts written as "code" refer to the one-byte device timeout code
  of section 7.
- "Observed" with no date and no unit named means reported as seen on
  real hardware by at least one of the original sources of section 9
  (evidence level [inherited] below). "Observed 2026-09-19" means seen in
  this project's USB captures of NI-488.2 ([captured]). "Observed on
  GPIB-USB-HS 01CEE482" or "2026-09-18" means seen on this project's
  bench ([bench]). "Uncertain" and "not established" mark what no
  evidence settles; section 11 lists all of it.
- Section references are written "5.2" and "§5.2" alike.

### How to read this document

The text has three layers. Sections 1-9 were written first, from the
original sources of section 9. Section 10 and the "Observed 2026-09-19"
notes came next, from USB captures of NI-488.2 driving the same adapter
model. Two further rounds of captures the same day added NI's exact
thresholds, its error paths and the measured timeout expiries (7.3). The
layers did not always agree, so every table row, rule and procedure step
an implementer would act on carries one or more evidence tags:

- **[captured]** -- seen on the wire in the NI-488.2 captures of
  2026-09-19 (GPIB-USB-HS 013CC9DF, Keithley 2420 at PAD 24). The packet
  reference `(name.pcap t DIR n B)` stands beside the tag or in the
  section 10 subsection cited there. It shows what the vendor's driver
  does, under the vendor's initialisation (§10.3.1); it does not show
  that nothing else works.
- **[bench]** -- exercised on hardware by this project's driver on
  2026-09-18 (GPIB-USB-HS 01CEE482, Keithley 2400 at PAD 3, macOS,
  pyusb). Used only for what the bench notes in this document and the
  clean-room record (`ni_usb_gpib_clean_room.md`) say ran: the attach
  sequence and the shutdown, addressing, the framed write (0x0d), the
  framed read (0x0a) with its 16-byte tail, command bytes (0x0c), the
  presence probe via NDAC (5.16) and the settle after IFC (8.18).
- **[inherited]** -- taken from the original sources and not seen on a
  wire by this project. It may well be right; nobody here has checked.

Two tags on one statement mean both saw it. How the levels rank:

- A [captured] or [bench] observation governs over an [inherited]
  statement it contradicts. The contradicted text is not deleted: it
  moves to a **Superseded** note at the end of its subsection, with a
  line on what replaced it and where. Nothing inside a Superseded note
  is normative.
- Where two levels describe *different behaviours that both work* --
  this project's framed path sends 0x06 before a read, NI's batched
  messages never do -- both are given, each under its own tag, and the
  reader chooses knowing which has been seen where.
- Nothing specific to NI's driver has run on this project's bench yet:
  the raw-data instructions 0x0b and 0x0e, the 0x10 serial poll, the 0x02
  probe, the 0x03 block, messages batching more than a read with its
  register-write block, and the interrupt push are [captured] only.
  Conversely 0x06 in an instrument session, the two-write block embedded
  in a 0x0a, `00 00` in the EOS bytes of a read and AUXRA 0x81 at
  initialisation are [bench] only. No mixture of the two sets (0x06
  followed by 0x0b, say) has been seen by anyone.
- Section 10 is [captured] throughout and is not tagged line by line.
  Section 11 lists every open question with back-references.

---

## 1. Devices

### 1.1 USB identification

| Model                          | VID    | PID    | Notes |
|--------------------------------|--------|--------|-------|
| GPIB-USB-B (firmware loaded)   | 0x3923 | 0x702a | Enumerates as 0x702b until firmware is uploaded (see below). |
| GPIB-USB-B (pre-firmware)      | 0x3923 | 0x702b | Not usable. A firmware upload (out of scope here) is required before the 0x702a device appears. |
| GPIB-USB-HS                    | 0x3923 | 0x709b | The lab's unit. No firmware upload needed. |
| GPIB-USB-HS+                   | 0x3923 | 0x7618 | Two USB interfaces; claim interface 0 only. |
| Keithley KUSB-488A             | 0x3923 | 0x725c | Same endpoints and protocol as the HS. |
| Measurement Computing USB-488  | 0x3923 | 0x725d | Same endpoints and protocol as the HS. |

The HS+ exposes a second USB interface for its built-in bus analyzer. It is
not part of this protocol and must be left alone; match on interface 0.

Evidence: the GPIB-USB-HS row **[bench] [captured]**; every other row and
the HS+ interface note **[inherited]**.

### 1.2 Endpoints

Each model has a primary bulk pair, an interrupt IN endpoint and an
alternate bulk pair. The primary pair carries the framed messages of
section 3 and their replies. Addresses are given as raw endpoint numbers
and as libusb addresses (IN = 0x80 | n).

| Model  | Bulk OUT | Bulk IN     | Interrupt IN | Alternate bulk endpoints | Evidence |
|--------|----------|-------------|--------------|--------------------------|----------|
| USB-B  | 0x02     | 0x02 (0x82) | 0x04 (0x84)  | bulk IN 0x06 (0x86); use not established | [inherited] |
| HS, KUSB-488A, MC USB-488 | 0x02 | 0x04 (0x84) | 0x01 (0x81) | bulk OUT 0x06, bulk IN 0x08 (0x88): raw data of 0x0e / 0x0b on the HS | HS primary pair [bench] [captured]; HS interrupt and alternate pair [captured]; KUSB-488A, MC USB-488 [inherited] |
| HS+    | 0x01     | 0x02 (0x82) | 0x03 (0x83)  | bulk OUT 0x04, bulk IN 0x05 (0x85); use not established | [inherited] |

- **[bench]** The framed path needs only the primary bulk OUT and bulk IN
  endpoints (on the HS: 0x02 and 0x84). The interrupt endpoint is
  optional (2.5).
- **[captured]** (§10.1.3, §10.5.2) On the HS the alternate pair carries
  raw, unframed data: bulk IN 0x88 the bytes read by a 0x0b instruction,
  bulk OUT 0x06 the bytes written by a 0x0e instruction, while the
  instruction and its reply stay on 0x02 / 0x84.
- **[captured]** (§10.1.1, §10.5.2) NI's driver moves to the alternate
  pair by size: reads of 1025 bytes and more, writes of 2049 bytes and
  more. A write that cannot start is refused with a STALL on 0x06; a read
  that gets nothing ends with a zero-length transfer on 0x88 (§10.6.5,
  §10.6.6).
- Not established: what the alternate endpoints of the USB-B and the HS+
  carry (neither model was captured).

Reply sizes on the framed path are bounded: the largest single reply is
the reply to a maximum-length (65535-byte) 0x0a read, about 70 KB (see
5.2) **[inherited]** -- the largest 0x0a count seen on a wire is 1024
**[captured]** (256 **[bench]**). The reply to any other single
instruction is well under 64 bytes for the register counts used here; a
batched message is answered with the sum of its blocks' replies
(§10.2.1). Ordinary synchronous bulk reads with a correctly sized buffer
suffice. A 0x0b read delivers up to its requested count on 0x88 in one
transfer (20480 bytes was the largest captured, §10.1.4).

**Superseded (1.2).** [inherited] "All bulk endpoints carry the framed
messages of section 3"; the table heading "Alternate endpoints (present,
unused)"; "Uncertain: the purpose of the alternate endpoints". Replaced
for the HS by the raw-data use above (§10.1.3, §10.5.2); the uncertainty
stands for the USB-B and the HS+. The first-batch wording "NI's driver
uses them for every read above 1024 bytes and for a 2050-byte write" is
replaced by the exact thresholds above.

---

## 2. Attach / initialisation

### 2.1 USB configuration

1. Find the device by VID/PID; on macOS also compare the USB serial-number
   string descriptor if several adapters are present.
2. Set the device's default configuration if it is not already configured
   (on macOS it normally is; a failure of this step is harmless).
3. Claim interface 0. Detach any kernel driver first if the OS has one
   (not the case on macOS).

A USB reset-configuration before claiming is not required.

Evidence: **[bench]** on macOS with one adapter; the serial-number
comparison and the kernel-driver detach **[inherited]**.

### 2.2 Control transfers (all models unless stated)

All but the last row are device-to-host, vendor class. bmRequestType 0xC0
= IN | Vendor | Device; 0xC1 = IN | Vendor | Interface; 0x40 = OUT |
Vendor | Device. Each device-to-host response begins with a byte echoing
bRequest.

| bRequest | bmRequestType | wValue | wIndex | wLength | Purpose | Response | Evidence |
|----------|---------------|--------|--------|---------|---------|----------|----------|
| 0x41 | 0xC0 | 0x0000 | 0x0000 | 16 | Serial-number query | byte 0 = 0x41; bytes 1..4 = 32-bit serial, little-endian. Response length 5 (HS) or 16 with zero padding (HS+). | HS [bench] (2.3); HS+ [inherited] |
| 0x40 | 0xC0 | 0x0000 | 0x0000 | 16 | Readiness query | 11 significant bytes, see 2.3. | HS [bench] (2.3) |
| 0x20 | 0xC0 | 0x0000 | 0x0000 | 8  | Stop / abort the in-flight bulk operation | 8-byte status block (section 4). | [inherited]; in none of the captures (5.11) |
| 0x21 | 0xC0 | 0x0200 | 0x0000 | 8  | Status query | 8-byte status block with current ibsta: `21 ss ss ee cc cc xx xx`. | [captured] (§10.3.5) |
| 0x21 | 0xC0 | 0x0300 | mask   | 8  | Set interrupt-monitor mask (2.5) | 8-byte status block. | [inherited]; in none of the captures (2.5) |
| 0x48 | 0xC0 | 0x0000 | 0x0000 | 16 | HS+ only, extra init | expected `48 f3 30 00 00 00 00 00 00 00 00 00 00 00 00 00` | [inherited] |
| 0x4b | 0xC0 | 0x0001 | 0x0000 | 2  | HS+ only, LED to steady | expected `4b 00` | [inherited] |
| 0xf8 | 0xC1 | 0x0000 | 0x0001 | 9  | HS+ only, extra init | expected `f8 01 00 00 00 01 00 00 00` | [inherited] |
| 0x3b | 0x40 | 0x0000 | 0x0000 | 0  | Host-to-device. NI sends it once after every interrupt-IN push, before it re-arms the interrupt read; function not established | no data stage | [captured] (§10.4.2) |

A 1000 ms host timeout suffices for all of these (100 ms per readiness
query attempt) **[inherited]**.

**[captured]** (§10.3.5, §10.4.2) NI uses the 0x21 / 0x0200 status query
to read line states without bulk traffic. The 0x40, 0x41, 0x20 and 0x21 /
0x0300 requests appear in none of the 28 captures. Every capture starts
after NI's driver already owned the adapter, so their absence says
nothing about what the driver does when the adapter is first plugged in
(0x40, 0x41, the mask); it does say that NI ends no failed or timed-out
instruction with 0x20 (5.11). On the bench adapter in its hung state
(8.17) the serial-number, readiness, status and stop requests were all
answered normally.

### 2.3 Readiness poll (HS, HS+, KUSB-488A, MC USB-488; not USB-B)

1. Send the serial-number query (0x41). Verify byte 0 == 0x41. The value
   is informational.
2. Send the readiness query (0x40) repeatedly, up to 50 attempts, 100 ms
   apart, 100 ms USB timeout per attempt, until the device reports ready.

Evidence: steps 1 and 2 **[bench]** on the HS; the table below
**[inherited]**; the reply of the bench unit, quoted after the table,
**[bench]**.

Response layout of the readiness query. The "accepted" column lists the
values the original sources report from real hardware; it is not
exhaustive (the bench unit returned others in bytes 6, 7 and 10, below),
and other values are unexpected but not fatal. Model annotations are given
where a value is known to be model-specific; attributions to the 0x725c
model are uncertain (the observations name a "488A" unit without its PID).

| Byte | Accepted values | Meaning |
|------|-----------------|---------|
| 0    | 0x40 | echo of request (must match) |
| 1    | 0x01; 0x00 (HS+) | - |
| 2    | 0x00 | - |
| 3    | 0x01; 0x08 (MC USB-488, occasionally HS); 0x07 (0x725c model, uncertain); 0x00 (HS+) | - |
| 4    | 0x30; 0x00 (HS+) | - |
| 5    | 0x01; 0x00 (MC USB-488, HS+, occasionally HS) | - |
| 6    | 0x00 = not ready; 0x02, 0x0e, 0x0f (HS+) | **nonzero = ready** |
| 7    | 0x00 = not ready; 0x03, 0x05 (MC USB-488), 0x06 (0x725c model, uncertain) | **nonzero = ready** |
| 8    | 0x00; 0x02 (MC USB-488) | - |
| 9    | 0x00; 0x03 (HS, 0x725c model uncertain); 0x30 (HS+) | - |
| 10   | 0x00 = not ready; 0x96, 0x07 (MC USB-488) | **nonzero = ready** |

Ready condition: any of bytes 6, 7, 10 is nonzero **[bench]**. Before the
device is ready all three are 0x00 **[inherited]**. A single readiness
query without the loop has been observed to succeed on an already-settled
HS; the loop is the normative form (2.8).

Observed on GPIB-USB-HS 01CEE482 (bcdDevice 0x101), 2026-09-18: the reply
is `40 01 00 01 30 01 19 08 00 00 65` (11 bytes), i.e. byte 6 = 0x19,
byte 7 = 0x08, byte 9 = 0x00, byte 10 = 0x65, immediately after a USB
reset and unchanged afterwards. These bytes therefore vary between units
or firmware versions (0x65 = 101 matches the 1.01 of bcdDevice); only
"nonzero" carries meaning. The serial-number reply was `41 82 e4 ce 01`
(5 bytes), decoding to 0x01CEE482 as described.

**Superseded (2.3).** [inherited] 'The "accepted" column lists every value
observed on real hardware.' The bench reply above has 0x19, 0x08 and 0x65
in bytes 6, 7 and 10, none of them in the column.

### 2.4 Model-specific extra initialisation

- **HS+**: after the readiness poll, issue the three control requests
  0x48, 0x4b, 0xf8 from the table in 2.2, in that order **[inherited]**.
  According to the original sources the sequence was captured from NI's
  Windows driver (not one of this project's captures, which have no HS+);
  GPIB traffic works without it, and its only visible effect is that 0x4b
  changes the LED from blinking to steady. The 0xf8 request is the only
  interface-recipient (0xC1) request in the protocol and carries wIndex =
  1.
- **USB-B** **[inherited]**: no readiness poll exists. Instead the serial
  number is read with a register-read bulk instruction (3.4) of bank 3
  addresses 0x0b, 0x0a, 0x09, 0x08, which yield serial bytes 0 (LSB) to 3
  (MSB). See 3.5 for the four-register reply layout.

### 2.5 Interrupt-monitor mask (optional)

**[inherited]** Control request 0x21 with wValue 0x0300 and wIndex = mask
selects which ibsta bits, when they become set, cause the device to push
a status block on the interrupt IN endpoint. Each push is one 8-byte
status block; read the interrupt endpoint with a buffer of at least its
wMaxPacketSize. The full mask is 0x10ff = bit 12 (SRQI) plus bits 0-7
(DCAS, DTAS, LACS, TACS, ATN, CIC, REM, LOK). Operation without this
request and without the interrupt endpoint has been observed to be
reliable; the mask is only useful for event-driven SRQ waits. Placement
in the attach sequence: 2.8.

**[captured]** (§10.4.2) With NI's driver the adapter pushed one 8-byte
packet on the interrupt endpoint when the instrument asserted SRQ, `30 18
00 60 31 a1 01 00`: byte 0 = 0x30; bytes 1-2 = ibsta 0x1800 (SRQI, RQS);
byte 3 = the instrument's status byte (0x60) -- the adapter had
serial-polled the device itself and SRQ was released; bytes 4-7 not
established. No 0x21 / 0x0300 request appears in any of the 28 captures,
and the push also arrived in a process that had enabled no event
(srq_poll.pcap 1.0308). NI answers each push with control request 0x3b
(2.2) and re-arms a 64-byte interrupt read.

What the two together do and do not say:

- No mask request is needed per session or per event: NI sends none and
  gets its push **[captured]**.
- Every capture starts after NI's driver already owned the adapter -- the
  interrupt read was already pending when each capture began (§10.4.1)
  -- so a mask set once at driver load would not be in them. Whether an
  adapter that has never been sent a mask pushes at all is therefore
  **not established**. A driver that wants pushes and attaches to a
  fresh adapter has only the [inherited] steps 4 and 6 of 2.8 to go by.
- The one push seen is not a plain status block: byte 0 is 0x30, not an
  instruction id, and byte 3 is the device's status byte, not an error
  code (4.1). Whether pushes for the other mask bits look the same is not
  established; none was captured.

### 2.6 Register initialisation sequence

Sent as ONE register-write bulk instruction (3.4) containing all 26 writes
below, in this order. `P` = the adapter's own primary GPIB address
(0..30, normally 0). Column "bank" is the register-bank selector byte.

Evidence: the sequence as tabulated, with 0x81 in rows 3 and 4,
**[bench]**; the same 26 writes in the same order with 0x99 in rows 3 and
4 **[captured]** (§10.3.1). The register names and meanings in the last
column are **[inherited]** from the chip documentation of section 9.

| # | bank | addr | value | Register / meaning |
|---|------|------|-------|--------------------|
| 1  | 3 | 0x10 | 0x00 | device-level register, purpose unknown; also written at shutdown |
| 2  | 1 | 0x1c | 0x22 | CMDR: soft reset (clears CFG, HSSEL, IMR3; empties FIFOs) |
| 3  | 1 | 0x0a | 0x81 (0x91 if an 8-bit EOS compare is already configured, see 5.2) | AUXMR -> AUXRA: holdoff on all data (bit 0); bit 4 = BIN |
| 4  | 1 | 0x06 | same as #3 | see note (a) |
| 5  | 1 | 0x0d | 0x01 | HSSEL: one-chip mode (bit 0) |
| 6  | 1 | 0x0a | 0x02 | AUXMR: chip reset |
| 7  | 1 | 0x1d | 0x80 | IMR0: bit 7 must be written 1; all interrupt enables off |
| 8  | 1 | 0x02 | 0x00 | IMR1: all off |
| 9  | 1 | 0x04 | 0x00 | IMR2: all off |
| 10 | 1 | 0x12 | 0x00 | IMR3: all off |
| 11 | 1 | 0x0a | 0x51 | AUXMR: holdoff handshake immediately |
| 12 | 1 | 0x0a | T1 row 1 | AUXMR -> AUXRI, see 2.7 |
| 13 | 1 | 0x0a | T1 row 2 | AUXMR -> AUXRB, see 2.7 |
| 14 | 1 | 0x17 | T1 row 3 | KEYREG, see 2.7 |
| 15 | 1 | 0x0a | 0x48 | AUXMR -> AUXRG: NTNL (no talking when no listener, bit 3) |
| 16 | 1 | 0x1c | 0x03 (system controller) or 0x02 (not) | CMDR: set / clear system controller |
| 17 | 1 | 0x0a | 0x16 | AUXMR: clear IFC |
| 18 | 1 | 0x0c | P    | ADR: own primary address (ARS=0) |
| 19 | 2 | 0x00 | P    | bank-2 mirror of the primary address, purpose unknown |
| 20 | 1 | 0x0c | 0xe0 (no secondary) or 0x80 \| S | ADR with ARS=1: second address register; 0xe0 = DT+DL (disabled) |
| 21 | 1 | 0x08 | 0x31 (no secondary) or 0x32 | ADMR: bits 5,4 always 1; bit 0 = mode 1 (normal), bit 1 = mode 2 (extended addressing) |
| 22 | 2 | 0x01 | 0x00 (no secondary) or 0x60 \| S | bank-2 mirror of the secondary address command byte |
| 23 | 2 | 0x02 | 0xfd | purpose unknown; value resembles a timeout code |
| 24 | 1 | 0x0f | 0x11 | not in the TNT4882 one-chip register map; purpose unknown |
| 25 | 1 | 0x0a | 0x00 | AUXMR: immediate execute power-on (releases pon) |
| 26 | 1 | 0x0a | 0x01 | AUXMR: clear parallel-poll flag |

**[captured]** (§10.3.1) NI-488.2 22.5 sends exactly these 26 writes in
this order, byte-identical in all 28 captures, with one value difference:
rows 3 and 4 carry 0x99 (AUXRA: holdoff-all, XEOS, BIN) instead of 0x81 /
0x91. Rows 12-14 were the 500 ns T1 row (0xe9, 0xa4, 0x00), row 16 =
0x03, rows 18-22 for address 0 without secondary. The reply was `09 00 00
00 cc cc ff ff 1a 00 00 00 04 00 00 00` as expected. Bank 2 has further
registers 0x03..0x07 that NI writes per session (§10.2.4).

Rows 3 and 4 are therefore a choice between two values that have both
initialised a working adapter: 0x81 **[bench]**, under which this
project's framed reads with `m` = `e` = 0x00 ran; 0x99 **[captured]**,
under which everything in section 10 was recorded. What the difference
(XEOS, BIN) changes on the wire is not established; the one rule that
may depend on it is error 4 on the EOS bytes of a read (4.3, 5.2).

Note (a): uncertain purpose. In the one-chip / 7210 register map, write
offset 0x06 is SPMR (serial poll mode). In NI's Turbo+9914 map the
auxiliary command register (AUXCR) sits at 0x0a when the KEYREG SWAP bit
is set and at 0x06 when SWAP is clear, so this may be intended as a
9914-mode AUXCR write for a chip that is not in that mode. It is present
in NI's own driver traffic **[captured]** (`01 06 99`, §10.3.1);
replicate it verbatim.

EOSR (bank 1, addr 0x0e) is never written by this protocol; the EOS
character travels only inside each read instruction (5.2) **[captured]**
(§10.1.6: no register write when the termination character changes).

Expected reply: the 16-byte register-write reply (3.5) with error code 0
and "writes completed" == 26 **[bench] [captured]**.

**Superseded (2.6).** "byte-identical in 22 captures": the count of the
first batch; all 28 agree (§10.3.1).

### 2.7 T1 delay rows (#12, #13, #14 above)

| Requested T1 | #12 AUXMR (AUXRI base 0xe0) | #13 AUXMR (AUXRB base 0xa0) | #14 KEYREG | Resulting T1 |
|--------------|------|------|------|--------|
| <= 350 ns    | 0xe9 (USTD+SISB) | 0xa4 (TRI) | 0x20 (MSTD) | 350 ns |
| <= 500 ns    | 0xe9 | 0xa4 | 0x00 | 500 ns |
| <= 1100 ns   | 0xe9 | 0xa0 | 0x00 | 1100 ns |
| > 1100 ns    | 0xe1 (SISB only) | 0xa0 | 0x00 | 2000 ns |

Both the 500 ns row and the 2000 ns row have been observed to work
**[inherited]**; the 500 ns row is the one NI sends **[captured]**
(§10.3.1). 2000 ns is the IEEE-488.1 default and the safe choice for
long cables. These three writes can be re-sent later on their own to
change T1 **[inherited]**.

### 2.8 Attach sequence (normative)

1. **[bench]** Claim interface 0 (2.1); set-configuration may be skipped
   if the device is already configured.
2. **[bench]** (HS) Readiness poll per 2.3 (all models except the USB-B).
3. **[inherited]** Model extras per 2.4 (HS+ control requests; USB-B
   serial read).
4. **[inherited]** Optional: set the interrupt-monitor mask to 0x0000
   (2.5).
5. **[bench] [captured]** The 26-write register initialisation of 2.6;
   verify the reply (error 0, 26 writes completed).
6. **[inherited]** Optional: set the interrupt-monitor mask to 0x10ff
   (2.5). Steps 4 and 6 belong to the interrupt endpoint; whether pushes
   depend on them at all is not established (2.5).
7. If acting as system controller: IFC pulse (5.5), REN on (5.6), take
   control (5.4) **[bench]**, then the pause of 8.18 before the first
   addressed command **[bench]**. NI sends the IFC pulse and REN on but
   no take-control: the IFC reply already shows CIC and ATN
   **[captured]** (§10.3.2). Both work; the take-control is redundant,
   not harmful. Error code 5 from the take-control reply on an empty bus
   is harmless **[inherited]**.

The adapter is then CIC with ATN true **[captured]** and ready for the
addressing sequences of section 6.

**[captured]** (§10.3.2) NI's sequence for an instrument session is: step
5 (the 26 writes); a read of bank-1 0x0d, 0x0c, 0x1f; IFC pulse; the same
register read followed in the same message by REN on; then the bank-2
configuration `02 03 01`, `02 04 01`, `02 05 PAD`, `02 06 SAD-byte-or-0`,
`02 07 timeout-code`; two bank-2 0x03 writes; a presence probe `02 PAD SAD
00` (§10.6.1); one more bank-2 0x03 write. Steps 1-4 and 6 are not in the
captures, which all start after NI's driver already owned the adapter
(they may belong to driver load). Whether the bank-2 configuration is
needed for anything is not established (8.15); the bench runs of
2026-09-18 predate its discovery and so ran the framed paths without it
**[bench]**. A board (INTFC) session sends the 26 writes and
five or six 0x21 / 0x0200 status queries only -- no IFC, no REN -- and is
not CIC until the application pulses IFC (§10.3.5, §10.6.4).

### 2.9 Shutdown

**[bench] [captured]** One register-write instruction with two writes:
bank 1 addr 0x0a value 0x02 (AUXMR chip reset), then bank 3 addr 0x10
value 0x00. Then release the interface. On the wire (§10.3.3): `09 02 00
01 0a 02 03 10 00 00 00 00 04 00 00 00`, sent by NI about 0.5 s after the
last `viClose`, at process end. The chip reset drops REN and the
instrument returns to local **[bench]** (8.18).

**[captured]** Do not test the ibsta of the shutdown reply: it read
0xffff in 25 of the 28 captures, 0x0000 in two (counts.pcap 20.5265,
read_thresholds.pcap 5.2342) and 0x1010 in one (board_io.pcap 9.6195);
see §10.3.3. `viClose` itself writes bank-2 0x03 := 1 and 0x04 := 0 (20
bytes).

**Superseded (2.9).** "the reply reads ibsta 0xffff", stated of every
capture after the first batch; replaced by the counts above (§10.3.3).
"(Sequence captured from NI's Windows driver.)" was the original
sources' statement [inherited]; this project's captures now confirm it
byte for byte.

---

## 3. Bulk message framing

### 3.1 Host -> device message shape

**[bench] [captured]** A message is one or more **instruction blocks**
followed by one **termination block**. Each instruction block starts with
a one-byte opcode. Every instruction block is zero-padded to a multiple
of 4 bytes. The termination block is exactly `04 00 00 00`.

```
+-----------+-----+------------+-----------+-----+-------------+
| opcode 1  | ... | pad to x4  | opcode 2  | ... | 04 00 00 00 |
+-----------+-----+------------+-----------+-----+-------------+
```

Transfers and replies:

- **[bench] [captured]** The message goes out on the primary bulk OUT
  endpoint (0x02 on the HS), normally as a single transfer. NI hands a
  message longer than 512 bytes to USB as two OUT transfers, 512 bytes
  and the remainder; on the bus that is still one run of packets ended by
  a short one, and the split carries no protocol meaning **[captured]**
  (§10.5.2).
- **[captured]** (§10.2.1) Every message produces exactly one reply on
  the primary bulk IN endpoint (0x84): one reply per block, in block
  order, followed by a single termination block. For a message of one
  instruction that is the reply of 3.5 **[bench]**.
- **[captured]** Two instructions move their *data* outside the message
  and its reply. A 0x0b delivers the bytes read as a separate transfer on
  bulk IN 0x88, which completes just before the reply on 0x84 (§10.1.3);
  a 0x0e takes the bytes to write as a separate transfer on bulk OUT 0x06
  (§10.5.2). Both still get their one reply on 0x84, also when the raw
  transfer fails (§10.6.7).
- Only one message may be outstanding at a time: read its reply (and
  finish its raw transfer, if any) before sending the next
  **[inherited]**. NI never did otherwise in the 946 messages of the 28
  captures **[captured]**; it does submit its IN transfers before the
  data can have come back (§10.6.7).

Which messages have been seen:

- **[bench]** single-instruction messages, and the 0x0a read followed by
  its register-write block (5.2).
- **[captured]** (§10.2.1) up to five blocks per message (0x03, 0x0c,
  data instruction, 0x09, 0x09; also 0x08 + 0x09 and 0x09 + 0x09); blocks
  execute in message order.
- Any other combination is untested.

**Superseded (3.1).** [inherited] "The whole message is written in a
single bulk OUT transfer" as an absolute; "Every message produces exactly
one reply on the bulk IN endpoint" without the raw transfers of 0x0b /
0x0e; "The only multi-instruction message observed is the read
instruction with its embedded register-write block (5.2). Batching any
other combination of opcodes in one message is untested." Replaced by
the list above (§10.2.1, §10.1.3, §10.5.2).

### 3.2 Host -> device opcodes

The "Reply" column gives the reply to the instruction sent alone in a
message: its own reply plus the termination block (an 8-byte status
block + `04 00 00 00` = the "12-byte status reply"). Inside a batched
message each block contributes its own reply without a termination block
of its own (§10.2.1).

| Opcode | Instruction | Fixed header bytes after the opcode | Payload | Reply | Evidence |
|--------|-------------|--------------------------------------|---------|-------|----------|
| 0x01 | Take control (adapter becomes active controller with ATN true) | `s 00 00` where s = 0x01 synchronous, 0x00 asynchronous | none | 12-byte status reply | [bench]; [captured] on INTFC sessions (§10.7.1) |
| 0x02 | Presence probe of a GPIB address | `P S 00` (P = primary address, S = 0x60 \| secondary or 0x00) | none | 16 bytes: status block + `01 00 00 00` (present) or `00 00 00 00` (absent) + termination | [captured] (§10.6.1) |
| 0x03 | Status snapshot: `03 00 00 00` replies with one 8-byte status block carrying the current ibsta; NI puts it first in nearly every instrument-operation message (§10.2.2) | `00 00 00` | none | 8-byte status block | [captured] (§10.2.2) |
| 0x04 | Termination block | `00 00 00` | none | n/a (also appears at the end of replies) | [bench] [captured] |
| 0x06 | Go to standby (ATN false) | `00 00 00` [bench]; NI sends `00 00 0a`, and `01 00 0a` for the "deassert after handshake" variant, byte 3 = 0x0a meaning not established [captured] (§10.7.1) | none | 12-byte status reply, id 0x06 | [bench] in instrument sessions; [captured] on INTFC sessions only |
| 0x07 | Parallel poll | `t 00 00` where t = timeout code (only 0xf0 observed) | none | status block + 1 result byte | [inherited] |
| 0x08 | Register read | `n` (count of reads) | n × (`bank addr`) | see 3.5 | [bench] [captured] |
| 0x09 | Register write | `n 00` (count of writes) | n × (`bank addr value`) | 16-byte register-write reply | [bench] [captured] |
| 0x0a | Read data from bus, data framed in the reply | `m e t cl ch 00 00` (see 5.2) | none of its own. This project's message puts a two-write 0x09 block behind it [bench]; NI puts none there [captured] (5.2) | data blocks + status, see 5.2 | [bench] [captured] |
| 0x0b | Read data from bus, data delivered raw on bulk IN 0x88 | `m e t c0 c1 c2 c3` (4-byte negative count, 3.3) | none | 12-byte block on 0x84: status block + `xx 00 00 00` (xx = 0xe0 EOI seen / 0x60 not); data on 0x88, padded to an even length (§10.1.3) | [captured] (§10.1.2) |
| 0x0c | Command bytes (adapter drives ATN true and leaves it true, 5.3) | `c 00 t` (c = 8-bit count code, t = timeout code) | up to 16 command bytes | 12-byte status reply | [bench] [captured]; the limit of 16 [inherited] (5 was the most captured) |
| 0x0d | Write data to bus | `cl ch t 00 e f 00` (see 5.1): e = 0x00 [bench], e = the session's termination character [captured] | data bytes | 12-byte status reply | [bench] [captured] |
| 0x0e | Write data to bus, data sent raw on bulk OUT 0x06 | `00 00 t 00 e f 00 c0 c1 c2 c3` (4-byte negative count, 3.3; block is 12 bytes) | none (data on 0x06) | 8-byte status block, id 0x0e | [captured] (§10.5.2) |
| 0x0f | Interface clear pulse (IFC) | `00 00 00` | none | 12-byte status reply | [bench] [captured] |
| 0x10 | Serial poll of one device | `01 00 x P S t 00` (x = 0x00 or 0x01, meaning not established; P, S as for 0x02 -- S = 0x61 observed; t = timeout code) | none | `3a P S sb` (sb = status byte) + 8-byte status block id 0x39; on failure the status block alone, without the `3a` block (§10.6.6) | [captured] (§10.5.4) |

**Superseded (3.2).** [inherited] Row 0x03: "Reserved / unknown; never
observed in use" -- NI uses it constantly (§10.2.2). Row 0x0a, payload:
"an embedded 0x09 block" as part of the instruction -- it has the form of
an ordinary 0x09 block, and NI's 0x0a carries none (5.2). Rows 0x0b /
0x0e: "32-bit negative count" stated flatly -- four bytes are sent, the
32-bit reading is likely but not settled (3.3, §10.1.2). Row 0x0d header
`cl ch t 00 00 f 00` as the only form -- NI fills byte 5 (5.1).

### 3.3 Count encoding

Byte counts in instructions are sent as the **two's-complement negative
of the length**:

- **[bench] [captured]** 16-bit counts (read 0x0a, write 0x0d): `cl ch` =
  (0x10000 - length) & 0xffff, little-endian.
- **[bench] [captured]** 8-bit count (command 0x0c): `c` = (0x100 -
  length) & 0xff.
- **[captured]** (§10.1.2, §10.5.2) 4-byte counts (raw read 0x0b, raw
  write 0x0e): `c0 c1 c2 c3`, little-endian; `00 b0 ff ff` for 20480,
  `fe f7 ff ff` for 2050. Every count captured is below 0x10000, so the
  bytes fit a 32-bit two's complement and equally a 16-bit count followed
  by `ff ff`; which one the adapter reads is **not settled** (§10.1.2).
  The reply count is 32 bits wide, which favours the 32-bit reading. A
  count of at most 0xffff is encoded identically under both.

Equivalently, bitwise-NOT of (length - 1). Examples: length 1 -> `ff ff`
(or `ff`); length 3 -> `fd`; length 6 -> `fa ff`; length 256 -> `00 ff`;
length 65535 -> `01 00`.

The count field in a reply status block (4.1) uses the same convention:
it holds (bytes transferred - bytes requested), so zero means "all
requested bytes were transferred". It is 16 bits (bytes 4-5) in replies
to 0x0a, 0x0c and 0x0d **[bench] [captured]** and 32 bits (bytes 4-7) in
replies to 0x0b and 0x0e **[captured]** (§10.1.3, §10.5.2: `00 00 00 00`
for a full 20480-byte chunk, `52 b0 ff ff` = 82 - 20480).

**Superseded (3.3).** "the 0x0b and 0x0e instructions carry the count as a
32-bit little-endian two's complement", stated flatly after the first
batch; §10.1.2 leaves the width of the instruction's field open, and the
bullet above says so.

### 3.4 Register-access blocks

Register write block (opcode 0x09):

```
09 nn 00 | b1 a1 v1 | b2 a2 v2 | ... | pad to x4
```
- `nn` = number of (bank, addr, value) triplets (1 byte; 26 is known to work).
- `b` = bank: 1 = TNT4882 controller chip, 2 = unknown bank, 3 = unknown
  device-level bank **[inherited]**; all three are written by the
  initialisation of 2.6 **[bench] [captured]**. **[captured]** (§10.2.4):
  NI writes bank-2 0x03
  := 1 before and after almost every operation (and polls with it), 0x04
  := 1 at session open / 0 at the last close, 0x05 := the instrument's
  primary address, 0x06 := 0x60 | secondary address or 0, 0x07 := the
  session's timeout code.
- `a` = register address within the bank (1 byte).
- `v` = value (1 byte).

Register read block (opcode 0x08):

```
08 nn | b1 a1 | b2 a2 | ... | pad to x4
```
- `nn` = number of (bank, addr) pairs: 1 **[bench]**; 2 and 3
  **[captured]**; 4 **[inherited]** (USB-B serial read, 2.4).

Bank 1 addresses. NEC 7210-compatible registers sit at **twice** their
7210 register number; TNT4882-specific registers sit at their native
one-chip-mode offsets. The names are **[inherited]** from the chip
documentation of section 9. Seen on a wire: the writes of 2.6 (0x02,
0x04, 0x06, 0x08, 0x0a, 0x0c, 0x0d, 0x0f, 0x12, 0x17, 0x1c, 0x1d)
**[bench] [captured]**; the read of 0x1f **[bench] [captured]**; the
reads of 0x0c and 0x0d **[captured]**. The rest of the table has not been
touched by this project.

| addr | write (host -> chip) | read (chip -> host) |
|------|----------------------|---------------------|
| 0x00 | CDOR (data out)      | DIR (data in) |
| 0x02 | IMR1                 | ISR1 |
| 0x04 | IMR2                 | ISR2 |
| 0x06 | SPMR (see 2.6 note a)| SPSR |
| 0x08 | ADMR                 | ADSR |
| 0x0a | AUXMR (aux commands and hidden registers AUXRA/B/E/G/I via top bits) | CPTR |
| 0x0c | ADR                  | ADR0 [inherited]. [captured] (§10.3.2): the value read here equalled the low byte of the current ibsta in every read captured (0x00, 0x30, 0x64, 0x74), which is not what an address register would hold; the label is kept, the reading is not explained |
| 0x0e | EOSR (never written) | ADR1 |
| 0x0d | HSSEL                | - |
| 0x0f | (not in NI's map; written 0x11 at init) | - |
| 0x12 | IMR3                 | IMR3 |
| 0x17 | KEYREG               | - |
| 0x1c | CMDR                 | STS2 |
| 0x1d | IMR0                 | ISR0 |
| 0x1f | BCR                  | BSR (bus line status, see 5.13) |

Other TNT4882 registers (CFG/STS1 0x10, CNT0..CNT3, CCR/ISR3 0x1a, SASR
0x1b, TIMER 0x1e) sit at their one-chip offsets and are not used by this
protocol.

AUXMR values used by this protocol (write to bank 1 addr 0x0a). The
meanings are **[inherited]** from the chip documentation; the last column
says where the value has been seen on a wire.

| value | meaning | seen |
|-------|---------|------|
| 0x00 | immediate execute pon | [bench] [captured] (2.6) |
| 0x01 | clear parallel-poll flag (ist = 0) | [bench] [captured] (2.6) |
| 0x02 | chip reset | [bench] [captured] (2.6, 2.9) |
| 0x05 | return to local | [inherited] only (5.15) |
| 0x09 | set parallel-poll flag (ist = 1) | [inherited] only |
| 0x14 | disable system control | [inherited] only |
| 0x16 | clear IFC | [bench] [captured] (2.6) |
| 0x17 | clear REN | [captured] (§10.7.1) |
| 0x1e | set IFC | [inherited] only |
| 0x1f | set REN | [bench] [captured] (5.6) |
| 0x51 | holdoff handshake immediately | [bench] [captured] (2.6); behind a read [bench] only (5.2) |
| 0x55 | clear END status | behind a 0x0a [bench]; behind a 0x0b [captured] (5.2) |
| 0x60 \| cfg | parallel-poll register (PPR) load | [inherited] only |
| 0x80 \| bits | AUXRA: bit0 holdoff-all, bit1 holdoff-on-END, bit2 REOS, bit3 XEOS, bit4 BIN | 0x81 [bench]; 0x99 [captured] (2.6) |
| 0xa0 \| bits | AUXRB: bit0 CPT enable, bit1 SPEOI, bit2 TRI, bit4 ISS | 0xa4 [captured]; the row of 2.7 in use [bench] |
| 0x40 \| bits | AUXRG: bit0 CHES, bit3 NTNL | 0x48 [bench] [captured] |
| 0xe0 \| bits | AUXRI: bit0 SISB, bit2 PP2, bit3 USTD | 0xe9 [captured]; the row of 2.7 in use [bench] |

CMDR values used (bank 1 addr 0x1c): 0x02 clear system controller
**[inherited]**, 0x03 set system controller, 0x22 soft reset **[bench]
[captured]** (2.6). (Also defined by the chip: 0x04 GO, 0x08 STOP, 0x10
reset FIFOs.)

**Superseded (3.4).** "`nn` = number of (bank, addr) pairs (1 and 4
observed)" -- 2 and 3 are now captured as well. "all 50 reads captured"
for the 0x0c reading was the first batch's count; the six later captures
add 18 reads that agree.

### 3.5 Device -> host replies

Reply block ids:

| id | Block | Length | Evidence |
|----|-------|--------|----------|
| 0x01, 0x06, 0x09, 0x0c, 0x0d, 0x0f | status block echoing the instruction opcode | 8 bytes (4.1) | [bench] [captured] |
| 0x07 | status block of a parallel poll | 8 bytes (4.1) | [inherited] |
| 0x34 | register-read data chunk: `34 r r r` (up to 3 register values) | 4 bytes | [bench] [captured] |
| 0x35 | register-read end: `35 k 00 00`; k = the number of registers read, for 1 read [bench] and for 2 and 3 reads [captured] (§10.8). For more than 3 reads k is not established: the sources leave open whether it is the total or the count in the last chunk, and both give k mod 3 = the number of reads mod 3 [inherited] | 4 bytes | [bench] [captured] |
| 0x36 | read-data block: id + 15 data bytes | 16 bytes | [bench] [captured] |
| 0x37 | extended read-data block: `37 00` + 30 data bytes | 32 bytes | [bench] [captured] |
| 0x38 | read-data status block (4.1) | 8 bytes | [bench] [captured] |
| 0x04 | termination `04 00 00 00` | 4 bytes | [bench] [captured] |
| 0x02, 0x03, 0x0b, 0x0e, 0x21, 0x39 | status blocks of the instructions of §10.2.1; 0x21 arrives as the control-request reply, 0x39 follows the 0x3a block of a serial poll | 8 bytes | [captured] |
| 0x11 | `11 00 00 00`, four in a row before the first 0x37 block of a 0x0a reply that follows other blocks (§10.1.5); skip | 4 bytes | [captured] |
| 0x3a | serial-poll result `3a P S sb` (§10.5.4); absent when the poll fails (§10.6.6) | 4 bytes | [captured] |

Register-read reply layouts:

- 1 register **[bench]**: `34 vv 00 00 | 35 01 00 00 | 04 00 00 00`,
  exactly 12 bytes; the termination block does follow.
- 2 and 3 registers **[captured]** (§10.3.2, §10.8): `34 v0 v1 v2 | 35 k
  00 00`, k = 2 or 3; with 2 reads the third value byte is stale.
- 4 registers (USB-B serial read) **[inherited]**: `34 v0 v1 v2 | 34 v3
  pp pp | 35 k 00 00 | 04 00 00 00`, where `pp` is padding of the partial
  final chunk (value unverified; expect 0x00) and k mod 3 = 1.
- Request 32 bytes and accept a shorter transfer.

Reply sizes to expect when the instruction is alone in its message (in a
batched message the termination block comes once, at the end, §10.2.1):

| After instruction | Total bytes | Layout | Evidence |
|-------------------|-------------|--------|----------|
| 0x01, 0x06, 0x0c, 0x0d, 0x0f | 12 | status block (8) + `04 00 00 00` | [bench] [captured] |
| 0x09 register write | 16 | status block (8, id 0x09) + `k 00 00 00` (k = writes completed) + `04 00 00 00` | [bench] [captured] |
| 0x08 register read | up to 32 | 0x34 chunks + 0x35 block + termination | [bench] [captured] |
| 0x07 parallel poll | up to 32 | status block (8) + result byte + padding (+ termination, uncertain) | [inherited] |
| 0x0a read | see 5.2 | data blocks + 12 bytes of the read's own (status block + 4-byte tail) + the reply of any block behind it + termination | [bench] [captured] |
| 0x02 probe | 16 | status block (8, id 0x02) + `01 00 00 00` / `00 00 00 00` + `04 00 00 00` | [captured] (§10.6.1) |
| 0x0b read | 12 on 0x84 (+ data on 0x88) | status block (8, id 0x0b) + `xx 00 00 00`; never captured alone in a message | [captured] (§10.1.3) |
| 0x10 serial poll | 12, or 8 on failure | `3a P S sb` + status block (8, id 0x39); the status block alone when the poll fails (§10.6.6); never captured alone in a message | [captured] (§10.5.4) |

The 12- and 16-byte reply lengths of single-instruction messages are
exact and can be asserted **[bench]**: the trailing 4 bytes of every such
reply are `04 00 00 00`.

**Superseded (3.5).** [inherited] 0x35: "k appears to be a count
(uncertain: total or last-chunk; k mod 3 equals the number of reads mod
3)" -- settled for up to 3 reads, k = the number read (§10.8); still open
above 3. "0x08 register read ... (+ termination, uncertain)" and the
bracketed `[| 04 00 00 00]` of the layouts -- the termination block is
sent [bench]. "0x0a read: data blocks + 16-byte trailer (observed; the
sources implied 28)" -- both lengths are now accounted for, see 5.2.
"0x10 serial poll: 12" -- 8 on failure (§10.6.6).

### 3.6 Worked hex examples

All examples use timeout code 0xfc (3 s), controller primary address 0,
instrument primary address 22 (0x16). They are single-instruction
messages as this project's driver sends them; NI's batched forms are in
section 10. In the IN lines, bytes 4-7 of the status blocks of 0x01 and
0x09 and bytes 6-7 of the others are written `00`; on the wire they hold
stale values (4.1) **[bench] [captured]** and must not be asserted.

**Register write, one write (REN on)** [bench] [captured]:
```
OUT: 09 01 00 01 0a 1f 00 00 04 00 00 00
IN : 09 ss ss 00 00 00 00 00 01 00 00 00 04 00 00 00
```
`ss ss` = ibsta big-endian; byte 3 = error 0; byte 8 = 1 write completed.

**Command bytes UNL, MTA 0, LAD 22 (address instrument to listen)**
[bench] in this byte order; NI sends `40 3f 36`, MTA first [captured]
(section 6):
```
OUT: 0c fd 00 fc 3f 40 36 00 04 00 00 00
IN : 0c ss ss ee cc cc 00 00 04 00 00 00
```
`fd` = -3 (three command bytes). `ee` = error code; `cc cc` = 0 if all
three were accepted.

**Write `*IDN?\n` (6 bytes) with EOI on the last byte** [bench]; NI's
byte 5 holds the termination character, `0d fa ff fc 00 0a 08 00 ..`
[captured] (5.1):
```
OUT: 0d fa ff fc 00 00 08 00 2a 49 44 4e 3f 0a 00 00 04 00 00 00
IN : 0d ss ss ee cc cc 00 00 04 00 00 00
```
`fa ff` = -6 little-endian; byte 6 = 0x08 requests EOI; two pad bytes
after the data.

**Read up to 256 bytes, EOS disabled (OUT [bench]; IN [inherited], as
the sources implied it, NOT what the bench device sent -- see the next
example and 5.2):**
```
OUT: 0a 00 00 fc 00 ff 00 00 09 02 00 01 0a 51 01 0a 55 00 00 00 04 00 00 00
IN : 36 41 42 43 44 45 0a xx xx xx xx xx xx xx xx xx
     38 ss ss 00 06 ff 00 00
     aa 06 00 00
     09 ss ss 00 00 00 00 00
     02 00 00 00
     04 00 00 00
```
Instrument answered `ABCDE\n` (6 bytes) with EOI. One 0x36 block; the
`xx` filler after the 6 valid bytes is unspecified. Status: id 0x38, error
0, count `06 ff` = 0xff06 = 6 - 256 (250 bytes not transferred). `aa` =
ADR1 register bits (ignored); `06` = valid bytes in the last data block;
`00 00` pad. The two blocks `09 ...` / `02 00 00 00` (status of the
embedded 2-register write) were inferred from the sources and are **not
sent** by the GPIB-USB-HS; a parser must accept their absence.

**Read up to 256 bytes, EOS disabled -- observed on GPIB-USB-HS 01CEE482
with a Keithley 2400 at PAD 3, 2026-09-18:**
```
OUT: 0a 00 00 fc 00 ff 00 00 09 02 00 01 0a 51 01 0a 55 00 00 00 04 00 00 00
IN : 37 00 4b 45 49 54 48 4c 45 59 20 49 4e 53 54 52 55 4d 45 4e 54 53 20 49 4e 43 2e 2c 4d 4f 44 45
     37 00 4c 20 32 34 30 30 2c 31 31 37 35 36 38 30 2c 43 33 30 20 20 20 4d 61 72 20 31 37 20 32 30
     37 00 30 36 20 30 39 3a 32 39 3a 32 39 2f 41 30 32 20 20 2f 4b 2f 4a 0a 00 00 00 00 00 00 00 00
     38 20 20 00 52 ff ff ff
     e0 16 00 00
     04 00 00 00
```
112 bytes in one USB transfer (64 + 48). Payload: `KEITHLEY INSTRUMENTS
INC.,MODEL 2400,1175680,C30   Mar 17 2006 09:29:29/A02  /K/J\n` (82
bytes) in three 30-byte 0x37 blocks, the last holding 22 valid bytes
(`16`) and then filler. Status: ibsta 0x2020 (END, CIC), error 0, count
`52 ff` = 0xff52 = 82 - 256; bytes 6-7 `ff ff`. `e0` = ADR1 with bit 7 set
(EOI seen); `00 00` pad; termination. Trailer = 16 bytes.

Also observed (same setup): a 1-byte read (serial poll) and a 5-byte read
arrive in 0x36 blocks (`36 00 20 00 aa 55 ff ff ... | 38 00 20 00 00 00 01
00 60 01 00 00 04 00 00 00`), so the block size depends on the request;
the filler after the valid bytes is stale data from earlier replies; ADR1
is 0x60 without EOI. A read that times out with nothing to read returns
the bare 16-byte trailer `38 00 20 0a c0 ff ff ff e0 5e 00 00 04 00 00 00`
(error 0x0a, count 0xffc0 = 0 - 64); the last-block-count byte (`5e`) is
meaningless when there is no data block.

**Register read of BSR (bus lines)** [bench]; the termination block,
bracketed because the sources left it open, is sent (3.5):
```
OUT: 08 01 01 1f 04 00 00 00
IN : 34 vv 00 00 35 01 00 00 [04 00 00 00]
```
`vv` = BSR value (5.13).

**Take control, synchronous** [bench]:
```
OUT: 01 01 00 00 04 00 00 00
IN : 01 ss ss ee 00 00 00 00 04 00 00 00
```

---

## 4. Status block

### 4.1 Layout (8 bytes)

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| 0 | 1 | id | echoes the instruction opcode; 0x38 for the 0x0a read-data status [bench] [captured]; 0x39 for the 0x10 serial poll, 0x21 for the status control request [captured] |
| 1 | 2 | ibsta | **big-endian** (byte 1 = high byte) [bench] [captured] |
| 3 | 1 | error code | see 4.3; 0 = success [bench] [captured] |
| 4 | 2 | count | **little-endian**, two's complement: (transferred - requested). Bytes not transferred = (0x10000 - count) & 0xffff; bytes transferred = requested - that. 0 when everything was transferred [bench] [captured]. 4 bytes wide (offsets 4-7) in replies to 0x0b and 0x0e [captured] |
| 6 | 2 | no meaning of their own | see below; ignore, except as the high half of a 0x0b / 0x0e count |

The count is **meaningful only in replies to the data instructions**:
0x0a, 0x0c, 0x0d **[bench] [captured]**, 0x0b, 0x0e (32 bits)
**[captured]**. Everywhere else it is left over:

- **[bench]** (unit 01CEE482) replies to 0x01 and 0x06 carried `aa 55`;
  replies to 0x09 and 0x0f the count of the last data operation (`ff ff`
  after a reset, `52 ff` after a read that transferred 82 of 256).
- **[captured]** (unit 013CC9DF, §10.8) replies to 0x01, 0x06, 0x0f, 0x03
  and 0x09 all carried the stale count of the last data operation; `aa
  55` appeared once, in the first reply of one capture (§10.3.1). The
  0x02 probe writes the field to 0.
- The two units thus differ in what 0x01 / 0x06 leave there; nothing
  depends on it.

Bytes 6-7: `00 00` according to the sources **[inherited]**. **[bench]**:
`ff ff` in most replies, `01 00` in a read reply that filled the
requested count, `00 00` after a successful 0x0d. **[captured]** (§10.8):
`ff ff` in every reply until the first 0x0b / 0x0e of a session,
thereafter the high half of that instruction's 32-bit count and `00 00`
in later 0x0c / 0x0d replies.

The control requests 0x20 **[inherited]** and 0x21 **[captured]** return
the same 8-byte layout. The one interrupt push captured has the same
length and ibsta position but byte 0 = 0x30, byte 3 = the instrument's
status byte and bytes 4-7 not established (2.5) **[captured]**.

**Superseded (4.1).** "Meaningful only in replies to 0x0a, 0x0c, 0x0d" --
0x0b and 0x0e added. "unused: observed 0x00 0x00 (sources)" for bytes
6-7 -- contradicted by bench and captures alike. "The same 8-byte layout
is ... pushed on the interrupt endpoint" -- true of the length only, for
the one push seen.

### 4.2 ibsta bits (NI-488.2 convention, public)

| Bit | Value  | Name | Meaning |
|-----|--------|------|---------|
| 15 | 0x8000 | ERR  | error occurred |
| 14 | 0x4000 | TIMO | timeout |
| 13 | 0x2000 | END  | EOI (or EOS match, when enabled) ended the read |
| 12 | 0x1000 | SRQI | SRQ line asserted |
| 11 | 0x0800 | RQS  | device requesting service (device-level) |
| 10 | 0x0400 | SPOLL | board has been serial-polled |
| 9  | 0x0200 | EVENT | DCAS, DTAS or IFC event queued |
| 8  | 0x0100 | CMPL | I/O complete |
| 7  | 0x0080 | LOK  | lockout state |
| 6  | 0x0040 | REM  | remote state |
| 5  | 0x0020 | CIC  | adapter is controller-in-charge |
| 4  | 0x0010 | ATN  | ATN asserted |
| 3  | 0x0008 | TACS | adapter addressed as talker |
| 2  | 0x0004 | LACS | adapter addressed as listener |
| 1  | 0x0002 | DTAS | device trigger state |
| 0  | 0x0001 | DCAS | device clear state |

Bits of the device's ibsta field that are reliable as bus state: mask
0x10fc (LACS, TACS, ATN, CIC, REM, LOK, SRQI), plus END (0x2000) in a
read reply as the end-of-read indication **[inherited]**. Seen following
the bus state: CIC, END **[bench]**; LACS, TACS, ATN, CIC, REM, END
(§10.3.5) and LOK (§10.7.4) **[captured]**. SRQI was seen only in the
interrupt push, together with RQS (2.5), and in one shutdown reply
(§10.7.2); never in a 0x21 reply or an ordinary bulk reply, the adapter
having polled the device itself first (§10.4.4). Derive ERR, TIMO and
CMPL from the error code rather than from this field: replies with error
0x0a, 0x08 and 0x07 had ERR and TIMO clear **[captured]** (§10.1.3,
§10.6.2, §10.6.4).

### 4.3 Error codes (byte 3 of the status block)

Only 0x00, 0x07, 0x08 and 0x0a occur in the 28 captures; 0x00 and 0x0a in
the bench notes of this document. The causes given for the others are
the original sources'.

| Code | Label used in this document | Cause / recovery | Evidence |
|------|-----------------------------|------------------|----------|
| 0x00 | success | - | [bench] [captured] |
| 0x01 | cut short by a stop request | control request 0x20 (5.11) ended the operation early; the partial count is valid | [inherited] |
| 0x02 | read attempted while ATN true | a 0x0a was issued with ATN asserted; send go-to-standby (0x06) first (5.3). Not reproduced: in all 103 captured reads that follow a 0x0c in the same message, and in the one bare 0x0a sent as its own message while ATN was set (§10.7.2), the read released ATN itself and no error 2 came back. When the adapter does return it is not established | [inherited] |
| 0x03 | not addressed | read/write as controller while the adapter is not addressed as listener/talker; send the addressing command bytes (section 6) first | [inherited] |
| 0x04 | EOS configuration rejected / command chunk too long | read with a nonzero EOS mode byte or EOS character while the REOS bit (0x04) is clear; also returned by the USB-B for a 0x0c instruction carrying 17 or more command bytes. Not reproduced: NI sends `m` = 0x00 with `e` = 0x0a on every unterminated read, 100 of them in the captures, and never gets error 4 (§10.1.6). Those were all under AUXRA 0x99 (2.6); whether the rule holds under 0x81 is not established | [inherited] |
| 0x05 | no acceptor on the bus | command bytes were not accepted by any device (bus empty or unpowered); harmless after a take-control on an empty bus | [inherited] |
| 0x07 | not controller in charge | 0x01, 0x06 or 0x0c issued while the adapter is not CIC (e.g. a board session before any IFC); ibsta 0x0000 and bytes 4-7 `ff ff ff ff` in the reply; NI-VISA reports VI_ERROR_NCIC | [captured] (§10.6.4) |
| 0x08 | no listener addressed | data write as controller with no listener addressed: reply `0d 00 28 08 f9 ff ff ff` for a 7-byte write to an empty address, 1 ms after the instruction; NI-VISA reports VI_ERROR_NLISTENERS (§10.6.2). For a 0x0e: `0e 00 28 08` + 32-bit count, and the data transfer on 0x06 is refused with a STALL (§10.6.5) | [captured] |
| 0x0a | device-side timeout | the adapter's wait under the timeout code (7.3) expired; the partial count is valid | [bench] [captured] |
| other (6, >= 11) | unknown | treat as a generic I/O error | [inherited] |

One implementation assigns different meanings to codes 4-7 and appears to
be wrong; the table above is the one to use **[inherited]**; for code 7
the captures agree with the table.

**Superseded (4.3).** Row 0x04 stood with no note that §10.1.6
contradicts it for `e` without `m`; the row now says so. Row 0x02 read as
"ATN true always gives error 2"; the captures show the read instructions
dropping ATN themselves (§10.1.2, §10.1.5).

---

## 5. Operations

Each operation below is: message sent, reply expected, how outcome is
recognised. "Status reply" means the 12-byte reply of 3.5 to an
instruction sent alone. For every operation, error code 0 with the
expected id is success; nonzero error code maps via 4.3.

A USB-level timeout while waiting for the reply means the host-side wait
(7.2) was too short relative to the adapter's own expiry (7.3), and the
device still owes a reply. **[inherited]**: send the stop request (5.11)
and then read the reply. **[captured]**: in every failure NI's driver was
captured in (no listener on a 0x0d and a 0x0e, device timeout on 0x0a,
0x0b and 0x10; §10.6.2-10.6.7) the reply arrived by itself with the error
code and NI sent no stop request, before or after. A host wait sized per
7.2 never reaches this case.

**ATN rule.** A 0x0c leaves ATN asserted (5.3) and a read needs it
released. Two ways of getting there have each been seen to work; they
have not been mixed:

- **[bench]** Framed path of this project: after any 0x0c, send a 0x06
  (go to standby) as its own message before the next 0x0a. Sending 0x06
  before a 0x0d is likewise harmless **[inherited]**; sequencing 0x0c
  directly into 0x0d works **[bench] [captured]**.
- **[captured]** (§10.1.2, §10.1.5) NI never sends 0x06 in an instrument
  session. Its 0x0c is followed in the same message by the 0x0a, 0x0b or
  0x0d; the 0x0c reply shows ATN set and the data instruction's reply
  shows it clear: the read instruction drops ATN itself. None of the 103
  such reads (44 x 0x0a, 59 x 0x0b) returned error 2. A bare 0x0a sent as
  its own message with ATN still set did the same (§10.7.2).
- Not seen by anyone: 0x06 followed by 0x0b or 0x10; and whether the
  adapter ever returns error 2 (4.3).

**Superseded (5, preamble).** [inherited] "after any 0x0c, send a 0x06
before the next 0x0a. This ... avoids error 2", as a rule applying
throughout: it is one of two working forms. The first-batch counts "38
such 0x0a and 45 such 0x0b" are the 22-capture subset of the 103.

### 5.1 Write data (0x0d)

```
0d cl ch t 00 e f 00 <data...> <pad to x4> 04 00 00 00
```
- `cl ch` = -(length) 16-bit little-endian (3.3) **[bench] [captured]**.
  Max length 0xffff **[inherited]**; the longest 0x0d captured carried
  2048 bytes (§10.5.2).
- `t` = timeout code **[bench] [captured]**.
- `e` (byte 5): 0x00 **[bench]**. NI fills it with the session's
  termination character (0x0a by default, 0x2c / 0x0d when
  VI_ATTR_TERMCHAR was changed) on every write; byte 4 stays 0x00
  **[captured]** (§10.5.1). Whether byte 5 has an effect was not tested.
- `f` = 0x08 to assert EOI with the last byte, 0x00 otherwise **[bench]
  [captured]** (follows VI_ATTR_SEND_END_EN, §10.5.1).
- Reply: status reply, id 0x0d. Bytes written = length - (bytes not
  transferred from the count field) **[bench] [captured]**.
- **[captured]** (§10.5.2) NI sends every length up to 2048 bytes as one
  0x0d in exactly this layout and switches to the raw 0x0e (data on bulk
  OUT 0x06) at 2049 bytes.
- **[inherited]** If a caller's buffer exceeds 0xffff, split it into
  instructions of at most 0xffff bytes and set the EOI flag only on the
  last one.
- Precondition: the adapter must be talker and the target listener
  (section 6) -- otherwise error 8 **[captured]** (§10.6.2) or error 3
  **[inherited]**.

**Superseded (5.1).** Header `0d cl ch t 00 00 f 00` with both bytes 4
and 5 fixed at 0x00 [inherited]: still what this project sends, but byte
5 is a field NI uses. "the longest 0x0d captured carried 17 bytes" and
"for a 2050-byte write NI used the 0x0e": first-batch statements,
replaced by the 2048 / 2049 boundary (§10.5.2).

### 5.2 Read data (0x0a)

This project's message **[bench]**, 24 bytes:
```
0a m e t cl ch 00 00  09 02 00 01 0a 51 01 0a 55  <pad to x4>  04 00 00 00
```
NI's message **[captured]** (§10.1.5), 32 bytes, no AUXMR block:
```
03 00 00 00 | 0c fd 00 fd 3f 20+C 40+N 00 | 0a m e t cl ch 00 00 | 09 01 00 02 03 01 00 00 | 04 00 00 00
```
- `m` = EOS mode byte: 0x04 = terminate on EOS character (REOS), 0x10 =
  compare all 8 bits (BIN; otherwise 7-bit compare), 0x08 = XEOS (not
  meaningful for reads) **[inherited]**; 0x14 = REOS + BIN ends the read
  on the character **[captured]** (§10.1.6).
- `e` = EOS character.
- EOS disabled, two forms that both work: `m` = `e` = 0x00 **[bench]**
  (under AUXRA 0x81); `m` = 0x00 with `e` = the session's termination
  character, 0x0a **[captured]** (§10.1.6; 100 reads under AUXRA 0x99,
  none with error 4). The [inherited] rule "if EOS is disabled, `m` and
  `e` must both be 0x00, else error 4" has not been reproduced; whether
  it holds under AUXRA 0x81 is not established. `00 00` is safe under
  both.
- An EOS match sets END exactly like EOI; bit 7 of the tail byte (item 3
  below) tells them apart **[captured]** (§10.1.6).
- `t` = timeout code.
- `cl ch` = -(max length) 16-bit little-endian. Max 0xffff
  **[inherited]**; counts seen on a wire: up to 256 **[bench]**, up to
  1024 **[captured]**. For counts above 1024 NI uses the 0x0b instruction
  with raw data on bulk IN 0x88 (§10.1.1-10.1.4): 1024 is the last count
  it sends as 0x0a and 1025 the first it sends as 0x0b.
- The register-write block behind the read. **[bench]**: this project
  sends a 0x09 block with two AUXMR writes, 0x51 (holdoff immediately)
  and 0x55 (clear END), meant to stop the talker at the byte boundary and
  clear the END latch for the next read **[inherited]**. **[captured]**:
  NI's 0x0a carries no such block at all, and its 0x0b is followed by a
  separate `09 01 00 01 0a 55` block (clear END only, no 0x51). Both
  work.
- EOS configuration: the per-read bytes `m`/`e` alone select EOS
  termination and the 8-bit compare; they may change from read to read
  without any register write **[captured]** (§10.1.6). Init write #3
  (2.6) carries the BIN bit only when the initialisation is (re)run while
  an 8-bit compare is already configured; a working sequence is 0x81 at
  attach followed later by reads with `m` = 0x14, so the two need not
  agree **[inherited]**. Uncertain: whether the BIN bit in write #3 has
  any effect at all, and whether changing the EOS mode later ever
  requires re-sending write #3 (no such re-send is known to be needed).
  EOSR is never written.
- Reply layout, in order **[bench] [captured]**:
  1. When the 0x0a reply follows other blocks in the same reply and uses
     0x37 sizing: four `11 00 00 00` blocks, also when no data block
     follows **[captured]** (§10.1.5). Skip them.
  2. Zero or more data blocks, each either `36` + 15 bytes or `37 00` +
     30 bytes. Requested counts 1..15 -> 0x36 blocks, 16 and above ->
     0x37 blocks **[captured]** (§10.1.5; on the bench 1 and 5 -> 0x36,
     256 -> 0x37). All blocks in one reply share one size in everything
     seen; uncertain whether the device ever mixes the two.
  3. 8-byte status block, id 0x38.
  4. 1 byte: ADR1 register contents (bit 7 = EOI seen with last byte;
     0xe0 / 0x60 in everything seen); may be ignored -- use ibsta END --
     except to tell EOI from an EOS match.
  5. 1 byte: number of valid data bytes in the LAST data block
     (meaningless if there is none).
  6. 2 bytes: 0x00 0x00.
  7. The reply of whatever block follows the 0x0a in the message (see
     the next bullet), then `04 00 00 00`.
- Length of what follows the data blocks. Items 3-6 are the read's own
  12 bytes. **[bench]**: behind them came the termination block and
  nothing else -- a fixed 16-byte trailer; the two-write 0x09 block drew
  no status of its own. **[captured]**: the one-write 0x09 block behind
  NI's 0x0a drew the usual 12-byte register-write status (`09 ss ss 00 cc
  cc xx xx 01 00 00 00`), so 12 + 12 + 4 = 28 bytes follow the data
  (partial.pcap 0.5366 IN84 60 B). 28 bytes is also the length the
  sources implied for this project's message (a status block with id 0x09
  and `02 00 00 00` before the termination) **[inherited]**, which the
  bench unit did not send. Why the bench unit answered the block behind
  the read with nothing and the captured unit with a status is not
  established; **a parser must accept both**: after the 4-byte tail,
  either `04 00 00 00` or a 0x09 status followed by it.
- Filler after the valid bytes of the last block is unspecified: stale
  bytes in 0x36 and 0x37 blocks **[bench]**; zeros in 0x36 blocks and
  stale bytes in 0x37 blocks **[captured]** (§10.1.5). A timed-out
  0x36-sized read still carries one 0x36 block: zero-filled
  **[captured]**, or holding stale bytes **[bench, unit 01CEE482,
  2026-09-21]** -- `36 00 20 00 aa 55 ff ff 04 00 00 00 4e 53 54 52` (the
  tail of an earlier status block and of "INSTR"). Never read past the
  valid count.
- **The last-block-count byte (item 5) is not zero when nothing was
  read** **[bench, 01CEE482]**: on three timed-out reads it held
  min(requested, block size) -- 0x01 for count 1, 0x0a for count 10,
  0x1e for count 20480 -- while the 0x38 count field said 0 bytes were
  transferred (error 0x0a). Replies, count 1 and count 10:
  `36 00 20 00 aa 55 ff ff 04 00 00 00 4e 53 54 52 | 38 00 20 0a ff ff ff
  ff e0 01 00 00 04 00 00 00` and `36 00 20 00 aa 55 ff ff 04 00 00 00 04
  00 00 00 | 38 00 20 0a f6 ff ff ff e0 0a 00 00 04 00 00 00`. The 0x38
  count field is the authority for how many bytes were read; the byte in
  item 5 only says where the valid bytes of the last block end when the
  count field says there are some. A parser that derives the byte count
  from item 5 and then checks it against the count field rejects every
  timed-out 0x36-sized read on this unit.
- Bytes actually read = (blocks - 1) × block_size + last_block_count, or
  0 if no data block (the last-block-count byte is then meaningless).
  Cross-check: it must equal requested - (bytes not transferred from the
  0x38 count field) **[bench] [captured]**.
- Host receive buffer for a requested N: ceil(N/30) 32-byte blocks (or
  ceil(N/15) 16-byte blocks) plus 28 bytes for what follows them (the
  longer of the two forms above); request the larger of the two, rounded
  up to the endpoint's max packet size. In a batched message add the
  replies of the blocks in front and 16 bytes of 0x11 blocks (8.6). The
  device ends the reply with a short packet.
- End conditions **[bench] [captured]**: ibsta END (0x2000) set in the
  0x38 block means the read ended on EOI or, when `m` has 0x04, on the
  EOS character (NI-488.2 convention). END clear with error 0 means the
  count was reached. Error 0x0a means the timeout expired; the partial
  data is valid.
- Precondition: adapter listener, target talker (section 6); otherwise
  error 3 **[inherited]**. ATN: either send 0x06 after the addressing
  0x0c **[bench]**, or put the 0x0a directly behind the 0x0c in the same
  message, where it drops ATN itself **[captured]** (ATN rule, section
  5).

**Superseded (5.2).** [inherited] "**If EOS is disabled, `m` and `e` must
both be 0x00**, else error 4" -- not reproduced, see above and §10.1.6.
"The embedded 0x09 block ... is always present; its status follows the
read status in the reply, so it is evidently executed after the read" --
NI sends none, and on the bench its status did not follow. "Items 2-6
form a fixed 16-byte trailer ... the device does not send [the 28-byte
form]" -- true of the bench message; the captured message draws 28.
"The filler after the valid bytes of the last block is stale data, not
zeros" -- zeros were captured in 0x36 blocks. "Precondition: ... ATN
false (send 0x06 after the addressing 0x0c) -- otherwise error 3 or 2"
-- one of two working forms; error 2 never seen.

### 5.3 Command bytes (0x0c)

```
0c c 00 t <cmd bytes...> <pad to x4> 04 00 00 00
```
- `c` = -(count) 8-bit **[bench] [captured]**. **Maximum 16 command bytes
  per instruction** on all models (the USB-B returns error 4 for a 0x0c
  instruction carrying 17 or more). Split longer sequences into
  consecutive 0x0c messages, each with its own reply **[inherited]**; 5
  bytes is the most captured.
- `t` = timeout code. Which code: the session's **[bench]** (5.18); NI
  puts 0xfd in the addressing 0x0c of every instrument operation
  regardless of the session timeout, and the session's code in
  board-level `viGpibCommand` **[captured]** (§10.1.9). Both work. No
  0x0c timed out in any capture, so what the code does there was not seen
  (7.3).
- Reply: status reply, id 0x0c. Bytes accepted = count - (not
  transferred) **[bench] [captured]**.
- Error 5 = nothing on the bus accepted the byte (no devices powered).
  Error 0x0a = a device held off the handshake for the whole timeout
  **[inherited]**. Error 7 = the adapter is not CIC **[captured]**
  (§10.6.4).
- ATN **[captured]** (§10.2.3, §10.7.2): the adapter drives ATN true for
  the instruction and leaves it true afterwards -- the 0x0c reply's ibsta
  shows ATN set (0x0038 after talk-addressing the adapter, 0x0074 after
  listen-addressing it). The read instruction that follows releases it,
  or a 0x06 does (ATN rule, section 5). A preceding 0x01 (take control)
  is not required once the adapter is CIC but is harmless
  **[inherited]**.

**Superseded (5.3).** [inherited] "Whether it releases ATN afterwards is
uncertain" -- it does not (§10.2.3).

### 5.4 Take control (0x01) / go to standby (0x06)

- Take control **[bench]**; **[captured]** on INTFC sessions (§10.7.1):
  `01 s 00 00 04 00 00 00`, s = 0x01 synchronous (wait for the current
  handshake to finish), 0x00 asynchronous. Reply: status reply. After
  success ibsta shows CIC (0x20) and ATN (0x10).
- Go to standby: `06 00 00 00 04 00 00 00` **[bench]**. Reply: status
  reply with id 0x06. ATN false; the previously addressed talker may now
  source data. NI sends `06 00 00 0a` (reply ibsta 0x0020, ATN clear) and
  `06 01 00 0a` for VI_GPIB_ATN_DEASSERT_HANDSHAKE, on INTFC sessions
  only **[captured]** (§10.7.1); byte 3 = 0x0a meaning not established.
  Both forms have been accepted; byte 3 = 0x00 with byte 1 = 0x01 has not
  been seen.
- **[captured]** Both 0x01 and 0x06 return error 7 when the adapter is
  not CIC (§10.6.4): take-control does not make the adapter CIC, an IFC
  pulse does (5.5).

### 5.5 Interface clear -- IFC pulse (0x0f)

**[bench] [captured]** `0f 00 00 00 04 00 00 00`. Reply: status reply.
The device pulses IFC (IEEE-488.1 requires >= 100 us; the pulse length is
the device's) and the adapter becomes CIC: the reply ibsta is 0x0030
(CIC, ATN) and BSR reads 0xa0 (ATN, NDAC) afterwards; NI sends no
take-control after it **[captured]** (§10.3.2, §10.7.1). Wait before the
first addressed command (8.18) **[bench]**. Only meaningful when the
adapter is system controller (2.6 row 16 = 0x03). There is no separate
"assert IFC" / "release IFC" instruction; AUXMR 0x1e / 0x16 can be
written via register writes for a manual pulse if ever needed
**[inherited]**.

### 5.6 Remote enable on / off

Register write, one write: bank 1 addr 0x0a value 0x1f (REN on) **[bench]
[captured]** or 0x17 (REN off) **[captured]**. Reply: 16-byte
register-write reply. A device enters remote state when REN is true and
it is addressed to listen. **[captured]** (§10.7.1): NI precedes the
write in the same message with a read of bank-1 0x0d, 0x0c, 0x1f, and
BSR bit 0 follows REN (0x00 / 0x01 with the bus otherwise idle).

### 5.7 Take / release system control

**[inherited]** Register write:
- Take: `01 1c 03` (CMDR set SC), `01 0a 16` (clear IFC) -- 2 writes.
- Release: `01 0a 17` (clear REN), `01 0a 16` (clear IFC), `01 0a 14`
  (disable system control), `01 1c 02` (CMDR clear SC) -- 4 writes.

### 5.8 Device clear

Command bytes (5.3). Two byte orders, both valid IEEE-488.1:

| | Without the adapter's talk address [inherited] | As NI sends it [captured] (§10.5.3) |
|---|---|---|
| Selected device clear of address N | `3f 20+N [60+S] 04` (UNL, LAD N, SDC) | `40+C 3f 20+N [60+S] 04` (MTA first) |
| Trigger (`viAssertTrigger`) | `3f 20+N [60+S] 08` | `40+C 3f 20+N [60+S] 08` |
| Universal device clear | `14` (DCL) | not captured |

Then re-address before the next data transfer **[inherited]**; NI's next
write after a clear is its normal message, which re-addresses anyway. The
instrument discarded pending output on the SDC **[captured]** (§10.1.7).

### 5.9 Serial poll

Two ways.

**A. The dedicated instruction [captured]** (§10.5.4; not yet run by this
project's driver on hardware). `10 01 00 x P S t 00` (P = device primary
address, S = 0x60 | secondary or 0x00, t = timeout code, x = 0x00 or 0x01,
meaning not established). Reply `3a P S sb` (sb = status byte) plus a
status block with id 0x39; a poll that times out (error 0x0a at the
expiry of 7.3) returns the 0x39 status block without the `3a` block
(§10.6.6). This is what NI uses for `viReadSTB`.

**B. The IEEE-488.1 sequence with the framed instructions [inherited]**
(5.3, 5.4, 5.2); its 1-byte read was seen on the bench (3.6), the
sequence as a whole is not recorded as verified:

1. Command bytes: `3f 20+C 18 40+N` (UNL, controller listens, SPE, device
   N talks), C = adapter primary address.
2. Go to standby (0x06).
3. Read (0x0a) with max length 1, EOS disabled (`m` = `e` = 0). The single
   byte is the status byte; bit 6 (0x40) = RQS.
4. Command bytes: `19 5f` (SPD, UNT).

**[captured]** (2.5, §10.4.2) The adapter also polls the device by itself
when SRQ is asserted and reports the status byte in the interrupt push,
after which the device's RQS is already clear: the explicit 0x10 poll
that followed returned the status byte without bit 6.

Adapter's own serial-poll response byte (when the adapter is polled by
another controller): register write bank 1 addr 0x06 (SPMR) = status
byte; bit 6 requests service **[inherited]**.

**Superseded (5.9).** [inherited] "There is no dedicated serial-poll
instruction; use the IEEE-488.1 sequence" -- there is one, 0x10
(§10.5.4). The sequence stays valid as method B.

### 5.10 Parallel poll

**[inherited]** throughout; no parallel poll was captured or run on the
bench.

- Conduct: `07 t 00 00 04 00 00 00` (t = timeout code; only 0xf0 = no
  timeout has been observed). Reply: 8-byte status block then one byte =
  the 8 DIO lines during the poll; ignore the bytes after the result byte.
  Read up to 32 bytes.
- Configure own response: register write AUXMR = 0x60 | cfg (PPR).
- Own ist flag: AUXMR 0x09 (set) / 0x01 (clear).

### 5.11 Stop / abort an in-flight operation

**[inherited]** Control request 0x20 (bmRequestType 0xC0, wValue 0,
wIndex 0, wLength 8). Response: 8-byte status block. The device then
finishes the pending bulk instruction immediately and sends its normal
reply with error code 0x01 and a valid partial count. Its intended use:
when the host-side USB read times out while a long device-side timeout
(e.g. code 0xf0) is running, send it, then read the bulk reply.

**[captured]** (§10.6.7, §10.7.3, §10.8) The request appears nowhere in
the 28 captures of NI's driver, its failed and timed-out instructions and
a `viTerminate` included; those instructions end by themselves with a
normal reply. **[bench]** (8.17) The request was answered on the bench
adapter in its hung state and did not unhang it. Its effect on a pending
instruction has not been seen by this project.

### 5.12 Status query

**[captured]** (§10.3.5) Control request 0x21 with wValue 0x0200
(wLength 8) returns the current 8-byte status block, `21 ss ss 00 cc cc
xx xx`, without disturbing the bus. NI uses it at INTFC open and for the
ATN / SRQ / CIC state attributes.

Waiting for SRQ:

- **[inherited]** Poll this request for SRQI (0x1000), or use the
  interrupt endpoint with the monitor mask (2.5): with a mask set, the
  interrupt IN endpoint delivers one 8-byte status block whenever a
  monitored bit becomes set.
- **[captured]** SRQI was never seen set in a 0x21 reply, including right
  after an SRQ, because the adapter had already polled the device and
  SRQ was released (§10.4.4); whether SRQI ever shows there while SRQ is
  held is not established. NI's SRQ event came from the interrupt push
  (2.5, §10.4.2); its wait-for-SRQ with nothing pending polls ibsta
  through the 12-byte bank-2 0x03 write every 15 ms (§10.4.3), and no
  SRQI was seen there either, nothing being pending.

### 5.13 Bus line status

**[bench] [captured]** Register read (3.4) of bank 1 addr 0x1f (BSR).
Reply `34 vv 00 00 35 01 00 00 04 00 00 00` (3.5). Bits of `vv`
(**[inherited]** names; REN and NDAC seen on the bench, REN, NRFD, NDAC
and ATN in the captures, §10.3.2): 0x01 REN, 0x02 IFC, 0x04 SRQ, 0x08
EOI, 0x10 NRFD, 0x20 NDAC, 0x40 DAV, 0x80 ATN (1 = line asserted). Only
0x1f is needed for line status.

### 5.14 Change the adapter's own address

**[inherited]**; the captures only ever show address 0 (2.6 rows 18-22).

- Primary P: register write `01 0c P`, `02 00 P` (2 writes).
- Secondary S enable: `01 0c 80|S`, `01 08 32`, `02 01 60|S`;
  disable: `01 0c e0`, `01 08 31`, `02 01 00` (3 writes; same as 2.6 rows
  20-22).

### 5.15 Return to local

- **[captured]** (§10.7.4) To send an instrument to local, NI sends the
  GTL command byte to the addressed device, `40+C 3f 20+N 01`, and for
  VI_GPIB_REN_DEASSERT_GTL then REN off (5.6). It writes no register.
  The [inherited] byte order without the adapter's talk address is `3f
  20+N [60+S] 01` (section 6).
- **[inherited]** The original sources give a register write AUXMR = 0x05
  under this heading. It appears in no capture and was not exercised on
  the bench; what it does on the bus has not been seen by this project.
  For "send the instrument to local" use GTL.

**Superseded (5.15).** "Register write AUXMR = 0x05" as the whole of the
operation -- see above.

### 5.16 Find listeners (presence probe)

Two probes, both seen to discriminate a present from an absent address.

**A. The 0x02 instruction [captured]** (§10.6.1; not yet run by this
project's driver on hardware). `02 P S 00` (S = 0x60 | secondary or
0x00), reply status block + `01 00 00 00` for present / `00 00 00 00` for
absent, about 1.8 ms. NI issues it at every session open and, for an
absent address, 50 times at 104 ms intervals (each but the first preceded
by `40 3f 20+N 04`) before opening the session anyway. What it does on
the bus is not visible over USB.

**B. NDAC sampled through BSR [bench]** (IEEE-488.1 acceptor handshake
sampled via 5.13). On the GPIB-USB-HS with a Keithley 2400 at PAD 3,
2026-09-18: BSR read 0x01 (REN only) for empty addresses and had NDAC set
for address 3; the probe over 1..30 returned exactly [3]:

1. 0x0c `3f 20+N` (UNL, LAD N). Error 5 here means the bus is empty; stop
   **[inherited]**.
2. 0x06 (go to standby, ATN false).
3. Register read of BSR (5.13). NDAC (bit 0x20) asserted means a listener
   at N is present; released means none.
4. 0x01 (take control), then 0x0c `3f` (UNL).

Probe B addresses the instrument and so puts it in remote; see 8.18 for
what that led to on the bench.

Other presence signals **[inherited]**, except where tagged:

- Error 5 on a 0x0c: no device on the bus accepted the command byte. Every
  powered device accepts command bytes regardless of its address
  **[captured]** (§10.6.2), so this only says whether the bus is empty,
  not whether address N exists.
- Error 8 on a 0x0d **[captured]** (§10.6.2): no device is currently
  addressed to listen. This discriminates address N but requires sending
  at least one data byte to the instrument.

### 5.17 Pass control (low priority)

**[inherited]** Command bytes `40+N 09` (TAD N, TCT) hand control to
device N; the adapter then ceases to be CIC. Not exercised by either
source, the bench or the captures; how the
adapter reports the transition (CIC clearing in ibsta) is uncertain.

### 5.18 Mapping for a pyvisa-py GPIB session

Two complete mappings exist. The middle column is the framed path this
document was first written around; the right-hand column is what
NI-488.2 puts on the wire for the same VISA call **[captured]**
(section 10). Either column works within itself; rows have not been
mixed on hardware.

| VISA operation | Framed path | As NI-488.2 does it [captured] |
|----------------|-------------|--------------------------------|
| open | attach per 2.8: 26 writes, IFC pulse, REN on, take control, pause (8.18) [bench] | 26 writes, register read, IFC, REN on; bank-2 0x03..0x07 writes; 0x02 probe; no take-control (§10.3.2) |
| write | 0x0c `3f 40+C 20+N [60+S]`; 0x0d with EOI flag 0x08 (send_end) [bench] | one message `03 \| 0c 40+C 3f 20+N [60+S] \| 0d \| 09 bank-2 0x03`; 0x0e with the data on 0x06 from 2049 bytes (§10.5.1, §10.5.2) |
| read | 0x0c `3f 20+C 40+N [60+S]`; 0x06; 0x0a with `m`/`e` from the session's read termination; END -> stop, else loop until count [bench] | one message `03 \| 0c 3f 20+C 40+N [60+S] \| 0a \| 09 bank-2 0x03`, no 0x06; from 1025 bytes 0x0b with the data on 0x88 and an AUXMR 0x55 write behind it (§10.1.1-10.1.5) |
| read_stb | 5.9 method B (includes the 0x06) [inherited] | 5.9 method A, the 0x10 instruction (§10.5.4) |
| clear | 5.8 `3f 20+N [60+S] 04` [inherited] | `40+C 3f 20+N [60+S] 04` (§10.5.3) |
| assert_trigger | 0x0c `3f 20+N [60+S] 08` (GET) [inherited] | `40+C 3f 20+N [60+S] 08` (§10.5.3) |
| send_ifc | 5.5 [bench] | 5.5 (§10.7.1) |
| control_ren | 5.6 plus, for GTL, 0x0c `3f 20+N 01` [inherited] | on an instrument session: the REN write, plus the 0x02 probe for the "address" modes, plus `0c ff 00 fd 11` for the LLO modes, and `40+C 3f 20+N 01` for the GTL modes (§10.7.4); on a board session §10.7.1 |
| control_atn | 5.4 [bench] for 0x01 and `06 00 00 00` | 5.4 with `06 00 00 0a` / `06 01 00 0a` (§10.7.1) |
| gpib_command | 5.3 in 16-byte chunks [inherited] limit | 5.3, session timeout code (§10.7.1) |
| timeout attribute | section 7 code in every 0x0a/0x0c/0x0d; host wait per 7.2 | session code in 0x0a/0x0b/0x0d/0x0e/0x10 and bank-2 0x07; 0xfd in addressing 0x0c (§10.1.9) |
| termination character | `m`/`e` of the read (5.2) | the same, plus byte 5 of every write; setting the attribute writes nothing but bank-2 0x03 := 1 (§10.1.6) |
| SRQ event enable / disable | monitor mask (2.5) [inherited] | nothing but bank-2 0x03 := 1; the push arrives regardless (§10.4.1, §10.4.2) |
| close | 2.9 [bench] | bank-2 0x03 := 1 and 0x04 := 0; 2.9 at process end (§10.3.3) |

Errors on NI's raw paths are in §10.6.5-10.6.7.

**Superseded (5.18).** The single-column table with `read_stb | 5.9
(includes the 0x06)` as the only mapping, and the prose list of NI's
differences that followed it (first batch: "reads above 1024 bytes use
0x0b, a 2050-byte write used 0x0e"); both are folded into the table
above with the settled thresholds.

---

## 6. Addressing sequences (IEEE-488.1, public)

Command byte encodings (all sent with 0x0c):

| Byte | Meaning |
|------|---------|
| 0x01 | GTL go to local |
| 0x04 | SDC selected device clear |
| 0x08 | GET group execute trigger |
| 0x09 | TCT take control (pass control to the addressed talker) |
| 0x11 | LLO local lockout |
| 0x14 | DCL device clear (universal) |
| 0x18 | SPE serial poll enable |
| 0x19 | SPD serial poll disable |
| 0x20 + n | MLA / LAD: listen address n (n = 0..30) |
| 0x3f | UNL unlisten |
| 0x40 + n | MTA / TAD: talk address n (n = 0..30) |
| 0x5f | UNT untalk |
| 0x60 + s | MSA / SAD: secondary address s (s = 0..31), sent immediately after the primary talk/listen byte it qualifies |

With controller primary address C and instrument primary N (secondary S
optional). Both columns are valid IEEE-488.1 and both have addressed a
real instrument; they differ in whether and where the adapter's own talk
address goes:

| Purpose | This project's order | NI's order [captured] (§10.2.3) |
|---------|----------------------|----------------------------------|
| Controller talks, instrument listens (before 0x0d / 0x0e) | `3f 40+C 20+N [60+S]` [bench] | `40+C 3f 20+N [60+S]` (MTA before UNL) |
| Instrument talks, controller listens (before 0x0a / 0x0b) | `3f 20+C 40+N [60+S]` (optionally `5f` first) [bench] | `3f 20+C 40+N [60+S]`, the same |
| Serial poll (5.9 method B) | `3f 20+C 18 40+N [60+S]` ... 0x06 ... read 1 byte ... `19 5f` [inherited] | not used; 0x10 instead |
| Selected device clear | `3f 20+N [60+S] 04` [inherited] | `40+C 3f 20+N [60+S] 04` |
| Trigger | `3f 20+N [60+S] 08` [inherited] | `40+C 3f 20+N [60+S] 08` |
| Go to local | `3f 20+N [60+S] 01` [inherited] | `40+C 3f 20+N 01` |
| Local lockout | `11` [inherited] | `11` alone in a 0x0c (§10.7.4) |
| Pass control | `40+N [60+S] 09` [inherited] | not captured |

The placement of `60+S` directly after the primary it qualifies is the
same in both **[captured]** (§10.2.3, §10.5.3).

The adapter's own address C is the value written to ADR in 2.6 row 18
(0 by default). The adapter must be CIC for any of this: a 0x0c while not
CIC returns error 7 **[captured]** (§10.6.4). If ibsta lacks CIC, pulse
IFC (5.5) **[captured]**; a take-control (5.4) does not help, it returns
error 7 itself while the adapter is not CIC.

**Superseded (6).** A single column of command bytes with NI's order in a
trailing note. "if ibsta lacks CIC, pulse IFC (5.5) or take control
(5.4)" [inherited] -- 0x01 while not CIC returns error 7 (§10.6.4).

---

## 7. Timeouts

### 7.1 Device timeout code byte

Present in the framed instructions 0x0a, 0x0c, 0x0d **[bench]
[captured]**, in the raw-path instructions 0x0b, 0x0e and the serial poll
0x10 **[captured]**, and in the parallel poll 0x07 **[inherited]**. NI
also writes the session's code to bank-2 register 0x07 (§10.2.4). The
code for a requested timeout T (in microseconds) is the smallest row with
T <= limit:

| Limit | Code | Limit | Code |
|-------|------|-------|------|
| 0 (disabled / infinite) | 0xf0 | 30 ms   | 0xf8 |
| 10 us  | 0xf1 | 100 ms  | 0xf9 |
| 30 us  | 0xf2 | 300 ms  | 0xfa |
| 100 us | 0xf3 | 1 s     | 0xfb |
| 300 us | 0xf4 | 3 s     | 0xfc |
| 1 ms   | 0xf5 | 10 s    | 0xfd |
| 3 ms   | 0xf6 | 30 s    | 0xfe |
| 10 ms  | 0xf7 | 100 s   | 0xff |
|        |      | 300 s   | 0x01 |
|        |      | 1000 s  | 0x02 |

For codes 0xf1..0xff the code equals 0xf0 + the NI-488.2 T-code
(T10us = 1 ... T100s = 15). Requests above 1000 s fall back to 0xf0
**[inherited]**.

Evidence for the table: the rows 0xf9 to 0xff, 0x01, 0x02 and 0xf0
**[captured]** (§10.1.9): NI-VISA 22.5 maps VI_ATTR_TMO_VALUE 100 ms ->
0xf9, 300 -> 0xfa, 1000 -> 0xfb, 2000 and 3000 -> 0xfc, 10 s -> 0xfd, 20
s and 30 s -> 0xfe, 100 s -> 0xff, 300 s -> 0x01, 1000 s -> 0x02,
infinite -> 0xf0. The rows 0xf1 to 0xf8 **[inherited]**. The 0x02 code
for 1000 s has also been bench-confirmed on a USB-B according to the
sources **[inherited]**. The code NI programs at session open, before the
application sets a timeout, is 0xfb.

The limits in the table are **nominal**. What the GPIB-USB-HS actually
waits under each code was measured and is tabulated in 7.3
**[captured]**; one code (0xfa) expires before its nominal value.

What the code bounds -- the whole instruction, or an interval within it
-- is **not established**. 20480-byte chunks that took 3.93 to 4.00 s
each completed with error 0 under code 0xfc (readtimeout_long.pcap), in a
session whose VISA timeout was 2000 ms: longer than what the application
asked for and than the nominal 3 s, but shorter than the 4.194 s the
adapter really waits under 0xfc (7.3). So the captures show that a
transfer may outlast the nominal value; they do not show one outlasting
the measured expiry, and cannot tell a per-instruction bound from a
per-byte one.

**Superseded (7.1).** "Present in 0x0a, 0x0c, 0x0d (and 0x07)" -- 0x0b,
0x0e and 0x10 carry it too. "NI's own driver is reported to send 0xff
[for 1000 s]" [inherited] -- wrong; NI sends 0x02 (timeouts.pcap
10.7994). "The code does not bound a whole instruction: 20480-byte chunks
that took 4.0 s each completed with error 0 under code 0xfc" -- written
before 7.3 was measured, when 0xfc was taken to mean 3 s; 4.0 s is inside
the measured 4.194 s, so the conclusion does not follow.

### 7.2 Host-side USB wait

The device answers an instruction that carries a timeout code only after
the operation completes or the adapter's own wait expires, so the host's
wait on the bulk IN transfer must exceed that wait by a margin. The
adapter's wait is not the nominal limit T of 7.1 but the measured expiry
E of 7.3 **[captured]**.

| Transfer | Host wait | Evidence |
|----------|-----------|----------|
| bulk IN on 0x84 after 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x10 with a finite code; and the 0x88 transfer of a 0x0b | longer than E of the code sent; recommended E + 2 s, summed over the timed blocks of the message (rules below) | [captured] (7.3) |
| the same with code 0xf0 (no timeout) | a long finite wait chosen by the application; on expiry send the stop request (5.11) and read the reply | [inherited] |
| bulk OUT of a message carrying write data (0x0d), and the 0x06 transfer of a 0x0e | follows the device timeout like the reply: OUT transfers are paced by the bus (below) | [captured] (§10.5.2) |
| bulk OUT of any other message; bulk IN after 0x01, 0x06, 0x07, 0x08, 0x09, 0x0f | 1 s minimum | [inherited] |
| control requests | 1 s (100 ms per readiness-query attempt) | [inherited] |

Rules for the first row **[captured]** (7.3, §10.1.8):

- The host wait for the reply to any instruction that carries a timeout
  code -- and for the 0x88 transfer of a 0x0b -- **must be longer than
  the measured expiry E of that code in 7.3**: more than 0.133 s for
  0xf9, 0.264 s for 0xfa, 1.050 s for 0xfb, 4.197 s for 0xfc, 16.779 s
  for 0xfd, 33.556 s for 0xfe. The code that counts is the one put on
  the wire, not the timeout the application asked for (2000 ms goes out
  as 0xfc and runs 4.196 s).
- Recommended: E + 2 s. The reply was never more than 1.9 ms later than
  the power of two of 7.3 in twelve timed-out instructions, so a fixed
  margin is enough; nothing observed scales with T.
- A message may carry more than one timed block: NI's read messages are
  a 0x0c with its own code (0xfd, §10.1.9) followed by the 0x0a / 0x0b
  with the session's code. Each block can run to its own expiry, so the
  wait for the message's reply must exceed the sum of the E of its timed
  blocks. Inference from the message structure: no capture has two
  blocks of one message both expiring, or a 0x0c expiring at all.
- For a code not measured in 7.3, take E as the larger candidate of
  7.3's inference column (the smallest power of two in microseconds not
  below the nominal limit), which no measured code exceeded.
- E + 2 s covers an instruction during which nothing moves. Whether an
  instruction during which data keeps moving can run past E is the open
  question of 7.1; until it is settled a host that expects long
  transfers under a short code has no captured figure to size its wait
  by.

The 0x88 data transfer of a 0x0b read completes 0.4-0.5 ms before its
0x84 reply, and for a zero-byte result completes with zero bytes, so both
transfers must be waited for **[captured]** (§10.1.3). OUT transfers are
paced by the bus as well: the 2049 data bytes of a 0x0e took 368 ms to
complete on 0x06 and the tail of a 2080-byte 0x0d message 103 ms on 0x02
**[captured]** (§10.5.2), so a fixed 1 s is too short for a long write to
a slow listener.

**Superseded (7.2).** [inherited] First row "bulk IN after 0x0a, 0x0c,
0x0d with device timeout T > 0: T + max(2 s, 0.5 × T)", and "Reads that
may legitimately wait on a slow instrument should use the upper end of
the margin (T + 50 %) so that a device-side timeout is reported through
the status block rather than as a USB error." Replaced by E + 2 s: for
code 0xfd E = 16.78 s while T + max(2 s, 0.5 × T) = 15 s, so a host using
the old row gives up 1.78 s before the adapter would have ended the
instruction itself with error 0x0a, and is left with an instruction
still pending, for which the only documented recourse is the stop
request (5.11). For the other five measured codes the old row happens to
exceed E (2.1 s against 0.132, 2.3 against 0.264, 3 against 1.05, 5
against 4.20, 45 against 33.56). Also superseded: "bulk OUT of any
instruction ... 1 s minimum" for messages that carry write data
(§10.5.2).

### 7.3 Measured expiry per code (GPIB-USB-HS, observed 2026-09-19)

How long the adapter waits before it ends an instruction by itself with
error 0x0a. Timed on the wire from the submission of the OUT message
that carries the instruction to the completion of its reply on 0x84,
with NI-488.2 driving GPIB-USB-HS 013CC9DF and either nothing to read
or nobody at the address. Packets in §10.1.8. **[captured]** throughout;
the second table of predictions at the end is inference and is labelled
so.

| VISA timeout asked | Code sent | Nominal (7.1) | Measured expiry (s) | Instruction | Capture |
|--------------------|-----------|---------------|---------------------|-------------|---------|
| 100 ms | 0xf9 | 100 ms | 0.132272 | 0x0a | timeout_expiry |
| 300 ms | 0xfa | 300 ms | 0.263541 | 0x0a | timeout_expiry |
| 1000 ms | 0xfb | 1 s | 1.049837 | 0x0a | timeout_expiry |
| 3000 ms | 0xfc | 3 s | 4.195609 | 0x0a | timeout_expiry |
| 3000 ms | 0xfc | 3 s | 4.195640 | 0x0a | partial |
| 3000 ms (INTFC session) | 0xfc | 3 s | 4.195316 | 0x0a alone, no 0x0c before it | board_io |
| 2000 ms | 0xfc | 3 s | 4.196156 (0x88 ends at 4.195673) | 0x0b | nolistener |
| 2000 ms | 0xfc | 3 s | 4.195943 (0x88 ends at 4.195481) | 0x0b | raw_errors |
| 2000 ms | 0xfc | 3 s | 4.195767 | 0x10 | raw_errors |
| 10 000 ms | 0xfd | 10 s | 16.778423 | 0x0a | timeout_expiry |
| 20 000 ms | 0xfe | 30 s | 33.555345 | 0x0a | eos |
| 30 000 ms | 0xfe | 30 s | 33.555258 | 0x0a | timeout_expiry |
| any other | 0xf1..0xf8, 0xff, 0x01, 0x02 | | not measured | | |

Every figure is a power of two in microseconds plus about a
millisecond:

| Code | Power of two | Measured minus it | Measured against nominal |
|------|--------------|-------------------|--------------------------|
| 0xf9 | 2^17 us = 0.131072 s | +1.20 ms | 32 % longer |
| 0xfa | 2^18 us = 0.262144 s | +1.40 ms | **12 % shorter** (36.5 ms early) |
| 0xfb | 2^20 us = 1.048576 s | +1.26 ms | 5 % longer |
| 0xfc | 2^22 us = 4.194304 s | +1.01 to +1.85 ms (six cases) | 40 % longer |
| 0xfd | 2^24 us = 16.777216 s | +1.21 ms | 68 % longer |
| 0xfe | 2^25 us = 33.554432 s | +0.83 and +0.91 ms | 12 % longer |

- The excess is 0.8-1.9 ms at 0.13 s and at 33.6 s alike, so it is a
  fixed cost (USB turnaround, the 0x0c addressing that precedes the read
  in the same message, the reply's other blocks), not a fraction of the
  timeout. The one 0x0a sent without a 0x0c in front (board_io) has the
  smallest excess under 0xfc, 1.01 ms. The parts cannot be separated on
  the wire.
- **Code 0xfa is the only measured code that expires before its nominal
  value**: 0.2635 s for a nominal 300 ms. An application that asks NI
  for 300 ms gets its timeout error after 0.264 s
  (timeout_expiry.stdout.txt). The other five run longer than nominal,
  0xfd by the most (16.78 s for 10 s).
- The expiry follows the code, not the timeout asked for: 2000 and 3000
  ms both give 4.196 s, 20 000 and 30 000 ms both 33.555 s. It is the
  same for 0x0a, 0x0b and 0x10 under 0xfc (the only code timed with more
  than one instruction).
- No one rounding of the nominal value yields all six exponents. "The
  smallest power of two not below nominal" gives 17, 19, 20, 22, 24, 25
  and is wrong for 0xfa (it would be 0.524 s); "the power of two nearest
  nominal on a logarithmic scale" gives 17, 18, 20, 22, 23, 25 and is
  wrong for 0xfd (it would be 8.39 s); nearest on a linear scale is
  wrong for 0xfc. The exponent per code is a fact of the table, not
  something to compute.
- Not measured: codes 0xf1-0xf8, 0xff, 0x01 and 0x02 (0xf0 has no expiry
  to measure); the expiry of a 0x0c, 0x0d or 0x0e under any code -- no
  write or command instruction timed out in any capture; any adapter
  other than this GPIB-USB-HS; whether the expiry restarts with each byte
  handshaken, i.e. what the code bounds (7.1: the 4.0 s chunks under
  0xfc ended inside that code's 4.194 s, so they settle nothing).

**A second unit expires at other times [bench].** GPIB-USB-HS 01CEE482
(bcdDevice byte 0x65 like the captured unit), driven by this project's
messages (a 0x0c with the same code, then a 0x0a `0a 14 0a <code> <count>
00 00 09 02 00 01 0a 51 01 0a 55 00 00 00 04 00 00 00`), on a Mac over
libusb, nothing pending at the Keithley 2400 (2026-09-21). Timed from the
submission of the 0x0a to the completion of its reply; wire logs in
`docs/notes/bench/log_tmo*_hex.txt` (not tracked):

| Code | Count | 01CEE482 expiry (s) | 013CC9DF expiry (s), table above | Ratio |
|------|-------|---------------------|----------------------------------|-------|
| 0xf9 | 20480 | 0.127 (session total; the wire was not logged) | 0.132 | 0.96 |
| 0xfa | 20480 | 0.375 | 0.264 | 1.42 |
| 0xfb | 20480 | 1.250 | 1.050 | 1.19 |
| 0xfb | 1 | 1.250 | | |
| 0xfc | 20480 | 3.750 | 4.196 | 0.89 |
| 0xfd | 20480 | 20.000 | 16.778 | 1.19 |
| 0xfd | 10 | about 20.0 (the host gave up at 18.79 s; the reply, error 0x0a, was collected 1.01 s after the stop request, 20.0 s after the 0x0a) | | |
| 0xfe | 20480 | 41.250 | 33.555 | 1.23 |

- The figures are exact to the millisecond across repeats (0.375, 1.250,
  3.750, 20.000, 41.250 s) and do not depend on the count (1, 10 and
  20480 agree under 0xfb and 0xfd). They are not powers of two in
  microseconds. Every one is 1.25 times a round figure: 0.1, 0.3, 1, 3,
  16 and 33 s.
- Not established: whether the difference is the unit, its firmware, or
  the message (this driver's 0x0a differs from NI's in the bytes behind
  the code and in the block that follows it; the same message was not
  sent to 013CC9DF). Until it is, **a host wait must outlast both
  tables**: the wait derived from the 013CC9DF figures plus 2 s (18.78 s
  for 0xfd, 35.56 s for 0xfe) is shorter than 01CEE482's own expiry
  (20.0 s, 41.25 s). On the bench the host reached its wait first under
  0xfd, sent the stop request 0x20, and the adapter's own error-0x0a
  reply arrived 1.0 s later, at its usual 20.0 s; the read was reported
  as an I/O error instead of a timeout.

Inference, not measurement -- what a power of two in microseconds would
be for the unmeasured codes, under the two rules that each fit five of
the six measured codes. Where the rules agree the prediction is the
firmer; where they differ both are given and a host wait should assume
the larger:

| Code | Nominal | Smallest power of two not below nominal | Nearest power of two (log scale) |
|------|---------|------------------------------------------|----------------------------------|
| 0xf1 | 10 us | 2^4 = 16 us | 2^3 = 8 us |
| 0xf2 | 30 us | 2^5 = 32 us | same |
| 0xf3 | 100 us | 2^7 = 128 us | same |
| 0xf4 | 300 us | 2^9 = 512 us | 2^8 = 256 us |
| 0xf5 | 1 ms | 2^10 = 1.024 ms | same |
| 0xf6 | 3 ms | 2^12 = 4.096 ms | same |
| 0xf7 | 10 ms | 2^14 = 16.384 ms | 2^13 = 8.192 ms |
| 0xf8 | 30 ms | 2^15 = 32.768 ms | same |
| 0xff | 100 s | 2^27 = 134.217728 s | same |
| 0x01 | 300 s | 2^29 = 536.870912 s | 2^28 = 268.435456 s |
| 0x02 | 1000 s | 2^30 = 1073.741824 s | same |

Below about a millisecond the fixed cost above, not the code, would set
the time to the reply. Whether 0x01 and 0x02, which break the 0xf0 + n
numbering, follow the pattern at all is unknown.

---

## 8. Known pitfalls

1. **Padding and termination.** Every instruction block must be
   zero-padded to a 4-byte boundary and every message must end with
   `04 00 00 00` **[bench] [captured]**. A message without the
   termination block gets no reply **[inherited]**.
2. **One message, one reply.** Never queue a second message before the
   reply to the first has been read **[inherited]**; NI never does
   **[captured]** (3.1). The data of a 0x0e on 0x06 and the IN transfer on
   0x88 for a 0x0b are part of the same exchange and are started before
   the reply is read (§10.6.7). On a reply size mismatch (e.g. 12
   expected, other received) dump the bytes; the pipes are then out of
   step and the safest recovery is the stop request (5.11), a drain read,
   and a fresh register initialisation **[inherited]** -- a recovery NI
   was never seen to need or use (§10.6.7).
3. **EOS bytes on reads.** When the session disables read termination,
   sending `00 00` is safe **[bench]**. The [inherited] warning "nonzero
   EOS mode or character with REOS clear -> error 4 on every read" was
   not reproduced: NI sends mode 0x00 with a nonzero character on every
   unterminated read and never gets error 4 **[captured]** (§10.1.6;
   adapter initialised with AUXRA 0x99). Whether it holds under AUXRA
   0x81 is not established (4.3).
4. **16-byte command chunks.** A 0x0c instruction carrying 17 or more
   command bytes fails on the USB-B (error 4); keep the chunk limit on
   all models **[inherited]**.
5. **Count encodings are negative.** Instruction counts are -(length);
   reply counts are (transferred - requested). Getting the sign wrong
   yields a 65535-byte transfer request.
6. **Read reply buffer.** Size the receive buffer from the wire layout:
   ceil(N/30) 32-byte blocks (or ceil(N/15) 16-byte blocks) plus 28 bytes
   for what follows the data -- 16 were seen on the bench, 28 in the
   captures, so size for the longer and parse both (5.2) -- the larger of
   the two rounded up to the endpoint's max packet size; a smaller
   request is truncated by libusb with an overflow error. **[captured]**
   (§10.1.5): when the 0x0a is batched behind other blocks, add their
   replies plus 16 bytes of `11 00 00 00` padding; a 0x0b read needs a
   buffer of N rounded up to an even number on 0x88 (§10.1.3) and 56
   bytes on 0x84 for NI's five-block message.
7. **ibsta is big-endian, counts are little-endian** inside the same
   8-byte status block.
8. **Readiness poll.** Byte 0 of every control response must echo
   bRequest; a fresh HS may report not-ready (bytes 6, 7, 10 all zero) for
   several 100 ms polls. The HS+ returns 16 bytes to the serial-number
   query where the HS returns 5.
9. **HS+.** Claim interface 0 only; issue the three extra control
   requests (2.4) after the readiness poll. Its endpoints differ (1.2).
10. **USB-B.** Needs a firmware upload before it enumerates as 0x702a;
    the serial number comes from bank-3 register reads, not a control
    request; interrupt IN is 0x84.
11. **Interrupt endpoint.** Optional. Operation without it has been
    observed to be reliable **[inherited]**; if it is used, read at least
    wMaxPacketSize per transfer (NI arms 64 bytes **[captured]**). Arming
    it with the monitor mask (2.5) is the [inherited] procedure; NI's
    pushes came with no mask request in any capture, but whether a fresh
    adapter pushes without one is not established (2.5).
12. **Take control on an empty bus** can return error 5; treat as
    harmless at initialisation.
13. **Error 2 on read** means ATN is still asserted; send 0x06 first.
    Error 3 means the addressing command bytes were not sent (or were
    rejected); resend them. Both **[inherited]**: neither code has been
    seen by this project, and a 0x0a or 0x0b placed directly after the
    addressing 0x0c in the same message did not return error 2 in any of
    103 cases **[captured]** (section 5, ATN rule). Error 7 means the
    adapter is not CIC **[captured]** (§10.6.4).
14. **Register-write reply** is exactly 16 bytes and byte 8 must equal the
    number of writes sent; a smaller value means the device stopped at a
    bad (bank, addr) pair.
15. **Unknown registers.** Bank 3 addr 0x10, bank 2 addrs 0x00-0x02 and
    bank 1 addr 0x0f are written with fixed values at init because NI's
    driver does **[bench] [captured]**; their meaning is unknown. Do not
    omit them. **[captured]** (§10.2.4): bank 2 addrs 0x03-0x07 are
    written per session (0x03 := 1 constantly, 0x04 := 1/0 open/close,
    0x05 := PAD, 0x06 := SAD byte, 0x07 := timeout code); the instructions
    carry the same information themselves, and whether any of these writes
    is required for the instructions to work is not established; the
    framed paths ran on the bench without them (2.8).
16. Absence of serial/parallel poll or SRQ handling in a known working
    implementation is not evidence those instructions fail.
17. **A hung adapter** **[bench]**. Observed on GPIB-USB-HS 01CEE482,
    2026-09-18: the adapter answered every control request normally
    (serial number, readiness, status and stop), accepted bulk OUT
    messages on 0x02 until about 4 KB had been queued (and about 1 KB on
    0x06), then NAKed, and never sent a byte on 0x84, 0x88 or 0x81.
    Nothing on the USB side cleared it: not the stop request, the monitor
    mask, clear-halt, SET_CONFIGURATION 0/1, nor a USB bus reset (which
    empties the endpoint FIFOs but does not restart the firmware).
    Unplugging and replugging the adapter fixed it at once. A driver
    should treat "the initialisation message was accepted but no reply
    arrived within 2 s, nor after a stop request" as this condition and
    tell the user to power-cycle the adapter.
18. **Settle after IFC / REN before the first addressed command**
    **[bench]**. Observed
    on GPIB-USB-HS 01CEE482 with a Keithley 2400 at PAD 3, 2026-09-18: with
    the sequence attach -> presence probe (5.16, which addresses the
    instrument and puts it in remote) -> shutdown (2.9; the chip reset drops
    REN, the instrument returns to local) -> attach (IFC, REN, take control)
    -> `3f 40 23`, `0d ... *IDN?` within about 1 ms, the write instruction
    reported all 6 bytes transferred (the instrument's interface handshakes
    in hardware) but the instrument never parsed them: the following read
    timed out and the instrument logged `-420,"Query UNTERMINATED"`. It
    reproduced within 1-4 iterations; without the probe (instrument never
    in remote) it did not reproduce in 20. A pause of 20 ms after take
    control, or after the shutdown, was already enough (10/10 each); the
    driver waits 100 ms after the attach's take control and after a public
    IFC pulse. The adapter also hung once (8.17) under the application at
    exactly this point -- the first `0x0c` after such an attach never
    answered even after the device timeout -- so a stalled handshake with an
    instrument in this state may be what wedges the firmware; with the pause
    in place the sequence ran 10 x (attach, probe, close, attach, `*IDN?`,
    50 x `:OUTP?`, close) with no gap and 5 more cycles through pyvisa
    without incident.

**Superseded (8).** Item 3 as a flat rule ("-> error 4 on every read");
item 6 "plus the 28-byte trailer" with no word that 16 bytes is what the
bench saw; item 11 "arm it with the monitor mask" as a requirement; item
13's count of 83 cases (first batch; 103 in all 28 captures).

---

## 9. Provenance

Sources were downloaded on 2026-09-18 into a scratch directory outside the
repository and read for protocol facts only. No code, comments, identifiers
or paraphrased comment text from them appears in this document. Wire
formats are described from the device's point of view, not the programs'.

| File | URL | SHA-256 |
|------|-----|---------|
| ni_usb_gpib.c (linux-gpib kernel driver, GPL-2.0) | https://sourceforge.net/p/linux-gpib/code/HEAD/tree/trunk/linux-gpib-kernel/drivers/gpib/ni_usb/ni_usb_gpib.c?format=raw | ad0c1fac57e6dab9f13e470da783f2d52f7f82c5e66efd90aa54d884b89b41e5 |
| ni_usb_gpib.h (linux-gpib, GPL-2.0) | https://sourceforge.net/p/linux-gpib/code/HEAD/tree/trunk/linux-gpib-kernel/drivers/gpib/ni_usb/ni_usb_gpib.h?format=raw | e53f0592047aa239652ad9e8f5e9c14fd506922920bd4e01981508db94d818b9 |
| tnt4882_registers.h (linux-gpib, GPL-2.0) | https://sourceforge.net/p/linux-gpib/code/HEAD/tree/trunk/linux-gpib-kernel/drivers/gpib/include/tnt4882_registers.h?format=raw | abe56d519a161b40a5b88a825a375c5728bc02ae4be9c1f35ac72356c3034563 |
| nec7210_registers.h (linux-gpib, GPL-2.0) | https://sourceforge.net/p/linux-gpib/code/HEAD/tree/trunk/linux-gpib-kernel/drivers/gpib/include/nec7210_registers.h?format=raw | 8510f046d63d2e22646647c203f7eb2cfc23e0142c276fbb2d592cc48df4a6e2 |
| controller.py (ni-gpib-usb-hs user-space driver, GPL-2.0) | https://raw.githubusercontent.com/embeddedci-com/ni-gpib-usb-hs/main/ni_gpib_usb_hs/controller.py | 2af24c179860e277269c5e711a16538cc04bc31af080e27b414e036cfbb151b4 |
| gpib_user.h (linux-gpib public API header; used only to confirm NI-488.2 ibsta bit numbers, EOS flag values and command bytes) | https://sourceforge.net/p/linux-gpib/code/HEAD/tree/trunk/linux-gpib-kernel/drivers/gpib/include/gpib_user.h?format=raw | 8680bb597a799f19ab6d96551100aa2705954eab6fd1f1e7645eb2b28def1dbc |
| TNT4882 Programmer Reference Manual, NI part 370872A-01 (July 1995) | https://docs-be.ni.com/bundle/tnt4882-programmer-reference/raw/resource/enus/370872a.pdf | 11a790cc4050cb330c0a1654dca5e49ccb9d548ed5b12ab3363d9e161f79d9fb |

USB captures of NI's driver: statements marked "Observed 2026-09-19" and
all of section 10 come from USBPcap recordings of NI-488.2 / NI-VISA 22.5
on Windows 10 driving GPIB-USB-HS serial 013CC9DF with a Keithley 2420 at
primary address 24, one VISA operation per scenario, kept in
`docs/design/captures/ni_usb_gpib_2026-09-19/` with the harness
(`scenario.py`, `capture.ps1`), the decoder (`usbpcap_dump.py`), each
scenario's VISA timeline (`<name>.stdout.txt`) and `SHA256SUMS` over the
28 pcaps (22 from a first batch; read_thresholds, write_thresholds,
raw_errors, sad_poll and ren_device from a second batch the same day;
timeout_expiry, the source of 7.3, after those).
They record the behaviour of our own adapter under the vendor
driver; no program source was consulted for them.

Editorial pass of 2026-09-19 (after the three capture rounds): until
then, where the captures contradicted the sources the observed behaviour
was stated next to the original text, never in its place, and the
document ended up saying incompatible things. The pass added the evidence
tags [captured] / [bench] / [inherited] ("How to read this document"),
rewrote each normative statement so that it is true as it stands, moved
every contradicted statement into a "Superseded" note at the end of its
subsection rather than deleting it, and collected the open questions in
section 11. It added no protocol fact. It was made by an agent with no
access to the original sources or to any driver source, from this
document, the clean-room record, a reviewer's list of contradictions and
the captures; where two statements conflicted the packets were re-read
with `usbpcap_dump.py`, and the counts quoted with a tag (e.g. 25 / 2 / 1
shutdown replies, 103 reads behind a 0x0c, 946 messages) come from that
re-reading of all 28 pcaps.

Bench observations: statements marked "observed on GPIB-USB-HS 01CEE482"
were recorded on 2026-09-18 from a GPIB-USB-HS (USB 3923:709b, bcdDevice
0x101, serial 01CEE482) on macOS through pyusb/libusb, with a Keithley
2400 at primary address 3 on the bus, using the clean-room driver in
`resistamet_gui/gpib_usb/` and a byte-level trace of every USB transfer.
They correct the sources in these places: the read-reply trailer is 16
bytes (5.2, 3.5, 3.6); the count field is meaningful only after data
operations and bytes 6-7 are not zero (4.1); the termination block follows
the 0x35 block of a register-read reply (3.5); the readiness reply's
informational bytes vary by unit (2.3); the 5.16 probe works; the hung
state of 8.17 exists; and instruments need a pause after IFC/REN (8.18).

Section-level attribution: sections 1-5, 7 and 8 -- linux-gpib
ni_usb_gpib.c / ni_usb_gpib.h and ni-gpib-usb-hs controller.py; register
names and bit meanings in 2.6, 2.7, 3.4, 5.13 -- linux-gpib
tnt4882_registers.h / nec7210_registers.h; section 6, the ibsta table and
the presence probe of 5.16 -- IEEE-488.1 and NI-488.2 public standards.

The NI TNT4882 Programmer Reference Manual confirms, for one-chip /
Turbo+7210 mode, the register offsets and bit assignments used in 2.6,
2.7 and 3.4 (AUXMR, ADR, ADMR, HSSEL, IMR0 with bit 7 = 1, IMR3, KEYREG
with MSTD = bit 5 and SWAP = bit 6, CMDR 0x22/0x04/0x08/0x10, BSR bit
order, the AUXRA / AUXRB / AUXRG / AUXRI hidden registers, aux commands
0x00-0x0f, 0x50, 0x51, 0x55, the T1 table, and the SWAP-dependent 9914
offsets of note (a)). Being a Talker/Listener manual it does not list the
controller-side aux commands 0x10-0x1f or CMDR 0x02/0x03, which follow the
NEC uPD7210 / NAT4882 conventions; offset 0x0f is absent from its register
map.

---

## 10. Observed with NI-488.2 driving the adapter (captures of 2026-09-19)

Source: USBPcap recordings of National Instruments' own NI-488.2 / NI-VISA
22.5 stack on a Windows 10 PC driving a GPIB-USB-HS (USB 3923:709b, serial
013CC9DF, USB device address 2) connected to a Keithley 2420 at primary
address 24, one VISA operation per scenario. Files, harness, per-scenario
VISA timelines and SHA-256 sums are in
`docs/design/captures/ni_usb_gpib_2026-09-19/` (28 pcaps: 22 in a first
batch; the same day, five aimed at what the first batch left open --
read_thresholds, write_thresholds, raw_errors, sad_poll, ren_device; then
timeout_expiry, which times the adapter's wait under six timeout codes;
see its README). Provenance of each fact below is `(name.pcap t DIR n B)`:
pcap, seconds from the first packet of that pcap, direction (OUT = bulk OUT
0x02, IN84 = bulk IN 0x84, IN88 = bulk IN 0x88, OUT06 = bulk OUT 0x06,
INTR = interrupt IN 0x81, CTRL = control request), payload length.
Decode with `python usbpcap_dump.py <name>.pcap 2 --full`.

Everything in this section was recorded with the adapter initialised by
NI's 26-write sequence (10.3.1), which differs from 2.6 in one value; where
a fact may depend on that (EOS handling, 10.1.6), it is said so.

Evidence level: everything in this section is **[captured]** and is not
tagged line by line. None of it has yet been run by this project's
driver on hardware ("How to read this document"). Several subsections
were written after the first batch of 22 captures and extended after the
later six; where the later captures settled what the earlier text left
open, the settled statement now comes first and the earlier wording
stands in a Superseded note. Counts of packets are for all 28 captures
unless they say otherwise.

### 10.1 Read path

#### 10.1.1 Two read instructions

NI uses two read instructions, chosen by the requested count alone:
**requested count <= 1024 -> 0x0a, 16-bit count, data framed on 0x84;
requested count >= 1025 -> 0x0b, 4-byte count, data raw on 0x88.**

| Requested count | Instruction | Data returned on | Observed counts |
|-----------------|-------------|------------------|-----------------|
| 1 .. 1024 | 0x0a (5.2) | bulk IN 0x84, framed in 0x36 / 0x37 blocks | 1, 2, 8, 10, 15, 16, 30, 31, 32, 60, 63, 64, 65, 100, 127, 128, 200, 255, 256, 511, 512, 1023, 1024 (counts.pcap 0.4288 .. 12.8188; partial.pcap; eosmodes.pcap; eos.pcap 1.5455; timeout_expiry.pcap) |
| 1025 .. 20480 | 0x0b (new) | bulk IN 0x88, raw, unframed, padded to an even length (10.1.3) | 1025, 1500, 2000, 2047, 2048, 2049, 3000, 4095, 4096 (read_thresholds.pcap, below); 4096 (counts.pcap 13.4323); 20480 (counts.pcap 14.0488; every 0x0b in idn, clear, eos, trac, srq, timeouts, nolistener, two_sessions, longwrite, readtimeout_long, terminate) |

20480 is pyvisa's default chunk size, so every `viRead` issued by
pyvisa's `read()` used 0x0b. No count above 20480 was captured.

The boundary, from read_thresholds.pcap: `*IDN?` followed by `viRead` with
each of nine counts; every one was sent as 0x0b in the 40-byte message of
10.1.2 (`m e t` = `00 0a fc`), the 82 bytes came raw on bulk IN 0x88 and the
56-byte reply on 0x84:

| Count | `c0..c3` sent | Reply count (82 - count) | OUT 40 B at | IN88 82 B at |
|-------|---------------|--------------------------|-------------|--------------|
| 1025 | `ff fb ff ff` | `51 fc ff ff` | 0.3160 | 0.3259 |
| 1500 | `24 fa ff ff` | `76 fa ff ff` | 0.8335 | 0.8430 |
| 2000 | `30 f8 ff ff` | `82 f8 ff ff` | 1.3490 | 1.3586 |
| 2047 | `01 f8 ff ff` | `53 f8 ff ff` | 1.8662 | 1.8751 |
| 2048 | `00 f8 ff ff` | `52 f8 ff ff` | 2.3822 | 2.3917 |
| 2049 | `ff f7 ff ff` | `51 f8 ff ff` | 2.8991 | 2.9084 |
| 3000 | `48 f4 ff ff` | `9a f4 ff ff` | 3.4155 | 3.4249 |
| 4095 | `01 f0 ff ff` | `53 f0 ff ff` | 3.9324 | 3.9422 |
| 4096 | `00 f0 ff ff` | `52 f0 ff ff` | 4.4493 | 4.4587 |

With 1024 the last 0x0a (counts.pcap 12.8188 OUT 32 B, `0a 00 0a fc 00 fc
00 00`) and 1025 the first 0x0b, the rule at the head of this subsection
follows. Only the requested count decides: all nine reads returned the
same 82 bytes.
Nothing changes at 2048 / 2049 (the write boundary, 10.5.2) or at 4096.
In each of these nine reads the host submitted its IN transfer on 0x88
first and the one on 0x84 second, both within 0.1 ms of the OUT and
before any data had come back (0.3160 OUT, 0.3161 IN88, 0.3161 IN84).

**Superseded (10.1.1).** First-batch table row "4096 .. 20480 -> 0x0b" and
"The switch lies between 1024 and 4096; no count in between was
captured." Replaced by the 1024 / 1025 boundary from read_thresholds.pcap.
The rule was worded "32-bit count"; the width of the field is open
(10.1.2).

#### 10.1.2 The 0x0b instruction

```
0b m e t c0 c1 c2 c3
```
- `m`, `e`: EOS mode and character, same encoding as 0x0a (5.2). Observed
  `00 0a` with the termination character disabled (idn.pcap 0.5160 OUT
  40 B) and `14 0a` with `read_termination = '\n'` (eos.pcap 0.5172 OUT
  40 B).
- `t`: timeout code (section 7; map in 10.1.9).
- `c0..c3`: the negative count as a 32-bit little-endian two's complement:
  `00 b0 ff ff` = -20480 (idn.pcap 0.5160), `00 f0 ff ff` = -4096
  (counts.pcap 13.4323). Uncertain whether the field is truly 32 bits or a
  16-bit count followed by `ff ff`: no count above 0xffff was captured. The
  reply count (below) is 32 bits, which favours the 32-bit reading.
  read_thresholds.pcap adds nine counts (10.1.1), none above 0xffff, so
  this stays open.

NI always sends 0x0b inside this 40-byte message:
```
03 00 00 00                        status snapshot (10.2.2)
0c fd 00 fd 3f 20 58 00            UNL, MLA 0, TAD 24 (addressing, 10.2.3)
0b 00 0a fe 00 b0 ff ff            the read
09 01 00 01 0a 55 00 00            AUXMR 0x55, clear END
09 01 00 02 03 01 00 00            bank 2 addr 0x03 := 1 (10.2.5)
04 00 00 00
```
(idn.pcap 0.5160 OUT 40 B.) There is no 0x06 (go to standby) between the
0x0c and the 0x0b; the 0x0c reply shows ATN set (ibsta 0x0074) and the
0x0b reply shows it clear (0x2064), so the 0x0b instruction itself releases
ATN before reading (idn.pcap 0.5259 IN84 56 B). The AUXMR 0x51 (holdoff
immediately) write of 5.2 is not sent; only 0x55, after the read.

#### 10.1.3 The 0x0b reply

The data arrives first on bulk IN 0x88 as one transfer, no framing, no
termination block, of the bytes read **rounded up to an even length**: 82
bytes for the 2420's `*IDN?` answer (idn.pcap 0.5254 IN88 82 B), 2 bytes
for `1\n` (srq.pcap 2.5390 IN88 2 B), 20480 and 328 bytes for the chunks
of a 61 768-byte message (trac.pcap 4.5340, 12.4998), but **6 bytes `31 31
30 33 0a 00` for the 5-byte `1103\n`** (trac.pcap 13.0075 IN88 6 B) whose
0x0b reply count is `05 b0 ff ff` = 5 - 20480 (13.0079). The one padding
byte observed was 0x00; the count in the 0x0b reply, not the 0x88
transfer length, gives the number of bytes read. When nothing was read the
0x88 transfer completes with zero bytes (nolistener.pcap 9.8121 IN88 0 B,
the read that timed out). 0.4-0.5 ms after the 0x88 completion the 56-byte
reply arrives on 0x84:

```
03 00 28 00 00 00 ff ff              0x03 status (10.2.2)
0c 00 74 00 00 00 ff ff              0x0c status: ibsta REM|CIC|ATN|LACS, error 0, count 0
0b 20 64 00 52 b0 ff ff e0 00 00 00  0x0b status + 4-byte tail
09 00 64 00 52 b0 ff ff 01 00 00 00  register-write status (AUXMR 0x55), 1 write
09 00 64 00 52 b0 ff ff 01 00 00 00  register-write status (bank 2), 1 write
04 00 00 00
```
(idn.pcap 0.5259 IN84 56 B.) The 0x0b block is 12 bytes:

| Offset | Field | Observed |
|--------|-------|----------|
| 0 | id 0x0b | |
| 1-2 | ibsta, big-endian | 0x2064 (END, REM, CIC, LACS) when the read ended on EOI; 0x0064 when the count was reached (trac.pcap 4.5344) or the timeout expired (nolistener.pcap 9.8126) |
| 3 | error code | 0x00; 0x0a on timeout (nolistener.pcap 9.8126, `0b 00 64 0a 00 b0 ff ff 60 00 00 00`) |
| 4-7 | count, 32-bit little-endian two's complement of (transferred - requested) | `52 b0 ff ff` = -20398 = 82 - 20480; `00 00 00 00` for a full 20480-byte chunk (trac.pcap 4.5344); `48 b1 ff ff` = -20152 = 328 - 20480 (trac.pcap 12.5002); `00 b0 ff ff` = -20480 = nothing read (nolistener.pcap 9.8126) |
| 8 | 0xe0 when the last byte came with EOI, 0x60 otherwise (same byte as item 3 of the 0x0a trailer in 5.2) | e0 after EOI; 60 for full chunks and for the timeout |
| 9-11 | `00 00 00` | always |

Bytes read = requested + count (count negative or zero); use this, not
the 0x88 transfer length, which may carry one padding byte. END (0x2000) in
ibsta is the end-of-message indication; it was set only on the last chunk
of the 61 768-byte read (trac.pcap 12.5002) and clear on the three full
20480-byte chunks (4.5344, 8.5276, 12.4537).

#### 10.1.4 Multi-chunk read (trac.pcap)

`:TRAC:DATA?` returned 61 768 bytes in four `viRead(20480)`. Each chunk was
the complete 40-byte message of 10.1.2 -- NI re-addresses the instrument
(`3f 20 58`) before every chunk -- and each reply was IN88 20480 B + IN84
56 B, with the fourth IN88 328 B and END set. The instrument, not the
adapter, set the pace (4.0 s per 20480 bytes). The 0x88 completions of
20480 bytes are a multiple of 512, so if the host's IN transfer was larger
than the data (the header-only traces recorded 32768-byte URBs) the device
must have ended the transfer with a zero-length packet; the pcap does not
record the URB size.

#### 10.1.5 The 0x0a path as NI uses it

NI's 0x0a message (32 bytes) is
```
03 00 00 00 | 0c fd 00 fd 3f 20 58 00 | 0a 00 0a fc f6 ff 00 00 | 09 01 00 02 03 01 00 00 | 04 00 00 00
```
(partial.pcap 0.5329 OUT 32 B, count 10). Differences from 5.2: no
embedded AUXMR 0x51/0x55 block at all (the two-write block of 5.2 is
absent; nothing clears END between 0x0a reads), and again no 0x06 before
the 0x0a -- the 0x0a reply shows ATN clear (ibsta 0x0064) after the 0x0c
reply showed it set (0x0074) (partial.pcap 0.5366 IN84 60 B).

Reply (count 10, 60 bytes):
```
03 00 28 00 00 00 ff ff
0c 00 74 00 00 00 ff ff
36 4b 45 49 54 48 4c 45 59 20 49 00 00 00 00 00   "KEITHLEY I" + zero filler
38 00 64 00 00 00 ff ff 60 0a 00 00               status: no END, error 0, count 0; tail 60, 10 valid
09 00 64 00 00 00 ff ff 01 00 00 00
04 00 00 00
```
Block sizing by requested count: 1..15 -> 0x36 blocks (16 bytes); 16 and
above -> 0x37 blocks (32 bytes) (counts.pcap 2.3664 count 15 = one 0x36;
2.9767 count 16 = one 0x37). Filler after the valid bytes: zeros in 0x36
blocks (counts.pcap 0.4323, 1.1448, 2.3664; eos.pcap 1.5500 ..), stale
bytes in 0x37 blocks (counts.pcap 4.2005, `4c 00 4b 00 4b 00 ...` after the
one valid byte `L`; 7.8878, `20 4d 20 4d` after `\n`).

**0x11 blocks.** When the reply uses 0x37 blocks and the 0x0a reply is
preceded in the same message reply by other blocks, four 4-byte blocks
`11 00 00 00` sit between the last preceding block and the first 0x37
block (counts.pcap 2.9767 IN84 92 B: 8 + 8 + 4 x 4 + 32 + 12 + 12 + 4).
They appear even when no data block follows (partial.pcap 5.2418 IN84 60
B, timeout with 0 bytes, count 200) and never in 0x36-sized replies nor in
a bare 0x0a reply with no preceding blocks (board_io.pcap 6.1058 IN84 16
B). In every occurrence they bring the offset of the first 0x37 block to
32; meaning beyond that not established. A parser must skip them.

**Zero-byte results.** On the 0x36 path a timed-out read still carries one
0x36 block of zeros (eos.pcap 35.1236 IN84 60 B: `36 00 .. 00 | 38 00 64
0a f6 ff ff ff e0 00 00 00`, error 0x0a, count -10, tail byte 0xe0 stale,
0 valid). On the 0x37 path (count 200) no data block is sent, only the
four 0x11 blocks (partial.pcap 5.2418). A bare 0x0a (INTFC session, count
200) that timed out returned only the 16-byte trailer `38 00 24 0a 38 ff
ff ff 60 1e 00 00 04 00 00 00` (board_io.pcap 6.1058); the last-block
count byte 0x1e is stale, as 3.6 says.

**Reply count semantics on 0x0a** are the 16-bit form of 3.3: `ee ff` =
-18 = 82 - 100 (counts.pcap 7.8878), `00 00` when the count was reached.

#### 10.1.6 Termination character

The character is passed only inside the read instruction; setting
`VI_ATTR_TERMCHAR_EN` / `VI_ATTR_TERMCHAR` produced no register write
(eosmodes.pcap 0.4188 OUT 12 B and 0.4198 OUT 12 B are the bank-2 0x03
write of 10.2.5, nothing else). Observed (eosmodes.pcap, count 200, 0x0a):

| TERMCHAR_EN | TERMCHAR | `m e` sent | Result |
|-------------|----------|------------|--------|
| false | 0x0a | `00 0a` | 82 bytes, END on EOI, tail 0xe0 (0.4229 OUT / 0.4330 IN84 156 B) |
| true | 0x0a | `14 0a` | same (1.0399 / 1.0500) |
| true | 0x2c `,` | `14 2c` | 26 bytes `KEITHLEY INSTRUMENTS INC.,`, ibsta END set, tail 0x60 (no EOI), count `52 ff` = -174 (1.6597 / 1.6650 IN84 92 B) |
| true | 0x0d | `14 0d` | 82 bytes to EOI (2.2728 / 2.2827) |

So `m` = 0x14 (REOS + BIN) enables the compare and END is reported for an
EOS match exactly as for EOI, distinguishable only by bit 7 of the tail
byte. With the character disabled NI sends `m` = 0x00 and **`e` = the
current TERMCHAR (0x0a), not 0x00**, and none of these reads returned
error 4 (42 in 0x0a form and 58 in 0x0b form across the 28 captures; 36
and 44 in the first batch; every one of them returned error 0 or, when
nothing was pending, 0x0a). This contradicts the [inherited] "both must
be 0x00, else error 4" rule, at least with NI's initialisation (AUXRA
0x99, 10.3.1); 4.3, 5.2 and 8.3 now carry that rule as not reproduced.

#### 10.1.7 Count-limited read leaving data in the instrument

`viRead(10)` then `viRead(200)` on one 82-byte message (partial.pcap):
the first returned 10 bytes with count 0, no END (0.5366); the second was a
fresh 32-byte message including the addressing 0x0c and returned the
remaining 72 bytes with END (0.5456 IN84 156 B: three 0x37 blocks, tail
`e0 0c`, count `80 ff` = -128 = 72 - 200). Re-addressing an instrument
that still holds data does not disturb it. `viClear` (10.5.3) between
rounds in counts.pcap discarded the remainder each time.

#### 10.1.8 Timeouts on reads

timeout_expiry.pcap sets `VI_ATTR_TMO_VALUE` to 100, 300, 1000, 3000,
10 000 and 30 000 ms in turn and calls `viRead(100)` with nothing
pending. Count 100 is below the 0x0b threshold (10.1.1), so all six are
framed reads and nothing happens on 0x88. Each message is 32 bytes,
`03 00 00 00 | 0c fd 00 fd 3f 20 58 00 | 0a 00 0a tt 9c ff 00 00 |
09 01 00 02 03 01 00 00 | 04 00 00 00`, differing only in `tt`; each
reply is 60 bytes with the read's block `38 00 64 0a 9c ff ff ff 60 1e
00 00` -- ibsta 0x0064, error 0x0a, all 100 bytes not transferred.
Before each read NI writes the same code to bank-2 register 0x07
(10.2.4), e.g. `02 07 f9` at 0.4146.

| Asked | `tt` | OUT submitted | IN84 completed | Expiry (s) | Error | VISA saw |
|-------|------|---------------|----------------|------------|-------|----------|
| 100 ms | 0xf9 | 0.415501 | 0.547773 | 0.132272 | 0x0a | TMO after 0.133 s |
| 300 ms | 0xfa | 0.950453 | 1.213994 | 0.263541 | 0x0a | 0.264 s |
| 1000 ms | 0xfb | 1.617318 | 2.667155 | 1.049837 | 0x0a | 1.050 s |
| 3000 ms | 0xfc | 3.070347 | 7.265956 | 4.195609 | 0x0a | 4.196 s |
| 10 000 ms | 0xfd | 7.669550 | 24.447973 | 16.778423 | 0x0a | 16.780 s |
| 30 000 ms | 0xfe | 24.851128 | 58.406386 | 33.555258 | 0x0a | 33.556 s |

(Microsecond timestamps from the pcap records; usbpcap_dump.py prints
four decimals. The OUT transfer itself completes 0.12-0.20 ms after
submission, so timing from its completion changes the last column by
that much.) Afterwards, with the timeout back at 3000 ms, `*CLS` and
`*IDN?` succeed at once (58.8097 onward, 82 bytes on 0x88 at 58.8247):
six expiries in a row leave nothing to recover from.

The same expiry in the earlier captures: under 0xfc, partial.pcap
1.046186 OUT -> 5.241826 IN84 = 4.195640 s (0x0a); board_io.pcap
1.910505 -> 6.105821 = 4.195316 s (0x0a with no 0x0c in the message);
nolistener.pcap 5.616437 -> 9.812593 = 4.196156 s (0x0b; its 0x88
transfer ends with zero bytes at 9.812110); raw_errors.pcap 6.107460 ->
10.303403 = 4.195943 s (0x0b; 0x88 at 10.302941) and 10.804573 ->
15.000340 = 4.195767 s (0x10). Under 0xfe, eos.pcap 1.568237 ->
35.123582 = 33.555345 s (0x0a, session timeout 20 000 ms).

Each is a power of two in microseconds -- 2^17, 2^18, 2^20, 2^22, 2^24,
2^25 for 0xf9..0xfe -- plus 0.8-1.9 ms; the comparison, the one code
that expires early (0xfa: 0.2635 s against a nominal 300 ms), what was
not measured and what the pattern would predict for it are in 7.3.

What the code bounds cannot be read off these captures. With code 0xfc
the three full 20480-byte chunks of the 61 768-byte read took 3.999,
3.992 and 3.926 s and completed with error 0 (readtimeout_long.pcap
0.5227 -> 4.5217, 4.5223 -> 8.5146, 8.5152 -> 12.4410, OUT to IN84), in a
session whose VISA timeout was 2000 ms. That is longer than the
application asked for and longer than the nominal 3 s of 0xfc, but
shorter than the 4.194 s the adapter waits under 0xfc. The same chunks
took the same time under 0xfe (trac.pcap), so the 4.0 s is the
instrument's pace, not a limit. No instruction was captured running
longer than the measured expiry of its code; whether the code bounds the
whole instruction or an interval within it (per byte, per handshake) is
therefore not established (7.1).

**Superseded (10.1.8).** "12-40 % over nominal" as the description of the
expiry: it came from 0xfc and 0xfe alone; across the six codes the
expiry runs from 12 % under nominal to 68 % over (7.3). "The code does
not bound the whole instruction: with code 0xfc every 20480-byte chunk
... took 4.0 s and completed with error 0. What the code bounds (a
per-byte or handshake interval) cannot be read off the wire": the second
sentence stands, the first does not follow once 0xfc is known to run
4.194 s.

#### 10.1.9 VISA timeout to code byte

From timeouts.pcap (`VI_ATTR_TMO_VALUE` set, then `*IDN?`; the code
appears identically in bank-2 register 0x07 (10.2.4), in the 0x0d and in
the 0x0b of that query):

| VI_ATTR_TMO_VALUE | Code | §7.1 says |
|-------------------|------|-----------|
| 100 ms | 0xf9 (7.4806 / 7.4820 / 7.4843) | 0xf9 agrees |
| 300 ms | 0xfa (7.8968) | agrees |
| 1000 ms | 0xfb (8.3125) | agrees |
| 2000 ms (set explicitly, nolistener.pcap 5.1127) | 0xfc | agrees |
| 3000 ms | 0xfc (8.7260) | agrees |
| 10 000 ms | 0xfd (9.1410) | agrees |
| 20 000 ms (open_inst default in the harness, e.g. open.pcap 0.0114) | 0xfe | agrees |
| 30 000 ms | 0xfe (9.5554) | agrees |
| 100 000 ms | 0xff (9.9694) | agrees |
| 300 000 ms | 0x01 (10.3843) | agrees |
| 1 000 000 ms | 0x02 (10.7994) | agrees; the report that NI sends 0xff here is wrong |
| VI_TMO_INFINITE | 0xf0 (11.2154) | agrees |

timeout_expiry.pcap shows the same codes for 100, 300, 1000, 3000,
10 000 and 30 000 ms in a 0x0a (10.1.8), the instruction timeouts.pcap
did not exercise.

The code NI programs at session open, before the application touches
`VI_ATTR_TMO_VALUE`, is 0xfb (every INSTR open, e.g. open.pcap 0.0070 OUT
32 B). The 0x0c addressing blocks NI emits inside INSTR operations carry
0xfd regardless of the session timeout (354 of the 367 0x0c blocks in the
28 captures -- 237 of 250 in the first batch -- including the 100 ms
sessions, timeouts.pcap 7.4820 and timeout_expiry.pcap 0.4155, where the
read beside it carries 0xf9); the 13 with 0xfc are
the board-level `viGpibCommand` calls of INTFC sessions (session timeout
2000 or 3000 ms) and every 0x0c of srq_poll.pcap after an INTFC session
had been opened and closed in the same process (3.0392 onward). Meaning of
that change not established.

### 10.2 Message composition

#### 10.2.1 Rules observed

- A message may carry several instruction blocks; NI combines up to five
  (0x03, 0x0c, 0x0b, 0x09, 0x09) before the termination block. Blocks
  execute in message order: in `08 03 .. | 09 01 00 01 0a 1f` the register
  read returns the BSR value from before the REN write (ren.pcap 4.1164 OUT
  20 B: BSR 0x00 read, then REN asserted; 3.7144: BSR 0x01 read, then REN
  cleared).
- Each block is zero-padded to a multiple of 4 (0x0c with 3 command bytes
  -> 8 bytes; 0x0d with 7 data bytes -> 16; with 4 -> 12; with 13 -> 24;
  with 17 -> 28 (trac.pcap 13.0009 OUT 52 B)). One `04 00 00 00` ends the
  message.
- The reply is the concatenation of one reply per block, in block order,
  followed by a single `04 00 00 00`. Per-block reply lengths: 0x01, 0x03,
  0x06, 0x0c, 0x0d, 0x0e, 0x0f -> 8; 0x02, 0x09, 0x0b -> 12; 0x08 with n
  reads -> 4 x ceil(n/3) + 4; 0x10 -> 12 (10.5.4), but 8 when the poll
  failed (10.6.6); 0x0a -> [16 bytes of 0x11 blocks] + data blocks + 12.
  Example: the 40-byte write message
  (0x03 + 0x0c + 0x0d + 0x09) gets a 40-byte reply = 8 + 8 + 8 + 12 + 4
  (idn.pcap 0.5133 OUT / 0.5154 IN84).
- The count field (bytes 4-5, and 6-7) of every status block in a reply
  echoes the last data operation's count until a data operation in that
  same message overwrites it; the 0x03 and 0x09 blocks never write it
  (idn.pcap 0.5259: 03 carries `00 00 ff ff` from the write, the 09 blocks
  carry `52 b0 ff ff` from the 0x0b just before them).

#### 10.2.2 The 0x03 block

`03 00 00 00`. Reply: one 8-byte status block with id 0x03, error 0, and
the ibsta current when the block executes (idn.pcap 0.5154: 0x0030 before
the write; 0.5259: 0x0028 = CIC|TACS left by the write). It is the first
block of every INSTR-session message that carries a 0x0c, 0x0d, 0x0e,
0x0a, 0x0b or 0x10 -- with one exception, the lone LLO 0x0c of
`viGpibControlREN` (10.7.4, ren_device.pcap 0.9192, 1.3241) -- of the
first bank-2 configuration message of a session
(open.pcap 0.0070 OUT 32 B) and of the second session's first message
(two_sessions.pcap 0.3543 OUT 16 B: `03 | 09 bank2 03 | 04`). Never sent
alone, never in INTFC-session operations (intfc.pcap, board_io.pcap,
ren.pcap have none), never in the 12-byte bank-2 write (10.2.5), the
timeout update, the close or the shutdown. Its only observable effect is
the status snapshot in the reply.

#### 10.2.3 Addressing blocks

INSTR operations address with a 0x0c that puts the adapter's talk/listen
address first:

| Operation | 0x0c payload | §6 form |
|-----------|--------------|---------|
| before 0x0d / 0x0e | `40 3f 38` = MTA 0, UNL, LAD 24 (idn.pcap 0.5133) | `3f 40 38` |
| before 0x0a / 0x0b | `3f 20 58` = UNL, MLA 0, TAD 24 (idn.pcap 0.5160) | `3f 20 58` |
| with secondary address 1 | `40 3f 38 61` and `3f 20 58 61` (nolistener.pcap 10.8856, 10.8875) | `[60+S]` after the primary, agrees |
| selected device clear | `40 3f 38 04` (clear.pcap 0.5137) | `3f 38 04` |
| trigger | `40 3f 38 08` (trigger.pcap 0.5126) | `3f 38 08` |
| selected device clear, secondary address 1 | `40 3f 38 61 04` (sad_poll.pcap 0.9178) | `3f 38 61 04`, agrees on the `60+S` placement |
| trigger, secondary address 1 | `40 3f 38 61 08` (sad_poll.pcap 1.3212) | `3f 38 61 08`, agrees |
| go to local | `40 3f 38 01` (ren_device.pcap 1.7255) | `3f 38 01` |

Command count byte: `fd` for 3 bytes, `fc` for 4 (3.3 agrees); `fb` for
the 5 bytes of the secondary-address SDC / GET, whose block `0c fb 00 fd
40 3f 38 61 04 00 00 00` is 12 bytes (sad_poll.pcap 0.9178 OUT 28 B); `ff`
for the single LLO byte (ren_device.pcap 0.9192). The 0x0c
reply after `40 3f 38` shows ibsta 0x0038 (CIC, ATN, TACS); after `3f 20
58`, 0x0074 (REM, CIC, ATN, LACS).

#### 10.2.4 Bank-2 registers 0x03..0x07

All observed writes to bank 2 outside the 26-write initialisation:

| addr | values | when |
|------|--------|------|
| 0x03 | 0x01, never another value (655 writes in the 28 captures; the first batch counted 481) | 10.2.5 |
| 0x04 | 0x01 at session open (open.pcap 0.0070), 0x00 at the close of the last session on the address (open.pcap 1.0126 OUT 20 B; two_sessions.pcap 1.9060) | |
| 0x05 | 0x18 = the instrument's primary address 24; 0x05 for `GPIB0::5::INSTR` (nolistener.pcap 0.0215) | |
| 0x06 | 0x00 with no secondary address; 0x61 = 0x60 \| 1 for `GPIB0::24::1::INSTR` (nolistener.pcap 10.8794) | |
| 0x07 | the timeout code of 10.1.9 | written at open (0xfb) and whenever `VI_ATTR_TMO_VALUE` changes, as `09 04 00 02 04 01 02 05 18 02 06 00 02 07 tt` in a 28-byte message with a leading bank-2 0x03 write (open.pcap 0.0114 OUT 28 B) |

Meaning of 0x03 and 0x04 not established beyond the above. The data
instructions carry the address, secondary address and timeout themselves
(10.2.3, 10.1.2, 10.5.4), so these registers duplicate what the
instructions already say.

#### 10.2.5 The 12-byte bank-2 0x03 write

`09 01 00 02 03 01 00 00 04 00 00 00`, reply 16 bytes `09 ss ss 00 cc cc
xx xx 01 00 00 00 04 00 00 00`. Sent: twice after the configuration message
at open and once after the probe (open.pcap 0.0077, 0.0081, 0.0110); as
the last block of every INSTR operation message; alone for `viEnableEvent`
and `viDisableEvent` (srq.pcap 1.0201, 2.5399); alone for each
`viSetAttribute` of TERMCHAR / TERMCHAR_EN (eosmodes.pcap 0.4188, 0.4198);
at the close of a session that is not the last one on the address
(two_sessions.pcap 1.2908 OUT 12 B); and repeatedly, every 15 ms, during a
`viWaitOnEvent` for SRQ with nothing pending (srq.pcap 3.0463 .. 4.0395,
about 65 messages in 1 s). The reply's ibsta is then the only information
NI obtains, so this message serves as a status poll.

### 10.3 Session open and close

#### 10.3.1 The 88-byte initialisation

`09 1a 00` + 26 (bank, addr, value) triplets + `00 00 00` + `04 00 00 00`
(open.pcap 0.0000 OUT 88 B; byte-identical in all 28 captures, each a new
process). Compared with 2.6, write by write, the order and registers are
the same and the values agree except one:

| # | 2.6 | Observed | Note |
|---|-----|----------|------|
| 3 | 1 0x0a 0x81 (or 0x91) | `01 0a 99` | AUXRA 0x99 = holdoff-on-all (bit 0) + XEOS (bit 3) + BIN (bit 4) |
| 4 | same as #3 | `01 06 99` | |
| 12, 13, 14 | T1 row | `01 0a e9`, `01 0a a4`, `01 17 00` | the 500 ns row of 2.7 |
| 16 | 0x03 / 0x02 | `01 1c 03` | system controller |
| 18, 19 | P | `01 0c 00`, `02 00 00` | adapter address 0 |
| 20, 21, 22 | no secondary | `01 0c e0`, `01 08 31`, `02 01 00` | |
| all others | | as in 2.6 | |

Reply `09 00 00 00 cc cc ff ff 1a 00 00 00 04 00 00 00`: ibsta 0x0000,
error 0, 26 writes, count bytes stale (open.pcap 0.0011 IN84 16 B). The
stale count is whatever the previous process left (`52 b0`, `00 00`, `38
ff` ..); in read_thresholds.pcap alone it was `aa 55` (0.0011), the value
4.1 reports from the other unit. Cause not established; it would fit the
adapter having been restarted before that capture.

#### 10.3.2 INSTR session open (`viOpen GPIB0::24::INSTR`)

After the initialisation (open.pcap, times as given; idn.pcap and every
other INSTR capture are the same to the byte):

| t | OUT | Meaning | Reply |
|---|-----|---------|-------|
| 0.0012 | `08 03 01 0d 01 0c 01 1f` (12 B) | read bank-1 0x0d, 0x0c, 0x1f (BSR) | `34 00 00 00 35 03 00 00 04..` (12 B) |
| 0.0015 | `0f 00 00 00` (8 B) | IFC pulse | `0f 00 30 00 ..` ibsta CIC\|ATN (12 B) |
| 0.0020 | `08 03 01 0d 01 0c 01 1f \| 09 01 00 01 0a 1f 00 00` (20 B) | the same 3 reads, then REN on | `34 00 30 a0 35 03 00 00 \| 09 .. 01 00 00 00 \| 04..` (24 B) |
| 0.0070 | `03 \| 09 01 00 02 03 01 \| 09 04 00 02 04 01 02 05 18 02 06 00 02 07 fb` (32 B) | snapshot; bank-2 0x03 := 1; bank-2 0x04 := 1, 0x05 := PAD, 0x06 := SAD byte or 0, 0x07 := timeout 0xfb | 36 B |
| 0.0077, 0.0081 | bank-2 0x03 := 1 (12 B) x 2 | 10.2.5 | 16 B each |
| 0.0085 | `02 18 00 00` (8 B) | presence probe of PAD 24 (10.6.1) | `02 00 30 00 00 00 ff ff 01 00 00 00 04..` (16 B), 1.8 ms |
| 0.0110 | bank-2 0x03 := 1 (12 B) | | 16 B |
| 0.0114 | `09 01 00 02 03 01 \| 09 04 00 .. 02 07 fe` (28 B) | timeout update caused by the harness setting 20 s | 28 B |

No control request is issued at an INSTR open (none in open.pcap, idn.pcap
or any INSTR-only capture; the 0x21 request appears only in INTFC sessions
and at SRQ time, 10.3.5 and 10.4). No take-control (0x01) is sent; IFC
alone leaves the adapter CIC with ATN true. Compared with 2.8: steps 2-4
and 6 (readiness poll, model extras, monitor mask) are not in the captures
(they may belong to driver load, before any process); the register
initialisation, IFC and REN agree; the take-control of step 7 is absent;
the bank-2 configuration and the probe are additions.

The three register values read at open: bank-1 0x0d always 0x00 (all 68
reads in the 28 captures; 50 in the first batch); bank-1 0x0c equal to the
low byte of the current ibsta in every read (0x00 before IFC, 0x30 after
IFC, 0x64 / 0x74 later: nolistener.pcap 10.8702, srq.pcap 2.0333), which
does not match the ADR0 label of 3.4; bank-1 0x1f = BSR: 0x00 before IFC
with REN off (0x20, NDAC alone, in six captures, e.g. ren_device.pcap
0.0017), 0xa0 (ATN, NDAC) after IFC, 0xa1 with REN, 0x31 (NDAC, NRFD, REN)
at the second open of nolistener.pcap (10.8702) after a read had left ATN
false.

#### 10.3.3 Close

`viClose` of the last session on the address: `09 01 00 02 03 01 00 00 |
09 01 00 02 04 00 00 00 | 04 00 00 00` (20 B), reply 28 B (open.pcap
1.0126). Of a session that is not the last: the 12-byte bank-2 0x03 write
only (two_sessions.pcap 1.2908). About 0.5 s after the last close, at the
end of the process, the shutdown of 2.9 byte for byte: `09 02 00 01 0a 02
03 10 00 00 00 00 04 00 00 00` (open.pcap 1.5764 OUT 16 B), reply `09 ss
ss 00 cc cc xx xx 02 00 00 00 04..`. The ibsta of that reply is not
stable: 0xffff in 25 of the 28 captures (e.g. open.pcap 1.5771); 0x0000
with bytes 4-7 zero as well, `09 00 00 00 00 00 00 00 02 ..`, in
read_thresholds.pcap (5.2342) and counts.pcap (20.5265), the two captures
whose last bus operation was the SDC of a `viClear` (ibsta 0x0078 before
the close); 0x1010 in board_io.pcap (9.6195, see 10.7.2). An
implementation must not test the shutdown reply's ibsta.

**Superseded (10.3.3).** "ibsta reads 0xffff after the chip reset (every
capture; 0x1010 in board_io.pcap)", written after the first batch and
corrected after the second.

#### 10.3.4 Second session in the same process (two_sessions.pcap)

The second `viOpen` on the same address (0.3349 ..) sent: the 3-register
read (BSR 0xa1); `03 | 09 bank-2 0x03 := 1` (16 B); two bank-2 0x03 writes;
the probe `02 18 00 00`; two more bank-2 0x03 writes. No 88-byte
initialisation, no IFC, no REN, no bank-2 0x04..0x07 configuration. A
second open on a *different* address in the same process (nolistener.pcap
10.8702 .., `GPIB0::24::1::INSTR` after `GPIB0::5::INSTR`) sent the
3-register read, then the 32-byte configuration with the new PAD/SAD/
timeout, the probe `02 18 61 00`, the 28-byte timeout update, and again no
initialisation or IFC.

#### 10.3.5 INTFC session open and the 0x21 status request

`viOpen GPIB0::INTFC` sends the 88-byte initialisation, then five or six
control requests `bmRequestType 0xC0, bRequest 0x21, wValue 0x0200, wIndex
0, wLength 8` in a row (intfc.pcap 0.0014 .. 0.0028: 5; board_io.pcap 6)
and nothing else -- no IFC, no REN, no bank-2 writes. The 8-byte reply is a
status block (4.1) with id 0x21:

```
21 00 00 00 52 b0 ff ff     ibsta 0x0000, error 0, stale count      (intfc.pcap 0.0016)
21 00 30 00 00 00 ff ff     ibsta 0x0030 = CIC|ATN after IFC        (intfc.pcap 3.0136)
21 00 74 00 00 00 ff ff     ibsta 0x0074 = REM|CIC|ATN|LACS         (srq_poll.pcap 1.5310)
```
The bits that changed with bus state across the captures are exactly the
ibsta bits of 4.2 (ATN 0x10, CIC 0x20, REM 0x40, LACS 0x04, TACS 0x08,
END 0x2000). SRQI (0x1000) was never set in a 0x21 reply, including the
one taken while the instrument's status byte had RQS (srq_poll.pcap
1.5310; see 10.4.4). The request reads status without bulk traffic; it is
the 5.12 request, now observed.

Attribute reads on INTFC (intfc.pcap 3.0130 .. 3.0167, for ATN, REN,
NDAC, SRQ, CIC, SYS_CNTRL states in that order): one 0x21 request, two
3-register reads (`08 03 01 0d 01 0c 01 1f`, BSR 0xa1 both times), three
0x21 requests. The results (ATN 1, REN 1, NDAC 1, SRQ 0, CIC 1) agree with
ibsta 0x0030 and BSR 0xa1 (bit 0 REN, bit 5 NDAC, bit 7 ATN, 5.13).
`VI_ATTR_GPIB_SYS_CNTRL_STATE`, `HS488_CBL_LEN`, `PRIMARY_ADDR` produced
no further traffic.

### 10.4 Service request

#### 10.4.1 Enable / disable

`viEnableEvent(VI_EVENT_SERVICE_REQ, VI_QUEUE)` and `viDisableEvent` each
sent only the 12-byte bank-2 0x03 write (srq.pcap 1.0201 OUT 12 B, 2.5399
OUT 12 B). No register of the controller chip (IMR0..3) is touched, no
monitor-mask request (0x21 / 0x0300) appears, and the interrupt IN
transfer was already pending (its dispatch predates every capture; only
completions and re-arms are recorded).

#### 10.4.2 The interrupt push

After `*OPC` (srq.pcap 1.5220 OUT 40 B, reply 1.5240), 2.6 ms later:
```
1.5266 INTR ep81 cmpl 8 B:  30 18 00 60 31 a1 01 00
1.5267 CTRL OUT  bmRequestType 0x40, bRequest 0x3b, wValue 0, wIndex 0, wLength 0 (setup 40 3b 00 00 00 00 00 00)
1.5269 CTRL cmpl (no data)
1.5269 INTR ep81 req             the interrupt read is re-armed
```
The same push, `30 18 00 60 31 a1 01 00`, appeared in srq_poll.pcap
(1.0308) where no event had been enabled, so the adapter pushes on SRQ
without any enable from the session. Read as a status block (4.1): byte 0
= 0x30 (not an instruction echo); bytes 1-2 = ibsta 0x1800 = SRQI (0x1000)
+ RQS (0x0800); byte 3 = 0x60; bytes 4-7 = `31 a1 01 00`, meaning not
established. Byte 3 = 0x60 = 96 = the instrument's status byte (RQS + ESB)
at that moment: the following `viReadSTB` returned 96 without any serial
poll on the bulk pipe (srq.pcap 2.0325 OUT 20 B is a 2-register read of
bank-1 0x0d, 0x0c plus the bank-2 0x03 write, reply `34 00 74 19 35 02 00
00 ..`; srq_poll.pcap 2.0344 likewise), and by 1.53 s the SRQ line was no
longer asserted (0x21 reply without SRQI; `VI_ATTR_GPIB_SRQ_STATE` read 0)
although nothing on the bulk pipe had polled the instrument. The adapter
therefore serial-polled the device itself and reported the status byte in
the push; the explicit poll that followed returned 32 = ESB only
(srq_poll.pcap 2.5379, 10.5.4). The 0x3b request follows every push and
precedes the re-arm; its function beyond that is not established.
`viWaitOnEvent` itself caused no traffic when the event was already queued
(srq.pcap: 1.5240 .. 1.5310 shows only the push).

#### 10.4.3 Wait with nothing pending

`viWaitOnEvent` for 1 s with no SRQ (srq.pcap 3.0463 .. 4.0395): the
12-byte bank-2 0x03 write every 15 ms, each reply's ibsta 0x0064 (no
SRQI). No interrupt completion, no control request.

#### 10.4.4 SRQ_STATE attribute

`VI_ATTR_GPIB_SRQ_STATE` on a freshly opened INTFC session while the 2420
had `*SRE 32` and `*OPC` done (srq_poll.pcap 1.5306 .. 1.5337): seven 0x21
/ 0x0200 requests (six for the INTFC open, one for the attribute), all
replying ibsta 0x0074, SRQI clear, value 0 -- consistent with the adapter
having already polled the device (10.4.2). Whether SRQI ever appears in a
0x21 reply while SRQ is held is not shown by the captures.

### 10.5 Writes, trigger, clear, serial poll

#### 10.5.1 Write (0x0d)

Header as sent by NI: `0d cl ch t 00 e f 00`, i.e. 5.1 with **byte 5 = the
session's termination character** (0x0a by default; 0x2c and 0x0d after
`VI_ATTR_TERMCHAR` was set to 44 / 13, eosmodes.pcap 1.6576, 2.2707) and
byte 4 = 0x00 in all 114 writes captured (83 in the first batch). `f` = 0x08
with `VI_ATTR_SEND_END_EN` true, 0x00 with it false (write.pcap 0.5133 vs
1.0173 OUT 40 B, `*CLS\r\n`). The terminator is ordinary data: `*CLS\r\n` =
6 bytes, count `fa ff`; `viWrite` of `*CLS` without terminator = 4 bytes,
`fc ff`, block 12 bytes, message 36 (write.pcap 1.5201). Reply block `0d 00
28 00 00 00 ff ff`: ibsta CIC|TACS with ATN clear, error 0, count 0. Whether
`e` in the 0x0d header has any effect (e.g. EOI on the character) was not
tested; every write carried EOI or not per `f` alone.

#### 10.5.2 Long write (0x0e, new; longwrite.pcap)

NI chooses between 0x0d and 0x0e by length alone: **length <= 2048 ->
0x0d, length >= 2049 -> 0x0e** (write_thresholds.pcap, below). The write
boundary is not the read boundary (1024 / 1025, 10.1.1).

The first raw write captured, a 2050-byte `viWrite` (2048 bytes +
`\r\n`), was sent as
```
03 00 00 00
0c fd 00 fd 40 3f 38 00
0e 00 00 fe 00 0a 08 00 fe f7 ff ff
09 01 00 02 03 01 00 00
04 00 00 00
```
(1.8914 OUT 36 B), with the 2050 data bytes as one transfer on **bulk OUT
0x06** (1.8916 OUT06 2050 B, completed 1.9965), raw, no padding, no
termination block. The 0x0e block is 12 bytes: `0e 00 00 t 00 e f 00` +
32-bit little-endian negative count (`fe f7 ff ff` = -2050); `t`, `e`, `f`
as in 0x0d. The reply (2.0354 IN84 40 B) is `03 .. | 0c .. | 0e 00 28 00 00
00 00 00 | 09 .. 01 00 00 00 | 04..`: an 8-byte status block with id 0x0e,
count `00 00 00 00`. 0x0e is
to 0x0d what 0x0b is to 0x0a, and the endpoint pairs 0x02/0x84 (framed) and
0x06/0x88 (raw) are the "alternate endpoints" of 1.2.

The boundary, from write_thresholds.pcap: `viWrite` of 18, 24, 32, 48, 63,
64, 65, 100, 128, 255, 256, 257, 512, 1024, 1025 and 2048 bytes (`*CLS;`
repeated, `\n` last, EOI on) each went as one framed 0x0d on 0x02; 2049
bytes went as 0x0e with the data on 0x06, which gives the rule at the head
of this subsection.

*0x0d beyond 17 bytes.* Nothing in the layout changes with length. The
block is the 8-byte header `0d cl ch fc 00 0a 08 00`, the data bytes
inline, and zero padding to a multiple of 4; there is no second count
field and no framing inside the data. The message stays `03 | 0c .. 40 3f
38 | 0d .. | 09 bank-2 0x03 | 04`, 4 + 8 + 8 + pad4(n) + 8 + 4 bytes,
and the reply stays the 40 bytes of 10.5.1 with `0d 00 28 00 00 00 ff ff`.

| Length | `cl ch` | Pad bytes | Message, as OUT transfers on 0x02 | Reply after |
|--------|---------|-----------|-----------------------------------|-------------|
| 18 | `ee ff` | 2 | 52 B (0.3133) | 2.8 ms |
| 24 | `e8 ff` | 0 | 56 B (0.6169) | 3.3 ms |
| 32 | `e0 ff` | 0 | 64 B (0.9211) | 3.7 ms |
| 48 | `d0 ff` | 0 | 80 B (1.2258) | 4.8 ms |
| 63 | `c1 ff` | 1 | 96 B (1.5317) | 5.8 ms |
| 64 | `c0 ff` | 0 | 96 B (1.8382) | 5.7 ms |
| 65 | `bf ff` | 3 | 100 B (2.1445) | 5.9 ms |
| 100 | `9c ff` | 0 | 132 B (2.4516) | 8.2 ms |
| 128 | `80 ff` | 0 | 160 B (2.7602) | 10.1 ms |
| 255 | `01 ff` | 1 | 288 B (3.0713) | 19.2 ms |
| 256 | `00 ff` | 0 | 288 B (3.3919) | 19.1 ms |
| 257 | `ff fe` | 3 | 292 B (3.7119) | 19.2 ms |
| 512 | `00 fe` | 0 | 512 B (4.0321) + 32 B (4.0326) | 36.9 ms |
| 1024 | `00 fc` | 0 | 512 B (4.3702) + 544 B (4.3709) | 72.5 ms |
| 1025 | `ff fb` | 3 | 512 B (4.7432) + 548 B (4.7436) | 72.4 ms |
| 2048 | `00 f8` | 0 | 512 B (5.1166) + 1568 B (5.1171) | 321.1 ms |

Messages up to 292 bytes were one OUT transfer. Messages of 544 bytes and
more were handed to USB as two transfers, the first exactly 512 bytes and
the second the remainder, the second submitted when the first completed
and the IN on 0x84 submitted in between. The cut falls wherever byte 512
happens to be (inside the data in all four cases) and 512 is the
endpoint's packet size, so on the bus the two are one run of full packets
ended by a short one: the device cannot see the split, and it carries no
protocol meaning. No message with a total length that is a multiple of
512 was captured, so whether such a message needs a zero-length packet is
not established. The reply comes only after the instrument has taken the
data; the second OUT transfer itself completed late for the longer
messages (2048: submitted 5.1171, completed 5.2204, 103 ms), so an OUT
transfer on 0x02 can take as long as the bus handshake with the
instrument lets it.

*0x0e at 2049 bytes.*
```
5.7387 OUT   36 B   03 00 00 00 | 0c fd 00 fd 40 3f 38 00 | 0e 00 00 fc 00 0a 08 00 ff f7 ff ff | 09 01 00 02 03 01 00 00 | 04 00 00 00
5.7393 OUT06 2049 B the data, one transfer, submitted before anything came back
5.7393 IN84  submitted
6.1078 OUT06 completes (368 ms)
6.3530 IN84  40 B   03 00 28 .. | 0c 00 38 .. | 0e 00 28 00 00 00 00 00 | 09 00 28 00 00 00 00 00 01 00 00 00 | 04 00 00 00
```
The header fields behave as described above for the 2050-byte write:
byte 3 = the session timeout code (0xfc, 3 s here; 0xfe there), byte 4 =
0x00, byte 5 = 0x0a the session's termination character, byte 6 = 0x08
the EOI flag, bytes 8-11 = `ff f7 ff ff` = -2049 as a 32-bit
little-endian count. The 0x06 transfer was 2049 bytes, an odd number: raw
write data is **not** padded to an even length (the raw read is, 10.1.3).
Reply count `00 00 00 00`, and from then on bytes 6-7 of later status
blocks read `00 00` (6.8561), as 10.8 says. Both raw writes captured had
EOI on and the default termination character, so `f` = 0x00 and other `e`
values on 0x0e were not observed. A raw write that fails is in 10.6.5.

**Superseded (10.5.2).** "The switch from 0x0d lies between 17 bytes
(trac.pcap 13.0009, 0x0d) and 2050 bytes; no length in between was
captured" -- first batch; replaced by the 2048 / 2049 boundary.

#### 10.5.3 Trigger and device clear

`viAssertTrigger`: `03 | 0c fc 00 fd 40 3f 38 08 | 09 bank-2 0x03 | 04` (24
B), reply 32 B with 0x0c ibsta 0x0038 (trigger.pcap 0.5126). `viClear`:
same with `04` (SDC) in place of `08` (clear.pcap 0.5137). Both are 5.8 /
5.18 with MTA 0 prepended (10.2.3). The next write after a clear is a
normal 40-byte write; nothing else is re-sent.

Observed in sad_poll.pcap (session `GPIB0::24::1::INSTR`): `viClear` = `03
| 0c fb 00 fd 40 3f 38 61 04 00 00 00 | 09 bank-2 0x03 | 04` (0.9178 OUT
28 B) and `viAssertTrigger` the same with `08` (1.3212 OUT 28 B) -- the
secondary-address byte `61` = 0x60 | 1 directly after the listen address,
before the SDC / GET; reply 32 B with 0x0c ibsta 0x0078, error 0, count 0.
The write addressing in that session is `40 3f 38 61` (1.7233 OUT 40 B,
`*CLS\r\n`, reply ibsta 0x0068, error 0), as in nolistener.pcap (10.2.3).
At open the secondary address goes into bank-2 0x06 (`02 06 61`, 0.0083)
and into the probe (`02 18 61 00`, 0.0100 -> present). The 2420 has no
secondary addressing of its own; it answered all of these, so it evidently
ignores the secondary byte.

#### 10.5.4 Serial poll (0x10, new)

There is a dedicated instruction. `viReadSTB`:
```
03 00 00 00 | 10 01 00 x P S t 00 | 09 01 00 02 03 01 00 00 | 04 00 00 00     (24 B)
```
`P` = 0x18 primary address, `S` = 0x00 without a secondary address and 0x60
| S with one (sad_poll.pcap, below), `t` = timeout code 0xfe; `x` = 0x00 in
three polls of a fresh session (stb.pcap 0.5134, 0.7167, 0.9191) and 0x01 in
the poll after an SRQ had been serviced (srq_poll.pcap 2.5361), meaning not
established. Reply (36 B):
```
03 00 74 00 00 00 ff ff
3a 18 00 20                      id 0x3a, P, 0x00, status byte (0x20 = 32; 0x00 in stb.pcap)
39 00 74 00 00 00 ff ff          status block id 0x39: ibsta REM|CIC|ATN|LACS, error 0
09 00 74 00 00 00 ff ff 01 00 00 00
04 00 00 00
```
(srq_poll.pcap 2.5379 IN84 36 B; stb.pcap 0.5151 `3a 18 00 00`.) The 0x0c /
0x06 / 0x0a sequence of 5.9 is not used by NI; 5.9 remains valid as an
IEEE-488.1 procedure but is not what the vendor driver sends.

Observed in sad_poll.pcap: through secondary address 1 the instruction is
`10 01 00 00 18 61 fc 00` (0.5152 OUT 24 B) -- `S` = 0x61 = 0x60 | 1 --
and the reply's 0x3a block echoes
both address bytes: `3a 18 61 04 | 39 00 74 00 00 00 ff ff` (0.5170 IN84
36 B; status byte 4, the value `viReadSTB` returned). `x` was 0x00 here
and in raw_errors.pcap (10.8046); its meaning stays not established. When
the poll fails the 0x3a block is missing from the reply (10.6.6).

**Superseded (10.5.4).** "`S` = 0x00 (secondary byte, presumably 0x60|S
when present -- not captured)" -- captured since, in sad_poll.pcap.

### 10.6 Errors and addressing

#### 10.6.1 Presence probe (0x02, new) and an absent device

`02 P S 00`, `S` = 0x00 or the secondary byte 0x61 (nolistener.pcap 10.8801
`02 18 61 00`). Reply 16 B: 8-byte status block id 0x02 with error 0 and
count field written to `00 00`, then `01 00 00 00` if a device at that
address answered, `00 00 00 00` if not, then `04 00 00 00` (open.pcap 0.0085
OUT / 0.0103 IN84: present, 1.8 ms; nolistener.pcap 0.0235 OUT / 0.0252
IN84: absent, 1.7 ms). What the adapter does on the bus during those 1.7 ms
is not visible in USB captures; the count field being rewritten shows a
data-type operation. For `GPIB0::5::INSTR` with nothing at 5, NI probed 50
times at 104 ms intervals (0.0235 .. 5.1099), from the second probe on each
preceded by `0c fc 00 fd 40 3f 25 04` (MTA 0, UNL, LAD 5, SDC; 49 times),
then opened the session anyway (nolistener.stdout: `viOpen` returned after
5.2 s) and wrote the bank-2 configuration for address 5 (5.1127).

#### 10.6.2 Write with no listener

`0d f9 ff fc 00 0a 08 00 *IDN?\r\n` after `40 3f 25` (nolistener.pcap
5.1138 OUT 40 B): reply block `0d 00 28 08 f9 ff ff ff` -- ibsta 0x0028,
**error 0x08**, count `f9 ff` = -7 (0 of 7 bytes), 1 ms after the OUT
(5.1149). The 0x0c before it succeeded (the instrument at 24 accepts
command bytes for any address). VISA reported `VI_ERROR_NLISTENERS`. 4.3
code 8 confirmed.

#### 10.6.3 Read from an absent device

0x0b read of 20480 with code 0xfc (5.6164): IN88 completes with 0 bytes at
9.8121, IN84 at 9.8126 with `0b 00 64 0a 00 b0 ff ff 60 00 00 00` (error
0x0a, count -20480), 4.196 s. VISA `VI_ERROR_TMO`. Repeated, with the USB
completion status, in raw_errors.pcap (10.6.6).

#### 10.6.4 Not controller in charge (error 0x07)

On an INTFC session without a prior IFC (ren.pcap): `viGpibControlREN(
VI_GPIB_REN_ASSERT_LLO)` sent REN on then `0c ff 00 fc 11 00 00 00` (LLO);
reply `0c 00 00 07 ff ff ff ff` -- ibsta 0x0000, **error 0x07**, count -1
(2.5124 / 2.5128). `viGpibControlATN` asrt / asrt_immediate / deassert /
deassert_handshake sent `01 01 00 00`, `01 00 00 00`, `06 00 00 0a`, `06 01
00 0a` and each got error 0x07 with ibsta 0x0000 and `ff ff ff ff` in bytes
4-7 (4.5180 .. 5.7229). VISA mapped all five to `VI_ERROR_NCIC`. So
`VI_ERROR_NCIC` *does* reach the wire; it is the device's error 7 for a
0x01, 0x06 or 0x0c while the adapter is not CIC. `VI_ERROR_INV_MODE` (the
deassert_gtl / asrt_address / address_gtl / asrt_address_llo modes)
produced no traffic. 4.3: code 7 = not controller in charge. The same
four modes on an instrument session are accepted and do reach the wire
(10.7.4).

#### 10.6.5 Raw write (0x0e) with no listener (raw_errors.pcap)

Session `GPIB0::5::INSTR`, nothing at 5 (opened after the 50 probes of
10.6.1, timeout 2 s -> 0xfc). A 2502-byte `viWrite` (2500 + `\r\n`):
```
5.6041 OUT   36 B   03 00 00 00 | 0c fd 00 fd 40 3f 25 00 | 0e 00 00 fc 00 0a 08 00 3a f6 ff ff | 09 01 00 02 03 01 00 00 | 04 00 00 00
5.6044 OUT06 2502 B all the data, one transfer, submitted
5.6045 IN84  submitted
5.6046 OUT   completes
5.6054 OUT06 completes, USBD status 0xc0000004                (1.0 ms after submission)
5.6057 IN84  40 B   03 00 30 00 00 00 ff ff | 0c 00 38 00 00 00 ff ff | 0e 00 28 08 3a f6 ff ff | 09 00 28 00 3a f6 ff ff 01 00 00 00 | 04 00 00 00
5.6057 URB function 0x001e on 0x06, completes 5.6063, status 0
5.6064 URB function 0x001e on 0x02, completes 5.6069, status 0
```
- NI submits the data on 0x06 straight after the header, without waiting
  for anything, exactly as on the success path (10.5.2).
- The adapter refuses the data at the USB level: the 0x06 transfer fails
  with 0xc0000004, the Windows USBD status for a STALL handshake
  (USBD_STATUS_STALL_PID in Microsoft's public usb.h). It is neither
  cancelled by the host nor completed short. How many of the 2502 bytes
  crossed the bus before the STALL is not recorded (USBPcap gives length
  0 on every OUT completion, successful ones included).
- The reply on 0x84 arrives by itself, 1.6 ms after the header, complete
  and ordinary in shape. The 0x0e status block reads ibsta 0x0028, **error
  0x08**, count `3a f6 ff ff` = -2502 (nothing transferred), the same
  error as the framed write (10.6.2); the 0x09 block behind it still
  executed (1 write). The 0x0c before it succeeded, as in 10.6.2.
- Function 0x001e is what usbpcap_dump.py labels `RESET_PIPE`, the Windows
  request that clears a halted endpoint. NI issues it on 0x06, then on
  0x02. 0x02 had reported no error; why it is reset as well is not
  established. Only the URBs are in the pcap; no control transfer (the
  CLEAR_FEATURE such a request stands for) was recorded, and no 0x20.
- VISA returned `VI_ERROR_NLISTENERS`.

#### 10.6.6 Raw read (0x0b) and serial poll (0x10) of an absent device

`viRead(20480)` on the same session:
```
 6.1075 OUT  40 B   03 00 00 00 | 0c fd 00 fd 3f 20 45 00 | 0b 00 0a fc 00 b0 ff ff | 09 01 00 01 0a 55 00 00 | 09 01 00 02 03 01 00 00 | 04 00 00 00
 6.1077 IN88 submitted
 6.1078 IN84 submitted
10.3029 IN88 completes, 0 B, USBD status 0
10.3034 IN84 56 B   03 00 28 00 3a f6 ff ff | 0c 00 74 00 00 00 00 00 | 0b 00 64 0a 00 b0 ff ff 60 00 00 00 | 09 00 64 00 00 b0 ff ff 01 00 00 00 | 09 00 64 00 00 b0 ff ff 01 00 00 00 | 04 00 00 00
```
After 4.195 s (code 0xfc) the adapter itself ends the pending 0x88
transfer with a zero-length packet -- it completes successfully with zero
bytes; the host did not cancel it and it did not time out at the USB
level -- and 0.5 ms later sends the reply: error 0x0a, count -20480,
tail 0x60. No STALL, no function 0x001e, no 0x20. The same as
nolistener.pcap 9.8121 / 9.8126 (10.6.3). The leading 0x03 block still
shows the failed write's count.

`viReadSTB` on the same session:
```
10.8046 OUT  24 B   03 00 00 00 | 10 01 00 00 05 00 fc 00 | 09 01 00 02 03 01 00 00 | 04 00 00 00
10.8046 IN84 submitted                                    (nothing on 0x88)
15.0003 IN84 32 B   03 00 64 00 00 b0 ff ff | 39 00 74 0a 00 00 00 00 | 09 00 74 00 00 00 00 00 01 00 00 00 | 04 00 00 00
```
The reply is 32 bytes, not the 36 of 10.5.4: **the `3a P S sb` block is
absent when the poll fails.** Only the status block with id 0x39 is sent:
ibsta 0x0074, error 0x0a, count `00 00 00 00`. The 0x10 reply is thus 12
bytes on success and 8 on failure; a parser must treat the 0x3a block as
optional (it is there exactly when the byte after the preceding block is
0x3a). The wait was 4.1955 s under code 0xfc, the same expiry as a read
(10.1.8). VISA returned `VI_ERROR_TMO`.

#### 10.6.7 Ordering and recovery on the error paths

From 10.6.5 and 10.6.6, with the success paths of 10.1.1 and 10.5.2:

- The order of submission never varies: the instruction message on 0x02,
  then the raw transfer (all the data on 0x06, or the IN on 0x88), then
  the IN on 0x84 for the reply -- all three within 0.6 ms and before
  anything has come back. NI never waits for a reply before starting the
  raw transfer.
- The adapter ends the raw transfer itself in each failure seen: a STALL
  on 0x06 for a write that cannot start, a zero-length IN on 0x88 for a
  read that got nothing. No host-side cancellation was needed and none
  was recorded (no URB function 0x0002, which usbpcap_dump.py labels
  `ABORT_PIPE`, in any of the 28 pcaps).
- The reply on 0x84 always comes, with every block of the message
  answered, and carries the error code and the count. Only the 0x3a block
  of a failed poll is dropped.
- Recovery: after the STALL, the halted-endpoint reset on 0x06 and on
  0x02; after the two timeouts, nothing. In no case a stop request 0x20,
  a drain read on 0x84 / 0x88, the 88-byte initialisation or an IFC.
- The next operations were ordinary and worked: the read 0.5 s after the
  failed write was accepted and answered at the USB level (6.1075); after
  the failed poll NI closed the session (15.5018 OUT 20 B, 10.3.3), opened
  `GPIB0::24::INSTR` in the same process with the different-address
  sequence of 10.3.4 (16.0056 ..: 3-register read `34 00 74 a1`, 32-byte
  configuration, probe `02 18 00 00` -> present with ibsta 0x0070, 28-byte
  timeout update) and `*IDN?` returned its 82 bytes (16.0208 OUT 40 B,
  16.0228 OUT 40 B, 16.0343 IN88 82 B, 16.0350 IN84 56 B).

Not shown by these captures: whether 0x06 is halted when the host has not
submitted any data by the time the 0x0e fails (NI always had); what the
0x06 transfer and the count do when a write fails part-way (a listener
that stops accepting); whether the adapter also completes a pending 0x88
transfer for read errors other than the timeout; and whether the halt on
0x06 would clear without the reset.

### 10.7 Board level (INTFC sessions)

#### 10.7.1 Line operations

| VISA | Wire | Compare |
|------|------|---------|
| `viGpibSendIFC` | `0f 00 00 00` (intfc.pcap 0.5049), reply ibsta 0x0030 | 5.5 agrees |
| `viGpibControlREN(ASSERT)` | `08 03 01 0d 01 0c 01 1f \| 09 01 00 01 0a 1f 00 00` (1.0066 OUT 20 B) | 5.6 write agrees; a 3-register read precedes it in the same message |
| `viGpibControlREN(DEASSERT)` | same with `0a 17` (ren.pcap 0.5052) | 5.6 agrees |
| `viGpibControlATN(ASSERT)` | `01 01 00 00` (intfc.pcap 1.5079), reply ibsta 0x0030 | 5.4 synchronous agrees |
| `viGpibControlATN(ASSERT_IMMEDIATE)` | `01 00 00 00` (ren.pcap 4.9198) | 5.4 asynchronous |
| `viGpibControlATN(DEASSERT)` | **`06 00 00 0a`** (intfc.pcap 2.0094), reply ibsta 0x0020 (ATN clear) | 5.4 has `06 00 00 00`; byte 3 = 0x0a in all three 0x06 captured; meaning not established |
| `viGpibControlATN(DEASSERT_HANDSHAKE)` | `06 01 00 0a` (ren.pcap 5.7225) | byte 1 = 1 for the handshake variant |
| `viGpibCommand(UNL UNT)` | `0c fe 00 fc 3f 5f 00 00` (intfc.pcap 2.5110), reply ibsta 0x0030, count 0 | 5.3 agrees; timeout byte = session timeout (2 s -> 0xfc) |

The 0x01 and 0x06 replies carried the stale count from the last data
operation (`52 b0 ff ff`), not `aa 55` (intfc.pcap 1.5087, 2.0101).

#### 10.7.2 Board-level write and read (board_io.pcap)

`viGpibCommand(UNL LAD24 MTA0)` = `0c fd 00 fc 3f 38 40 00` (1.0054, reply
ibsta 0x0038); board `viWrite("*IDN?\n")` = a bare `0d fa ff fc 00 0a 08
00 2a 49 44 4e 3f 0a 00 00 04..` (1.3071 OUT 20 B) with no 0x03, 0x0c or
bank-2 block, reply 12 B ibsta 0x0028, count 0; `viGpibCommand(UNL TAD24
MLA0)` = `3f 58 20` (1.6090, reply ibsta 0x0034 = CIC|ATN|LACS); board
`viRead(200)` = a bare `0a 00 0a fc 38 ff 00 00 04..` (1.9105 OUT 12 B),
reply after 4.195 s `38 00 24 0a 38 ff ff ff 60 1e 00 00 04..` (6.1058
IN84 16 B): error 0x0a, ibsta 0x0024 = CIC|LACS, ATN clear, 0 bytes.

So ATN was released and the read timed out with the instrument addressed
to talk. The wire does not show why the 2420 did not talk. Differences
from the INSTR-session read that worked: REN was never asserted in this
session (no AUXMR 0x1f; BSR never read), no bank-2 0x04..0x07 were written,
the talk-addressing order was UNL TAD MLA rather than UNL MLA TAD, and no
AUXMR 0x55 was sent. The shutdown reply of this session read ibsta 0x1010
(SRQI, ATN) instead of 0xffff (9.6195), i.e. the instrument was asserting
SRQ afterwards.

#### 10.7.3 viTerminate and VISA errors that never reach the wire

`viTerminate` from another thread 1.5 s into a 0x0b chunk produced no USB
traffic of any kind -- no control request 0x20, nothing on 0x02 -- and the
read completed normally 2.5 s later (terminate.pcap: OUT 0.5160, IN88
4.5146, nothing in between; all four chunks and the END chunk as in
trac.pcap). `VI_ERROR_INV_MODE` likewise produced nothing (10.6.4).

#### 10.7.4 viGpibControlREN on an instrument session (ren_device.pcap)

Session `GPIB0::24::INSTR`, REN already asserted by the open (10.3.2). "REN
message" below is `08 03 01 0d 01 0c 01 1f | 09 01 00 01 0a vv 00 00 | 04
00 00 00` (20 B, reply 24 B) with `vv` = 0x1f to assert, 0x17 to
deassert, as in 10.7.1.

| VISA mode (value) | Wire, in order | Replies |
|-------------------|----------------|---------|
| `VI_GPIB_REN_ASSERT_ADDRESS` (3) | REN message 0x1f (0.5143 OUT 20 B); probe `02 18 00 00 \| 04` (0.5154 OUT 8 B) | BSR read ahead of the write 0xa1; probe: present, ibsta 0x0030 |
| `VI_GPIB_REN_ASSERT_LLO` (4) | REN message 0x1f (0.9181); bare `0c ff 00 fd 11 00 00 00 \| 04` (0.9192 OUT 12 B) = LLO | `0c 00 b0 00 00 00 ff ff`: error 0, count 0, ibsta 0x00b0 = LOK, CIC, ATN |
| `VI_GPIB_REN_ASSERT_ADDRESS_LLO` (5) | REN message 0x1f (1.3210); probe `02 18 00 00` (1.3221); LLO message (1.3241) | ibsta 0x00b0 throughout |
| `VI_GPIB_REN_ADDRESS_GTL` (6) | `03 \| 0c fc 00 fd 40 3f 38 01 \| 09 01 00 02 03 01 00 00 \| 04` (1.7255 OUT 24 B) = MTA 0, UNL, LAD 24, GTL | 32 B; 0x0c ibsta 0x00b8 = LOK, CIC, ATN, TACS |
| `VI_GPIB_REN_DEASSERT_GTL` (2) | the same GTL message (2.1276 OUT 24 B); REN message 0x17 (2.1290 OUT 20 B) | BSR ahead of the write 0xa1; the 0x09 reply has ibsta 0x0038: LOK gone with REN |
| `VI_GPIB_REN_ASSERT` (1) | REN message 0x1f (2.5304) | BSR ahead of the write 0xa0 (REN was off) |

- Every one of the six modes produced traffic and none returned an error;
  no mode is a no-op on an instrument session. On a board session the
  four addressed modes produce nothing and return `VI_ERROR_INV_MODE`
  (10.6.4). `VI_GPIB_REN_DEASSERT` (0) was not exercised here; on the
  board it is the REN message 0x17 (10.7.1).
- The REN register write is sent even when REN is already asserted.
- "Address" in ASSERT_ADDRESS is the 0x02 probe of the session's address,
  not a 0x0c with a listen address. What 0x02 does on the bus is not
  visible over USB (10.6.1); that NI uses it here says it addresses the
  device to listen, which for a device with REN true means remote state.
  With a secondary address the probe would carry it (`02 18 61 00`,
  10.5.3); not captured in this mode.
- The LLO message is a lone 0x0c block with timeout byte 0xfd: no 0x03 in
  front, no bank-2 0x03 write behind. On the board session the same block
  carried 0xfc and got error 7 (10.6.4). The GTL message has the full
  INSTR wrapping, like SDC and GET (10.5.3).
- ibsta LOK (0x0080) is set in every reply from the LLO until REN is
  deasserted; this is the only capture in which LOK appears.
- The `*IDN?` that followed ran as usual (2.9318 OUT 40 B, 2.9346 OUT 40
  B, 2.9494 IN88 82 B).

### 10.8 Status block fields settled or observed

- **Bytes 6-7 of the status block.** `ff ff` in every reply of a session
  until the first 0x0b or 0x0e executes (all replies in write.pcap,
  eosmodes.pcap, partial.pcap, none of which use 0x0b). In a 0x0b or 0x0e
  reply they are the high half of the 32-bit count (`00 00` for count 0,
  trac.pcap 4.5344, longwrite.pcap 2.0354; `ff ff` for a negative count).
  Replies to later 0x0c / 0x0d carry `00 00` once a 0x0b has run (counts.pcap
  13.6452, trac.pcap 13.0037) even though before the first 0x0b they carried
  `ff ff` with the same 16-bit count 0. A parser should read bytes 4-5 as
  the 16-bit count for 0x0a/0x0c/0x0d and bytes 4-7 as the 32-bit count for
  0x0b/0x0e, and otherwise ignore 6-7.
- **0x35 block count.** `35 03` after 3 reads, `35 02` after 2 (srq.pcap
  2.0333): k = the number of registers read, at least for k <= 3. The third
  value byte of a 0x34 chunk holding only 2 values was 0x19, a stale byte.
- **Register-read + register-write in one message** works and replies in
  order without a termination block between them (10.3.2, 0.0020).
- **Count field after 0x01 / 0x06 / 0x0f** is the stale value of the last
  data operation on this unit (`52 b0`, `02 b0`, `fd ff`, `00 00`), not
  the `aa 55` the bench unit showed (4.1); after a 0x02 probe it is `00
  00`. `aa 55` did appear once on this unit, in the first reply of
  read_thresholds.pcap (10.3.1).
- **ibsta after chip reset** (shutdown reply) is not stable: 0xffff in 25
  captures, 0x0000 in two, 0x1010 in one (10.3.3). Do not test it.
- **Readiness (0x40), serial-number (0x41), stop (0x20) and monitor-mask
  (0x21 / 0x0300) control requests** appear in none of the captures; each
  capture starts after the driver had already owned the adapter, so these
  belong, if anywhere, to device start. The stop request in particular is
  absent from all three failures of raw_errors.pcap (10.6.7): NI does not
  use it to end a failed or timed-out instruction.
- **0x10 reply.** `3a P S sb` + status block id 0x39 on success (`S`
  echoed: `3a 18 61 04`, sad_poll.pcap 0.5170); the status block alone,
  with the error code, on failure (raw_errors.pcap 15.0003).
- **USB completion of raw transfers.** 0x88: success with the data, or
  success with zero bytes when nothing was read. 0x06: success, or STALL
  when the write could not start (raw_errors.pcap 5.6054). These are the
  only completions in the 28 pcaps; none was cancelled or timed out by
  the host.

### 10.9 What an implementer must do to interoperate (from this section)

Everything below is **[captured]** behaviour of NI's driver: it says how
to match NI, under NI's initialisation. The framed path of sections 5
and 6 is the one with **[bench]** evidence; "How to read this document"
says what has not been mixed.

- Use 0x0b with data on 0x88 for large reads and 0x0e with data on 0x06
  for large writes when matching NI's behaviour; the framed 0x0a / 0x0d
  paths remain valid for the counts NI uses them for. Take the byte count
  of a 0x0b read from its reply, not from the 0x88 transfer, which is
  padded to an even length (10.1.3).
- NI's thresholds, to match it exactly: read count <= 1024 -> 0x0a, >=
  1025 -> 0x0b (10.1.1); write length <= 2048 -> 0x0d, >= 2049 -> 0x0e
  (10.5.2). A framed 0x0d of any length up to 2048 is the plain 5.1 layout
  padded to 4; a message longer than one USB transfer must reach the
  adapter as one unbroken run of packets (10.5.2). Raw write data is not
  padded.
- For 0x0b and 0x0e keep NI's order: send the instruction message, start
  the raw transfer at once, then read the reply on 0x84. Do not wait for
  the reply before starting the raw transfer -- the reply to a successful
  0x0e comes only after the data has gone out -- and do not skip the
  reply when the raw transfer fails (10.6.7).
- When the 0x06 transfer of a 0x0e fails with a STALL (a pipe error in
  libusb terms), the instruction has failed: read the reply for the error
  code (0x08 for no listener) and count, then clear the halt on 0x06
  before the next raw write. NI also resets 0x02 (10.6.5).
- When a 0x0b gets nothing, the 0x88 transfer completes with zero bytes
  at the device timeout, just before the reply with error 0x0a. Give the
  0x88 read the same host wait as the reply (7.2) rather than cancelling
  it early; no stop request, drain or re-initialisation follows (10.6.6).
- Parse the 0x10 reply with the 0x3a block optional: absent on failure
  (10.6.6). With a secondary address send `S` = 0x60 | s in 0x10 and 0x02
  and `60+s` right after the listen / talk address in every 0x0c,
  including SDC, GET (10.5.3, 10.5.4).
- `viGpibControlREN` on an instrument session, as NI does it (10.7.4):
  ASSERT = REN write; ASSERT_ADDRESS = REN write + 0x02 probe; ASSERT_LLO
  = REN write + `0c .. 11`; ASSERT_ADDRESS_LLO = REN write + probe + LLO;
  ADDRESS_GTL = `40+C 3f 20+N 01`; DEASSERT_GTL = that, then REN off.
- Do not test the ibsta of the shutdown reply (10.3.3).
- Accept `e` != 0 with `m` = 0 in reads (NI does it on every read); do not
  rely on error 4 for that combination.
- Parse replies block by block using the per-block lengths of 10.2.1,
  skipping `11 00 00 00` blocks; do not assume fixed total lengths beyond
  the single-block cases of 3.5. Behind a 0x0a, accept a following 0x09
  block's status or its absence (5.2).
- Expect error 7 for 0x01 / 0x06 / 0x0c while not CIC, error 8 for a write
  with no listener, and error 0x0a for a device timeout.
- The adapter's wait under a timeout code is the measured expiry of 7.3,
  not the nominal value of 7.1: 0.132 s for 0xf9, 0.264 s for 0xfa
  (shorter than its nominal 300 ms), 1.050 s for 0xfb, 4.196 s for 0xfc,
  16.778 s for 0xfd, 33.555 s for 0xfe. The host wait for an
  instruction's reply, and for the 0x88 transfer of a 0x0b, must be
  longer than the expiry of the code sent (7.2 recommends expiry + 2 s,
  summed over the timed blocks of the message); a wait derived from the
  nominal value is too short for 0xfd (15 s against 16.78 s) and makes
  the host abandon an instruction the adapter is about to end itself.
  An application that needs "at least N ms" must not pick 0xfa for
  N = 300.
- The interrupt push on SRQ is 8 bytes `30 18 00 sb ..` with the status
  byte at offset 3; the adapter polls the device itself and releases SRQ,
  so a later explicit serial poll returns the status byte with RQS clear.

---

## 11. Open questions for the bench

Every "not established", "uncertain", "not captured" and "not measured"
in this document, in one list. Nothing here is new: each item points
back to where the text leaves it open. Ordered by how much an
implementer's choice depends on the answer; the first group decides
what to send, the last is curiosity.

### 11.1 Decide which bytes to send

- [ ] **Do NI's paths work from this project's driver at all?** 0x0b,
  0x0e, 0x10, 0x02, 0x03, batched messages and the interrupt push are
  [captured] only; none has run on the bench ("How to read this
  document"; the clean-room record lists the first runs to make: reads of
  1024 and 1025 bytes, a chunked `:TRAC:DATA?`, writes of 2048 and 2049
  bytes, a long write to an empty address followed by a normal query, a
  read and a serial poll that time out, the REN modes, SRQ on `*OPC`).
- [ ] **Mixtures nobody has seen.** 0x06 followed by 0x0b or 0x10; NI's
  batched 0x0c + 0x0a under AUXRA 0x81; the two-write block behind a 0x0a
  under AUXRA 0x99; 5.18 rows taken from different columns (section 5 ATN
  rule, 5.2, 2.6, 5.18).
- [ ] **Error 4 for `e` != 0 with `m` = 0.** Never returned in 100
  captured reads under AUXRA 0x99; whether it is returned under 0x81 is
  not established (4.3, 5.2, 8.3, §10.1.6). With it: what rows 3 and 4 of
  the initialisation (0x81 against 0x99: XEOS, BIN) change on the wire
  (2.6), and whether the BIN bit in write #3 has any effect or ever needs
  re-sending (5.2).
- [ ] **Are the bank-2 0x03..0x07 writes required for anything?** The
  bench's framed paths ran without them; NI never omits them. Meaning of
  0x03 and 0x04 not established (8.15, 2.8, §10.2.4).
- [ ] **Does the interrupt push need the monitor mask?** No mask request
  in any capture, but every capture starts after NI's driver owned the
  adapter (2.5, 2.8 steps 4 and 6, §10.4.1). With it: the function of
  control request 0x3b (2.2, §10.4.2); bytes 4-7 of the push (2.5);
  whether pushes for bits other than SRQ look the same (2.5); whether
  SRQI ever shows in a 0x21 reply while SRQ is held (5.12, §10.4.4).
- [ ] **What the timeout code bounds.** The whole instruction, or an
  interval inside it: the longest error-free instruction under 0xfc ran
  3.999 s, below that code's measured expiry of 4.194 s, so the captures
  do not say (7.1, 7.3, §10.1.8). The host wait of 7.2 for a long
  transfer depends on it. What a 0x0c does at its expiry was not seen
  either (5.3).
- [ ] **Expiry of the codes not measured**: 0xf1-0xf8, 0xff, 0x01, 0x02;
  whether 0x01 / 0x02 follow the power-of-two pattern at all; the expiry
  of a 0x0c, 0x0d or 0x0e under any code; any adapter other than this
  GPIB-USB-HS; two timed blocks of one message both expiring (7.2, 7.3).
- [ ] **Width of the 0x0b / 0x0e count field**: 32 bits, or 16 bits
  followed by `ff ff`; no count above 0xffff was captured (3.3, §10.1.2).

### 11.2 Decide how to parse and recover

- [ ] **The reply behind a 0x0a.** On the bench the two-write 0x09 block
  behind the 0x0a drew no status (trailer 16 bytes); in the captures the
  one-write 0x09 block behind NI's 0x0a drew its 12-byte status (12 + 12
  + 4 = 28). Why the two differ is not established (5.2, 8.6, §10.1.5).
  Also: whether one reply ever mixes 0x36 and 0x37 blocks (5.2); what the
  four `11 00 00 00` blocks mean (§10.1.5).
- [ ] **Raw-path failures not captured** (§10.6.7): whether 0x06 is
  halted when the host has submitted no data by the time the 0x0e fails;
  what the 0x06 transfer and the count do when a write fails part-way;
  whether the adapter completes a pending 0x88 transfer for read errors
  other than the timeout; whether the halt on 0x06 clears without the
  reset; why NI resets 0x02 as well (§10.6.5). How many bytes crossed
  before the STALL is not recorded (§10.6.5).
- [ ] **Does a message whose length is a multiple of 512 need a
  zero-length packet?** None was captured (§10.5.2). Likewise whether the
  device ends a 20480-byte 0x88 transfer with one (§10.1.4).
- [ ] **The stop request 0x20** and error code 1: [inherited] only; NI
  never sends it, not even after a failure (5.11, 4.3, 8.2, §10.6.7,
  §10.7.3). Its effect on a pending instruction has not been seen.
- [ ] **When error 2 is returned**, if ever: not by a read behind a 0x0c,
  nor by a bare 0x0a with ATN set (4.3, 8.13). Errors 3 and 5 likewise
  [inherited] only (4.3).
- [ ] **0x35 count `k` for more than 3 register reads**, and the padding
  bytes of a partial 0x34 chunk (3.5).
- [ ] **What 0x02 does on the bus** during its 1.7 ms (§10.6.1, §10.7.4);
  the probe with a secondary address in ASSERT_ADDRESS mode was not
  captured (§10.7.4).
- [ ] **Why the board-level read of board_io.pcap timed out** with the
  instrument addressed to talk (§10.7.2).
- [ ] **Why unit 01CEE482 expires at 1.25 x {0.1, 0.3, 1, 3, 16, 33} s
  and unit 013CC9DF at powers of two in microseconds** (7.3): the unit,
  or this driver's message? Send NI's exact 0x0a bytes from this driver
  to 01CEE482, or this driver's bytes to 013CC9DF.
- [ ] **A framed 0x0a with count 20480 whose answer was longer than the
  count wedged unit 01CEE482** **[bench, 2026-09-21]**: `:TRAC:DATA?` of
  500 x 5 elements (about 35 000 bytes) under code 0xfb drew no reply in
  23.8 s, the stop request 0x20 then timed out on EP0, every later
  control request timed out (`no langid`), and only unplugging cleared
  it. NI never sends a framed read above 1024 bytes (§10.1.8: 1025 and
  above go by 0x0b), so the framed path above 1024 is unobserved on the
  wire. Not established: whether the trigger is the count above 1024,
  the answer outrunning the count, or the timeout expiring mid-transfer.
  The same 20480-byte framed read of a short answer (`*IDN?`, 82 bytes)
  works on every attempt. Bracketed after a replug with `:TRAC:DATA?`
  answers under code 0xfc: 700, 980, 1050, 1400, 2100 and 4200 bytes all
  arrived, each in one reply (4200 bytes = 140 0x37 blocks, 4496-byte
  reply, 0.757 s); a 7000-byte answer wedged the adapter the same way,
  with nothing at all on 0x84. So the trigger is not the count above
  1024 and not the answer outrunning the count (every answer was shorter
  than 20480); the framed reply has a ceiling between 4496 and about
  7500 bytes, and an answer above it is fatal rather than truncated.

### 11.3 Bytes whose meaning is unknown but which can be copied

- [ ] Byte 3 = 0x0a of NI's 0x06 (5.4, §10.7.1).
- [ ] Byte `x` (0x00 / 0x01) of 0x10 (5.9, §10.5.4).
- [ ] Byte 5 (`e`) of 0x0d and 0x0e: whether it has any effect; `f` = 0x00
  and other `e` values on 0x0e were not observed (5.1, §10.5.1, §10.5.2).
- [ ] Why NI's addressing 0x0c carried 0xfc instead of 0xfd after an INTFC
  session had been opened and closed in the same process (§10.1.9).
- [ ] Bank-1 0x0c reading as the low byte of ibsta (3.4, §10.3.2); the
  write to bank-1 offset 0x06 in the initialisation, note (a) of 2.6;
  bank 3 addr 0x10, bank 2 addrs 0x00-0x02, bank 1 addr 0x0f (8.15); the
  one `aa 55` in the captures (§10.3.1).
- [ ] `VI_GPIB_REN_DEASSERT` on an instrument session was not exercised
  (§10.7.4).

### 11.4 Other models and rarely used operations ([inherited] throughout)

- [ ] What the alternate endpoints of the USB-B and HS+ carry (1.2); the
  readiness-byte attributions to the 0x725c model (2.3); everything HS+
  and USB-B in 2.4.
- [ ] Parallel poll: only timeout code 0xf0 observed; termination block
  after its reply uncertain (3.5, 5.10).
- [ ] Pass control: how the adapter reports losing CIC (5.17). Neither it
  nor the universal device clear (DCL, 5.8) was captured.
- [ ] Take / release system control (5.7), changing the adapter's own
  address (5.14), AUXMR 0x05 (5.15): never seen on a wire.
- [ ] The cause of the hung adapter of 8.17 (8.18 offers a conjecture).

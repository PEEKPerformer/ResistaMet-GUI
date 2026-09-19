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
- "Observed" means seen on real hardware by at least one of the source
  implementations; "uncertain" marks facts the sources do not settle.

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

### 1.2 Endpoints

All bulk endpoints carry the framed messages of section 3. Addresses are
given as raw endpoint numbers and as libusb addresses (IN = 0x80 | n).

| Model  | Bulk OUT | Bulk IN     | Interrupt IN | Alternate endpoints (present, unused) |
|--------|----------|-------------|--------------|----------------------------------------|
| USB-B  | 0x02     | 0x02 (0x82) | 0x04 (0x84)  | bulk IN 0x06 (0x86) |
| HS, KUSB-488A, MC USB-488 | 0x02 | 0x04 (0x84) | 0x01 (0x81) | bulk OUT 0x06, bulk IN 0x08 (0x88) |
| HS+    | 0x01     | 0x02 (0x82) | 0x03 (0x83)  | bulk OUT 0x04, bulk IN 0x05 (0x85) |

Only the primary bulk OUT and bulk IN endpoints are required (on the HS:
0x02 and 0x84). The interrupt endpoint is optional (2.5). Uncertain: the
purpose of the alternate endpoints. Observed 2026-09-19 (§10.1.3, §10.5.2):
on the HS the alternate pair carries raw data -- bulk IN 0x88 the bytes
read by a 0x0b instruction, bulk OUT 0x06 the bytes written by a 0x0e
instruction -- while the instruction and its reply stay on 0x02 / 0x84.
NI's driver uses them for every read above 1024 bytes and for a 2050-byte
write. Settled the same day (§10.1.1, §10.5.2): reads of 1025 bytes and
more, writes of 2049 bytes and more. A write that cannot start is refused
with a STALL on 0x06; a read that gets nothing ends with a zero-length
transfer on 0x88 (§10.6.5, §10.6.6).

Reply sizes are bounded: the largest single reply this protocol produces
is the reply to a maximum-length (65535-byte) read, about 70 KB (see 5.2);
everything else is well under 64 bytes for the register counts used here.
Ordinary synchronous bulk reads with a correctly sized buffer suffice.

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

### 2.2 Control transfers (all models unless stated)

All are device-to-host, vendor class. bmRequestType 0xC0 = IN | Vendor |
Device; 0xC1 = IN | Vendor | Interface. Each response begins with a byte
echoing bRequest.

| bRequest | bmRequestType | wValue | wIndex | wLength | Purpose | Response |
|----------|---------------|--------|--------|---------|---------|----------|
| 0x41 | 0xC0 | 0x0000 | 0x0000 | 16 | Serial-number query | byte 0 = 0x41; bytes 1..4 = 32-bit serial, little-endian. Response length 5 (HS) or 16 with zero padding (HS+). |
| 0x40 | 0xC0 | 0x0000 | 0x0000 | 16 | Readiness query | 11 significant bytes, see 2.3. |
| 0x20 | 0xC0 | 0x0000 | 0x0000 | 8  | Stop / abort the in-flight bulk operation | 8-byte status block (section 4). |
| 0x21 | 0xC0 | 0x0200 | 0x0000 | 8  | Status query | 8-byte status block with current ibsta. |
| 0x21 | 0xC0 | 0x0300 | mask   | 8  | Set interrupt-monitor mask (2.5) | 8-byte status block. |
| 0x48 | 0xC0 | 0x0000 | 0x0000 | 16 | HS+ only, extra init | expected `48 f3 30 00 00 00 00 00 00 00 00 00 00 00 00 00` |
| 0x4b | 0xC0 | 0x0001 | 0x0000 | 2  | HS+ only, LED to steady | expected `4b 00` |
| 0xf8 | 0xC1 | 0x0000 | 0x0001 | 9  | HS+ only, extra init | expected `f8 01 00 00 00 01 00 00 00` |

A 1000 ms host timeout suffices for all of these (100 ms per readiness
query attempt).

Observed 2026-09-19 (§10.3.5, §10.4.2): the 0x21 / 0x0200 status query
returns `21 ss ss ee cc cc xx xx`, a status block (4.1) with id 0x21, and
NI uses it to read line states without bulk traffic. One host-to-device
request not in the table exists: bmRequestType 0x40, bRequest 0x3b,
wValue 0, wIndex 0, wLength 0, sent once after every interrupt-IN push
and before the interrupt read is re-armed; its function is not
established. The 0x40, 0x41, 0x20 and 0x21 / 0x0300 requests do not
appear in the captures, which all start after the driver already owned
the adapter.

### 2.3 Readiness poll (HS, HS+, KUSB-488A, MC USB-488; not USB-B)

1. Send the serial-number query (0x41). Verify byte 0 == 0x41. The value
   is informational.
2. Send the readiness query (0x40) repeatedly, up to 50 attempts, 100 ms
   apart, 100 ms USB timeout per attempt, until the device reports ready.

Response layout of the readiness query. The "accepted" column lists every
value observed on real hardware; other values are unexpected but not
fatal. Model annotations are given where a value is known to be
model-specific; attributions to the 0x725c model are uncertain (the
observations name a "488A" unit without its PID).

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

Ready condition: any of bytes 6, 7, 10 is nonzero. Before the device is
ready all three are 0x00. A single readiness query without the loop has
been observed to succeed on an already-settled HS; the loop is the
normative form (2.8).

Observed on GPIB-USB-HS 01CEE482 (bcdDevice 0x101), 2026-09-18: the reply
is `40 01 00 01 30 01 19 08 00 00 65` (11 bytes), i.e. byte 6 = 0x19,
byte 7 = 0x08, byte 9 = 0x00, byte 10 = 0x65, immediately after a USB
reset and unchanged afterwards. These bytes therefore vary between units
or firmware versions (0x65 = 101 matches the 1.01 of bcdDevice); only
"nonzero" carries meaning. The serial-number reply was `41 82 e4 ce 01`
(5 bytes), decoding to 0x01CEE482 as described.

### 2.4 Model-specific extra initialisation

- **HS+**: after the readiness poll, issue the three control requests
  0x48, 0x4b, 0xf8 from the table in 2.2, in that order. The sequence was
  captured from NI's Windows driver; GPIB traffic works without it, and
  its only visible effect is that 0x4b changes the LED from blinking to
  steady. The 0xf8 request is the only interface-recipient (0xC1) request
  in the protocol and carries wIndex = 1.
- **USB-B**: no readiness poll exists. Instead the serial number is read
  with a register-read bulk instruction (3.4) of bank 3 addresses 0x0b,
  0x0a, 0x09, 0x08, which yield serial bytes 0 (LSB) to 3 (MSB). See 3.5
  for the four-register reply layout.

### 2.5 Interrupt-monitor mask (optional)

Control request 0x21 with wValue 0x0300 and wIndex = mask selects which
ibsta bits, when they become set, cause the device to push a status block
on the interrupt IN endpoint. Each push is one 8-byte status block; read
the interrupt endpoint with a buffer of at least its wMaxPacketSize. The
full mask is 0x10ff = bit 12 (SRQI) plus bits 0-7 (DCAS, DTAS, LACS,
TACS, ATN, CIC, REM, LOK).

Operation without this request and without the interrupt endpoint has
been observed to be reliable; the mask is only useful for event-driven
SRQ waits. Placement in the attach sequence: 2.8.

Observed 2026-09-19 (§10.4.2): with NI's driver the adapter pushed one
8-byte packet on the interrupt endpoint when the instrument asserted SRQ,
`30 18 00 60 31 a1 01 00`: bytes 1-2 = ibsta 0x1800 (SRQI, RQS), byte 3 =
the instrument's status byte (0x60) -- the adapter had serial-polled the
device itself and SRQ was released -- and bytes 4-7 not established. No
0x21 / 0x0300 request preceded it in any capture. NI answers each push
with control request 0x3b and re-arms a 64-byte interrupt read.

### 2.6 Register initialisation sequence

Sent as ONE register-write bulk instruction (3.4) containing all 26 writes
below, in this order. `P` = the adapter's own primary GPIB address
(0..30, normally 0). Column "bank" is the register-bank selector byte.

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

Observed 2026-09-19 (§10.3.1): NI-488.2 22.5 sends exactly these 26
writes in this order, byte-identical in 22 captures, with one value
difference: rows 3 and 4 carry 0x99 (AUXRA: holdoff-all, XEOS, BIN)
instead of 0x81 / 0x91. Rows 12-14 were the 500 ns T1 row (0xe9, 0xa4,
0x00), row 16 = 0x03, rows 18-22 for address 0 without secondary. The
reply was `09 00 00 00 cc cc ff ff 1a 00 00 00 04 00 00 00` as expected.
Bank 2 has further registers 0x03..0x07 that NI writes per session
(§10.2.4).

Note (a): uncertain purpose. In the one-chip / 7210 register map, write
offset 0x06 is SPMR (serial poll mode). In NI's Turbo+9914 map the
auxiliary command register (AUXCR) sits at 0x0a when the KEYREG SWAP bit
is set and at 0x06 when SWAP is clear, so this may be intended as a
9914-mode AUXCR write for a chip that is not in that mode. It is present
in NI's own driver traffic; replicate it verbatim.

EOSR (bank 1, addr 0x0e) is never written by this protocol; the EOS
character travels only inside each read instruction (5.2).

Expected reply: the 16-byte register-write reply (3.5) with error code 0
and "writes completed" == 26.

### 2.7 T1 delay rows (#12, #13, #14 above)

| Requested T1 | #12 AUXMR (AUXRI base 0xe0) | #13 AUXMR (AUXRB base 0xa0) | #14 KEYREG | Resulting T1 |
|--------------|------|------|------|--------|
| <= 350 ns    | 0xe9 (USTD+SISB) | 0xa4 (TRI) | 0x20 (MSTD) | 350 ns |
| <= 500 ns    | 0xe9 | 0xa4 | 0x00 | 500 ns |
| <= 1100 ns   | 0xe9 | 0xa0 | 0x00 | 1100 ns |
| > 1100 ns    | 0xe1 (SISB only) | 0xa0 | 0x00 | 2000 ns |

Both the 500 ns row and the 2000 ns row have been observed to work. 2000
ns is the IEEE-488.1 default and the safe choice for long cables. These
three writes can be re-sent later on their own to change T1.

### 2.8 Attach sequence (normative)

1. Claim interface 0 (2.1); set-configuration may be skipped if the device
   is already configured.
2. Readiness poll per 2.3 (all models except the USB-B).
3. Model extras per 2.4 (HS+ control requests; USB-B serial read).
4. Optional: set the interrupt-monitor mask to 0x0000 (2.5).
5. The 26-write register initialisation of 2.6; verify the reply (error 0,
   26 writes completed).
6. Optional: set the interrupt-monitor mask to 0x10ff (2.5). Steps 4 and 6
   are only needed when the interrupt endpoint will be used.
7. If acting as system controller: IFC pulse (5.5), REN on (5.6), take
   control (5.4). Error code 5 from the take-control reply on an empty
   bus is harmless.

The adapter is then CIC with ATN true and ready for the addressing
sequences of section 6.

Observed 2026-09-19 (§10.3.2): NI's sequence for an instrument session
is: step 5 (the 26 writes); a read of bank-1 0x0d, 0x0c, 0x1f; IFC pulse
(the reply already shows CIC and ATN, no take-control follows); the same
register read followed in the same message by REN on; then the bank-2
configuration `02 03 01`, `02 04 01`, `02 05 PAD`, `02 06 SAD-byte-or-0`,
`02 07 timeout-code`; two bank-2 0x03 writes; a presence probe `02 PAD SAD
00` (§10.6.1); one more bank-2 0x03 write. Steps 2-4 and 6 are not in the
captures. A board (INTFC) session sends the 26 writes and five or six
0x21 / 0x0200 status queries only -- no IFC, no REN -- and is not CIC
until the application pulses IFC (§10.3.5, §10.6.4).

### 2.9 Shutdown

One register-write instruction with two writes: bank 1 addr 0x0a value
0x02 (AUXMR chip reset), then bank 3 addr 0x10 value 0x00. Then release
the interface. (Sequence captured from NI's Windows driver.) Observed
2026-09-19 (§10.3.3): confirmed byte for byte, `09 02 00 01 0a 02 03 10 00
00 00 00 04 00 00 00`, sent about 0.5 s after the last `viClose` at
process end; the reply reads ibsta 0xffff. `viClose` itself writes bank-2
0x03 := 1 and 0x04 := 0 (20 bytes).

---

## 3. Bulk message framing

### 3.1 Host -> device message shape

A message is one or more **instruction blocks** followed by one
**termination block**. Each instruction block starts with a one-byte
opcode. Every instruction block is zero-padded to a multiple of 4 bytes.
The termination block is exactly `04 00 00 00`. The whole message is
written in a single bulk OUT transfer.

```
+-----------+-----+------------+-----------+-----+-------------+
| opcode 1  | ... | pad to x4  | opcode 2  | ... | 04 00 00 00 |
+-----------+-----+------------+-----------+-----+-------------+
```

Only one message may be outstanding on the bulk pipes at a time. Every
message produces exactly one reply on the bulk IN endpoint; read it
before sending the next message.

The only multi-instruction message observed is the read instruction with
its embedded register-write block (5.2). Batching any other combination
of opcodes in one message is untested. Observed 2026-09-19 (§10.2.1): NI
batches up to five blocks per message (0x03, 0x0c, data instruction,
0x09, 0x09; also 0x08 + 0x09 and 0x09 + 0x09); blocks execute in order
and the reply is the concatenation of one reply per block followed by a
single termination block. NI hands a message longer than 512 bytes to USB
as two OUT transfers, 512 bytes and the remainder; on the bus that is
still one run of packets ended by a short one (§10.5.2).

### 3.2 Host -> device opcodes

| Opcode | Instruction | Fixed header bytes after the opcode | Payload | Reply |
|--------|-------------|--------------------------------------|---------|-------|
| 0x01 | Take control (adapter becomes active controller with ATN true) | `s 00 00` where s = 0x01 synchronous, 0x00 asynchronous | none | 12-byte status reply |
| 0x02 | Presence probe of a GPIB address (observed 2026-09-19, §10.6.1) | `P S 00` (P = primary address, S = 0x60 \| secondary or 0x00) | none | 16 bytes: status block + `01 00 00 00` (present) or `00 00 00 00` (absent) + termination |
| 0x03 | Reserved / unknown; never observed in use. Observed 2026-09-19 (§10.2.2): `03 00 00 00`, replies with one 8-byte status block carrying the current ibsta; NI puts it first in every instrument-operation message | `00 00 00` | none | 8-byte status block |
| 0x04 | Termination block | `00 00 00` | none | n/a (also appears at the end of replies) |
| 0x06 | Go to standby (ATN false) | `00 00 00`. Observed 2026-09-19 (§10.7.1): NI sends `00 00 0a`, and `01 00 0a` for the "deassert after handshake" variant; byte 3 = 0x0a meaning not established | none | 12-byte status reply, id 0x06 |
| 0x07 | Parallel poll | `t 00 00` where t = timeout code (only 0xf0 observed) | none | status block + 1 result byte |
| 0x08 | Register read | `n` (count of reads) | n × (`bank addr`) | see 3.5 |
| 0x09 | Register write | `n 00` (count of writes) | n × (`bank addr value`) | 16-byte register-write reply |
| 0x0a | Read data from bus | `m e t cl ch 00 00` (see 5.2) | an embedded 0x09 block, see 5.2 | data blocks + status, see 5.2 |
| 0x0b | Read data from bus, data delivered raw on bulk IN 0x88 (observed 2026-09-19, §10.1.2) | `m e t c0 c1 c2 c3` (32-bit negative count) | none | 12-byte block on 0x84: status block + `xx 00 00 00` (xx = 0xe0 EOI seen / 0x60 not); data on 0x88, padded to an even length (§10.1.3) |
| 0x0c | Command bytes (adapter drives ATN true for the duration) | `c 00 t` (c = 8-bit count code, t = timeout code) | up to 16 command bytes | 12-byte status reply |
| 0x0d | Write data to bus | `cl ch t 00 00 f 00` (see 5.1) | data bytes | 12-byte status reply |
| 0x0e | Write data to bus, data sent raw on bulk OUT 0x06 (observed 2026-09-19, §10.5.2) | `00 00 t 00 e f 00 c0 c1 c2 c3` (32-bit negative count; block is 12 bytes) | none (data on 0x06) | 8-byte status block, id 0x0e |
| 0x0f | Interface clear pulse (IFC) | `00 00 00` | none | 12-byte status reply |
| 0x10 | Serial poll of one device (observed 2026-09-19, §10.5.4) | `01 00 x P S t 00` (x = 0x00 or 0x01, meaning not established; P, S as for 0x02 -- S = 0x61 observed, §10.5.4; t = timeout code) | none | `3a P S sb` (sb = status byte) + 8-byte status block id 0x39; on failure the status block alone, without the `3a` block (§10.6.6) |

### 3.3 Count encoding

Byte counts in instructions are sent as the **two's-complement negative
of the length**:

- 16-bit counts (read 0x0a, write 0x0d): `cl ch` = (0x10000 - length) & 0xffff, little-endian.
- 8-bit count (command 0x0c): `c` = (0x100 - length) & 0xff.

Equivalently, bitwise-NOT of (length - 1). Examples: length 1 -> `ff ff`
(or `ff`); length 3 -> `fd`; length 6 -> `fa ff`; length 256 -> `00 ff`;
length 65535 -> `01 00`.

The count field in a reply status block (4.1) uses the same convention:
it holds (bytes transferred - bytes requested) as a 16-bit two's
complement value, so 0x0000 means "all requested bytes were transferred".

Observed 2026-09-19 (§10.1.2, §10.1.3, §10.5.2): the 0x0b and 0x0e
instructions carry the count as a 32-bit little-endian two's complement
(`00 b0 ff ff` = -20480, `fe f7 ff ff` = -2050) and their reply status
blocks hold a 32-bit (transferred - requested) in bytes 4-7 (`00 00 00 00`
for a full 20480-byte chunk, `52 b0 ff ff` = 82 - 20480).

### 3.4 Register-access blocks

Register write block (opcode 0x09):

```
09 nn 00 | b1 a1 v1 | b2 a2 v2 | ... | pad to x4
```
- `nn` = number of (bank, addr, value) triplets (1 byte; 26 is known to work).
- `b` = bank: 1 = TNT4882 controller chip, 2 = unknown bank, 3 = unknown
  device-level bank. Observed 2026-09-19 (§10.2.4): NI writes bank-2 0x03
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
- `nn` = number of (bank, addr) pairs (1 and 4 observed).

Bank 1 addresses. NEC 7210-compatible registers sit at **twice** their
7210 register number; TNT4882-specific registers sit at their native
one-chip-mode offsets.

| addr | write (host -> chip) | read (chip -> host) |
|------|----------------------|---------------------|
| 0x00 | CDOR (data out)      | DIR (data in) |
| 0x02 | IMR1                 | ISR1 |
| 0x04 | IMR2                 | ISR2 |
| 0x06 | SPMR (see 2.6 note a)| SPSR |
| 0x08 | ADMR                 | ADSR |
| 0x0a | AUXMR (aux commands and hidden registers AUXRA/B/E/G/I via top bits) | CPTR |
| 0x0c | ADR                  | ADR0. Observed 2026-09-19 (§10.3.2): the value read here equalled the low byte of the current ibsta in all 50 reads captured (0x00, 0x30, 0x64, 0x74), not an address register |
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

AUXMR values used by this protocol (write to bank 1 addr 0x0a):

| value | meaning |
|-------|---------|
| 0x00 | immediate execute pon |
| 0x01 | clear parallel-poll flag (ist = 0) |
| 0x02 | chip reset |
| 0x05 | return to local |
| 0x09 | set parallel-poll flag (ist = 1) |
| 0x14 | disable system control |
| 0x16 | clear IFC |
| 0x17 | clear REN |
| 0x1e | set IFC |
| 0x1f | set REN |
| 0x51 | holdoff handshake immediately |
| 0x55 | clear END status |
| 0x60 \| cfg | parallel-poll register (PPR) load |
| 0x80 \| bits | AUXRA: bit0 holdoff-all, bit1 holdoff-on-END, bit2 REOS, bit3 XEOS, bit4 BIN |
| 0xa0 \| bits | AUXRB: bit0 CPT enable, bit1 SPEOI, bit2 TRI, bit4 ISS |
| 0x40 \| bits | AUXRG: bit0 CHES, bit3 NTNL |
| 0xe0 \| bits | AUXRI: bit0 SISB, bit2 PP2, bit3 USTD |

CMDR values used (bank 1 addr 0x1c): 0x02 clear system controller, 0x03
set system controller, 0x22 soft reset. (Also defined by the chip: 0x04
GO, 0x08 STOP, 0x10 reset FIFOs.)

### 3.5 Device -> host replies

Reply block ids:

| id | Block | Length |
|----|-------|--------|
| 0x01, 0x06, 0x07, 0x09, 0x0c, 0x0d, 0x0f | status block echoing the instruction opcode | 8 bytes (4.1) |
| 0x34 | register-read data chunk: `34 r r r` (up to 3 register values) | 4 bytes |
| 0x35 | register-read end: `35 k 00 00`; k appears to be a count (uncertain: total or last-chunk; k mod 3 equals the number of reads mod 3). Observed 2026-09-19 (§10.8): k = 3 after 3 reads and 2 after 2 reads, i.e. the number of registers read, at least up to 3 | 4 bytes |
| 0x36 | read-data block: id + 15 data bytes | 16 bytes |
| 0x37 | extended read-data block: `37 00` + 30 data bytes | 32 bytes |
| 0x38 | read-data status block (4.1) | 8 bytes |
| 0x04 | termination `04 00 00 00` | 4 bytes |
| 0x02, 0x03, 0x0b, 0x0e, 0x21, 0x39 | status blocks of the instructions observed 2026-09-19 (§10.2.1); 0x21 arrives as the control-request reply, 0x39 follows the 0x3a block of a serial poll | 8 bytes |
| 0x11 | `11 00 00 00`, four in a row before the first 0x37 block of a 0x0a reply that follows other blocks (observed 2026-09-19, §10.1.5); skip | 4 bytes |
| 0x3a | serial-poll result `3a P S sb` (observed 2026-09-19, §10.5.4) | 4 bytes |

Register-read reply layouts:

- 1 register: `34 v0 00 00 | 35 k 00 00 [| 04 00 00 00]`, k mod 3 = 1.
- 4 registers (USB-B serial read): `34 v0 v1 v2 | 34 v3 pp pp | 35 k 00 00
  [| 04 00 00 00]`, where `pp` is padding of the partial final chunk
  (value unverified; expect 0x00) and k mod 3 = 1. Observed on the
  GPIB-USB-HS (one register): the termination block does follow, the
  reply is exactly `34 vv 00 00 35 01 00 00 04 00 00 00` (12 bytes).
  Request 32 bytes and accept a shorter transfer.

Reply sizes to expect:

| After instruction | Total bytes | Layout |
|-------------------|-------------|--------|
| 0x01, 0x06, 0x0c, 0x0d, 0x0f | 12 | status block (8) + `04 00 00 00` |
| 0x09 register write | 16 | status block (8, id 0x09) + `k 00 00 00` (k = writes completed) + `04 00 00 00` |
| 0x08 register read | up to 32 | 0x34 chunks + 0x35 block (+ termination, uncertain) |
| 0x07 parallel poll | up to 32 | status block (8) + result byte + padding (+ termination, uncertain) |
| 0x0a read | see 5.2 | data blocks + 16-byte trailer (observed; the sources implied 28) |
| 0x02 probe | 16 | status block (8, id 0x02) + `01 00 00 00` / `00 00 00 00` + `04 00 00 00` (observed 2026-09-19, §10.6.1) |
| 0x0b read | 12 on 0x84 (+ data on 0x88) | status block (8, id 0x0b) + `xx 00 00 00`; no termination block of its own when batched (observed 2026-09-19, §10.1.3) |
| 0x10 serial poll | 12 | `3a P S sb` + status block (8, id 0x39) (observed 2026-09-19, §10.5.4) |

The 12- and 16-byte reply lengths are exact and can be asserted.
Observed on the GPIB-USB-HS: the trailing 4 bytes of every such reply are
`04 00 00 00`.

### 3.6 Worked hex examples

All examples use timeout code 0xfc (3 s), controller primary address 0,
instrument primary address 22 (0x16).

**Register write, one write (REN on):**
```
OUT: 09 01 00 01 0a 1f 00 00 04 00 00 00
IN : 09 ss ss 00 00 00 00 00 01 00 00 00 04 00 00 00
```
`ss ss` = ibsta big-endian; byte 3 = error 0; byte 8 = 1 write completed.

**Command bytes UNL, MTA 0, LAD 22 (address instrument to listen):**
```
OUT: 0c fd 00 fc 3f 40 36 00 04 00 00 00
IN : 0c ss ss ee cc cc 00 00 04 00 00 00
```
`fd` = -3 (three command bytes). `ee` = error code; `cc cc` = 0 if all
three were accepted.

**Write `*IDN?\n` (6 bytes) with EOI on the last byte:**
```
OUT: 0d fa ff fc 00 00 08 00 2a 49 44 4e 3f 0a 00 00 04 00 00 00
IN : 0d ss ss ee cc cc 00 00 04 00 00 00
```
`fa ff` = -6 little-endian; byte 6 = 0x08 requests EOI; two pad bytes
after the data.

**Read up to 256 bytes, EOS disabled (OUT observed; IN as the sources
implied it, NOT what the device sends -- see the next example):**
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

**Register read of BSR (bus lines):**
```
OUT: 08 01 01 1f 04 00 00 00
IN : 34 vv 00 00 35 01 00 00 [04 00 00 00]
```
`vv` = BSR value (5.13).

**Take control, synchronous:**
```
OUT: 01 01 00 00 04 00 00 00
IN : 01 ss ss ee 00 00 00 00 04 00 00 00
```

---

## 4. Status block

### 4.1 Layout (8 bytes)

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| 0 | 1 | id | echoes the instruction opcode; 0x38 for the read-data status |
| 1 | 2 | ibsta | **big-endian** (byte 1 = high byte) |
| 3 | 1 | error code | see 4.3; 0 = success |
| 4 | 2 | count | **little-endian**, two's complement: (transferred - requested). Bytes not transferred = (0x10000 - count) & 0xffff; bytes transferred = requested - that. 0 when everything was transferred. **Meaningful only in replies to 0x0a, 0x0c, 0x0d.** Observed on the GPIB-USB-HS: replies to 0x01 and 0x06 carry `aa 55` here, and replies to 0x09 and 0x0f carry the count left over from the last data operation (`ff ff` after a reset, `52 ff` after a read that transferred 82 of 256). Observed 2026-09-19 (§10.8): on unit 013CC9DF the 0x01 / 0x06 / 0x0f / 0x03 / 0x09 replies all carried the stale count of the last data operation (never `aa 55`); the 0x02 probe writes it to 0; 0x0b and 0x0e write 32 bits (bytes 4-7). |
| 6 | 2 | unused | observed 0x00 0x00 (sources); on the GPIB-USB-HS `ff ff` in most replies, `01 00` in a read reply that filled the requested count, `00 00` after a successful 0x0d. Observed 2026-09-19 (§10.8): `ff ff` in every reply until the first 0x0b / 0x0e of a session, thereafter the high half of that instruction's 32-bit count and `00 00` in later 0x0c / 0x0d replies; ignore |

The same 8-byte layout is returned by control requests 0x20 and 0x21 and
pushed on the interrupt endpoint.

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
read reply as the end-of-read indication. Derive ERR, TIMO and CMPL from
the error code rather than from this field.

### 4.3 Error codes (byte 3 of the status block)

| Code | Label used in this document | Cause / recovery |
|------|-----------------------------|------------------|
| 0x00 | success | - |
| 0x01 | cut short by a stop request | control request 0x20 (5.11) ended the operation early; the partial count is valid |
| 0x02 | read attempted while ATN true | a 0x0a was issued with ATN asserted; send go-to-standby (0x06) first (5.3) |
| 0x03 | not addressed | read/write as controller while the adapter is not addressed as listener/talker; send the addressing command bytes (section 6) first |
| 0x04 | EOS configuration rejected / command chunk too long | read with a nonzero EOS mode byte or EOS character while the REOS bit (0x04) is clear; also returned by the USB-B for a 0x0c instruction carrying 17 or more command bytes |
| 0x05 | no acceptor on the bus | command bytes were not accepted by any device (bus empty or unpowered); harmless after a take-control on an empty bus |
| 0x07 | not controller in charge (observed 2026-09-19, §10.6.4) | 0x01, 0x06 or 0x0c issued while the adapter is not CIC (e.g. a board session before any IFC); ibsta 0x0000 and bytes 4-7 `ff ff ff ff` in the reply; NI-VISA reports VI_ERROR_NCIC |
| 0x08 | no listener addressed | data write as controller with no listener addressed. Observed 2026-09-19 (§10.6.2): reply `0d 00 28 08 f9 ff ff ff` for a 7-byte write to an empty address, 1 ms after the instruction; NI-VISA reports VI_ERROR_NLISTENERS. For a 0x0e: `0e 00 28 08` + 32-bit count, and the data transfer on 0x06 is refused with a STALL (§10.6.5) |
| 0x0a | device-side timeout | the device timeout (section 7) expired; the partial count is valid |
| other (6, >= 11) | unknown | treat as a generic I/O error |

One implementation assigns different meanings to codes 4-7 and appears to
be wrong; the table above is the one to use.

---

## 5. Operations

Each operation below is: message sent, reply expected, how outcome is
recognised. "Status reply" means the 12-byte reply of 3.5. For every
operation, error code 0 with the expected id is success; nonzero error
code maps via 4.3; a USB-level timeout while waiting for the reply means
the host-side wait (7.2) was too short relative to the device timeout,
and the device still owes a reply -- send the stop request (5.11) and
then read the reply.

ATN rule (applies throughout): after any 0x0c, send a 0x06 (go to
standby) before the next 0x0a. This is harmless if the firmware has
already released ATN and avoids error 2. Sending 0x06 before a 0x0d is
likewise harmless; sequencing 0x0c directly into 0x0d has been observed
to work. Observed 2026-09-19 (§10.1.2, §10.1.5): NI never sends 0x06 in
instrument sessions; its 0x0c is followed in the same message by 0x0a,
0x0b or 0x0d, the 0x0c reply shows ATN set and the data instruction's
reply shows it clear, with error 0 (38 such 0x0a and 45 such 0x0b). The
0x06 remains harmless. On the stop request: in the three failures NI's
driver was captured in (no listener on a 0x0e, device timeout on a 0x0b
and on a 0x10, §10.6.5-10.6.7) the reply arrived by itself with the error
code and NI sent no stop request, before or after.

### 5.1 Write data (0x0d)

```
0d cl ch t 00 00 f 00 <data...> <pad to x4> 04 00 00 00
```
- `cl ch` = -(length) 16-bit little-endian (3.3). Max length 0xffff.
- `t` = timeout code.
- `f` = 0x08 to assert EOI with the last byte, 0x00 otherwise.
- Observed 2026-09-19 (§10.5.1): NI fills byte 5 (the second `00`) with
  the session's termination character (0x0a by default, 0x2c / 0x0d when
  VI_ATTR_TERMCHAR was changed) on every write, byte 4 stays 0x00; `f`
  follows VI_ATTR_SEND_END_EN. Whether byte 5 has an effect was not
  tested. For a 2050-byte write NI used the 0x0e instruction with the
  data on bulk OUT 0x06 instead (§10.5.2); the longest 0x0d captured
  carried 17 bytes. Settled the same day (§10.5.2): NI sends every length
  up to 2048 bytes as one 0x0d in exactly this layout and switches to
  0x0e at 2049.
- Reply: status reply, id 0x0d. Bytes written = length - (bytes not
  transferred from the count field).
- If a caller's buffer exceeds 0xffff, split it into instructions of at
  most 0xffff bytes and set the EOI flag only on the last one.
- Precondition: the adapter must be talker and the target listener
  (section 6) -- otherwise error 3 or 8.

### 5.2 Read data (0x0a)

```
0a m e t cl ch 00 00  09 02 00 01 0a 51 01 0a 55  <pad to x4>  04 00 00 00
```
- `m` = EOS mode byte: 0x04 = terminate on EOS character (REOS), 0x10 =
  compare all 8 bits (BIN; otherwise 7-bit compare), 0x08 = XEOS (not
  meaningful for reads). **If EOS is disabled, `m` and `e` must both be
  0x00**, else error 4.
- `e` = EOS character.
- `t` = timeout code.
- `cl ch` = -(max length) 16-bit little-endian. Max 0xffff.
- Observed 2026-09-19 (§10.1.6): NI sends `m` = 0x00 with `e` = the
  session's termination character (0x0a) on every read with the
  character disabled, and `m` = 0x14 with the character when enabled;
  none of 80 such reads returned error 4 (adapter initialised with AUXRA
  0x99, §10.3.1). The "both must be 0x00" rule above therefore does not
  hold at least under that initialisation. An EOS match sets END exactly
  like EOI; bit 7 of the tail byte (item 3 below) tells them apart.
- The embedded 0x09 block (two AUXMR writes: 0x51 holdoff immediately,
  0x55 clear END) is always present; its status follows the read status
  in the reply, so it is evidently executed after the read. It stops the
  talker at the byte boundary and clears the END latch for the next read.
  Total message length: 24 bytes. Observed 2026-09-19 (§10.1.5, §10.1.2):
  NI's 0x0a carries no embedded 0x09 block at all, and its 0x0b is
  followed by a separate `09 01 00 01 0a 55` block (clear END only, no
  0x51); both work.
- EOS configuration: the per-read bytes `m`/`e` alone select EOS
  termination and the 8-bit compare; they may change from read to read
  without any register write. Init write #3 (2.6) carries the BIN bit only
  when the initialisation is (re)run while an 8-bit compare is already
  configured; a working sequence is 0x81 at attach followed later by reads
  with `m` = 0x14, so the two need not agree. Uncertain: whether the BIN
  bit in write #3 has any effect at all, and whether changing the EOS
  mode later ever requires re-sending write #3 (no such re-send is known
  to be needed). EOSR is never written.
- Reply layout, in order:
  1. Zero or more data blocks, each either `36` + 15 bytes or `37 00` + 30
     bytes. Assume all blocks in one reply share one size (uncertain
     whether the device ever mixes the two).
  2. 8-byte status block, id 0x38.
  3. 1 byte: ADR1 register contents (bit 7 = EOI seen with last byte);
     may be ignored -- use ibsta END instead.
  4. 1 byte: number of valid data bytes in the LAST data block (0 if none).
  5. 2 bytes: 0x00 0x00.
  6. `04 00 00 00`.
  Items 2-6 form a fixed 16-byte trailer (observed on the GPIB-USB-HS,
  2026-09-18). The sources implied two further items before the
  termination block -- an 8-byte status block with id 0x09 for the
  embedded register write, then `02 00 00 00` -- making 28 bytes; the
  device does not send them. A parser should accept both forms.
- Block size: reads of 1 and 5 bytes arrived in 0x36 blocks, a read of up
  to 256 bytes in 0x37 blocks (observed). The filler after the valid bytes
  of the last block is stale data, not zeros. Observed 2026-09-19
  (§10.1.5): requested counts 1..15 -> 0x36 blocks, 16 and above -> 0x37
  blocks; the filler is zeros in 0x36 blocks and stale bytes in 0x37
  blocks. When the 0x0a reply follows other blocks in the same reply and
  uses 0x37 sizing, four `11 00 00 00` blocks precede the first 0x37 block
  (also when no data block follows); a timed-out 0x36-sized read still
  carries one zero-filled 0x36 block. For counts above 1024 NI uses the
  0x0b instruction with raw data on bulk IN 0x88 (§10.1.1-10.1.4); 1024
  is the last count sent as 0x0a and 1025 the first sent as 0x0b.
- Bytes actually read = (blocks - 1) × block_size + last_block_count, or
  0 if no data block (the last-block-count byte is then meaningless).
  Cross-check: it must equal requested - (bytes not transferred from the
  0x38 count field).
- Host receive buffer for a requested N: ceil(N/30) 32-byte blocks (or
  ceil(N/15) 16-byte blocks) plus the trailer (size for 28 bytes, the
  longer form); request the larger of the two, rounded up to the
  endpoint's max packet size. The device ends the reply with a short
  packet.
- End conditions: ibsta END (0x2000) set in the 0x38 block means the read
  ended on EOI or, when `m` has 0x04, on the EOS character (NI-488.2
  convention). END clear with error 0 means the count was reached. Error
  0x0a means the timeout expired; the partial data is valid.
- Precondition: adapter listener, target talker (section 6), ATN false
  (send 0x06 after the addressing 0x0c) -- otherwise error 3 or 2.
  Observed 2026-09-19 (§10.1.5): NI's 0x0a directly after the addressing
  0x0c in the same message, without 0x06, returned error 0 with ATN
  released; the instruction evidently drops ATN itself.

### 5.3 Command bytes (0x0c)

```
0c c 00 t <cmd bytes...> <pad to x4> 04 00 00 00
```
- `c` = -(count) 8-bit. **Maximum 16 command bytes per instruction** on
  all models (the USB-B returns error 4 for a 0x0c instruction carrying
  17 or more). Split longer sequences into consecutive 0x0c messages,
  each with its own reply.
- `t` = timeout code.
- Reply: status reply, id 0x0c. Bytes accepted = count - (not transferred).
- Error 5 = nothing on the bus accepted the byte (no devices powered).
  Error 0x0a = a device held off the handshake for the whole timeout.
- ATN: the adapter drives ATN true for the duration of the instruction.
  Whether it releases ATN afterwards is uncertain; apply the ATN rule of
  section 5 (0x06 before the next 0x0a). A preceding 0x01 (take control)
  is not required once the adapter is CIC but is harmless. Observed
  2026-09-19 (§10.2.3, §10.6.4): the 0x0c reply's ibsta shows ATN still
  set (0x0038 after talk-addressing the adapter, 0x0074 after
  listen-addressing it); a 0x0c while the adapter is not CIC returns error
  7. NI's timeout byte in addressing 0x0c blocks is 0xfd regardless of the
  session timeout; board-level `viGpibCommand` uses the session's code.

### 5.4 Take control (0x01) / go to standby (0x06)

- Take control: `01 s 00 00 04 00 00 00`, s = 0x01 synchronous (wait for
  the current handshake to finish), 0x00 asynchronous. Reply: status
  reply. After success ibsta should show CIC (0x20) and ATN (0x10).
- Go to standby: `06 00 00 00 04 00 00 00`. Reply: status reply with id
  0x06. ATN false; the previously addressed talker may now source data.
  Observed 2026-09-19 (§10.7.1): NI sends `06 00 00 0a` (reply ibsta
  0x0020, ATN clear) and `06 01 00 0a` for VI_GPIB_ATN_DEASSERT_HANDSHAKE;
  byte 3 = 0x0a meaning not established. Both 0x01 and 0x06 return error 7
  when the adapter is not CIC (§10.6.4).

### 5.5 Interface clear -- IFC pulse (0x0f)

`0f 00 00 00 04 00 00 00`. Reply: status reply. The device pulses IFC
(IEEE-488.1 requires >= 100 us; the pulse length is the device's) and the
adapter becomes CIC. Only meaningful when the adapter is system
controller (2.6 row 16 = 0x03). There is no separate "assert IFC" /
"release IFC" instruction; AUXMR 0x1e / 0x16 can be written via register
writes for a manual pulse if ever needed. Observed 2026-09-19 (§10.3.2,
§10.7.1): confirmed; the reply ibsta is 0x0030 (CIC, ATN) and BSR reads
0xa0 (ATN, NDAC) afterwards; NI sends no take-control after it.

### 5.6 Remote enable on / off

Register write, one write: bank 1 addr 0x0a value 0x1f (REN on) or 0x17
(REN off). Reply: 16-byte register-write reply. A device enters remote
state when REN is true and it is addressed to listen. Observed 2026-09-19
(§10.7.1): confirmed for both values; NI precedes the write in the same
message with a read of bank-1 0x0d, 0x0c, 0x1f, and BSR bit 0 follows REN
(0x00 / 0x01 with the bus otherwise idle).

### 5.7 Take / release system control

Register write:
- Take: `01 1c 03` (CMDR set SC), `01 0a 16` (clear IFC) -- 2 writes.
- Release: `01 0a 17` (clear REN), `01 0a 16` (clear IFC), `01 0a 14`
  (disable system control), `01 1c 02` (CMDR clear SC) -- 4 writes.

### 5.8 Device clear

Command bytes (5.3):
- Selected device clear of address N: `3f 20+N 04` (UNL, LAD N, SDC).
- Universal device clear: `14` (DCL).
Then re-address before the next data transfer. Observed 2026-09-19
(§10.5.3): NI sends `40+C 3f 20+N 04` (MTA first) for `viClear`, and
`40+C 3f 20+N 08` for `viAssertTrigger`; the instrument discarded pending
output on the SDC (§10.1.7). With a secondary address S: `40+C 3f 20+N
60+S 04` and `.. 60+S 08` (§10.5.3).

### 5.9 Serial poll

There is no dedicated serial-poll instruction; use the IEEE-488.1
sequence with 5.3, 5.4 and 5.2:

1. Command bytes: `3f 20+C 18 40+N` (UNL, controller listens, SPE, device
   N talks), C = adapter primary address.
2. Go to standby (0x06).
3. Read (0x0a) with max length 1, EOS disabled (`m` = `e` = 0). The single
   byte is the status byte; bit 6 (0x40) = RQS.
4. Command bytes: `19 5f` (SPD, UNT).

Adapter's own serial-poll response byte (when the adapter is polled by
another controller): register write bank 1 addr 0x06 (SPMR) = status
byte; bit 6 requests service.

Observed 2026-09-19 (§10.5.4): NI does not use the sequence above. It
sends the dedicated instruction `10 01 00 x P S t 00` (P = device primary
address, S = 0x00 without secondary, t = timeout code, x = 0x00 or 0x01
meaning not established) and receives `3a P S sb` (sb = status byte) plus
a status block with id 0x39. The adapter also polls the device by itself
when SRQ is asserted and reports the status byte in the interrupt push
(2.5, §10.4.2), after which the device's RQS is already clear. With a
secondary address S the instruction carries 0x60 | S and the `3a` block
echoes it (§10.5.4); a poll that times out (error 0x0a after the device
timeout) returns the 0x39 status block without the `3a` block (§10.6.6).

### 5.10 Parallel poll

- Conduct: `07 t 00 00 04 00 00 00` (t = timeout code; only 0xf0 = no
  timeout has been observed). Reply: 8-byte status block then one byte =
  the 8 DIO lines during the poll; ignore the bytes after the result byte.
  Read up to 32 bytes.
- Configure own response: register write AUXMR = 0x60 | cfg (PPR).
- Own ist flag: AUXMR 0x09 (set) / 0x01 (clear).

### 5.11 Stop / abort an in-flight operation

Control request 0x20 (bmRequestType 0xC0, wValue 0, wIndex 0, wLength
8). Response: 8-byte status block. The device then finishes the pending
bulk instruction immediately and sends its normal reply with error code
0x01 and a valid partial count. Use this when the host-side USB read
times out while a long device-side timeout (e.g. code 0xf0) is running,
then read the bulk reply. Observed 2026-09-19 (§10.6.7, §10.8): the
request appears nowhere in the 27 captures of NI's driver, its failed and
timed-out instructions included; those end by themselves with a normal
reply.

### 5.12 Status query

Control request 0x21 with wValue 0x0200 (wLength 8) returns the current
8-byte status block (ibsta) without disturbing the bus. Poll this for
SRQI (0x1000) when implementing a wait-for-SRQ, or use the interrupt
endpoint with the monitor mask (2.5). With a mask set, the interrupt IN
endpoint delivers one 8-byte status block whenever a monitored bit becomes
set. Observed 2026-09-19 (§10.3.5): reply `21 ss ss 00 cc cc xx xx`; NI
uses it at INTFC open and for the ATN / SRQ / CIC state attributes. SRQI
was never seen set in it, including right after an SRQ the adapter had
already polled away (§10.4.4); NI's wait-for-SRQ with nothing pending
polls ibsta through the 12-byte bank-2 0x03 write every 15 ms instead
(§10.4.3).

### 5.13 Bus line status

Register read (3.4) of bank 1 addr 0x1f (BSR). Reply `34 vv 00 00 35 01
00 00`. Bits of `vv`: 0x01 REN, 0x02 IFC, 0x04 SRQ, 0x08 EOI, 0x10 NRFD,
0x20 NDAC, 0x40 DAV, 0x80 ATN (1 = line asserted). Only 0x1f is needed
for line status.

### 5.14 Change the adapter's own address

- Primary P: register write `01 0c P`, `02 00 P` (2 writes).
- Secondary S enable: `01 0c 80|S`, `01 08 32`, `02 01 60|S`;
  disable: `01 0c e0`, `01 08 31`, `02 01 00` (3 writes; same as 2.6 rows
  20-22).

### 5.15 Return to local

Register write AUXMR = 0x05. Observed 2026-09-19 (§10.7.4): for the
go-to-local modes of `viGpibControlREN` NI writes no register; it sends
the GTL command byte to the addressed device (`40+C 3f 20+N 01`) and, for
DEASSERT_GTL, then REN off (5.6).

### 5.16 Find listeners (presence probe)

Presence signals attested on this adapter:

- Error 5 on a 0x0c: no device on the bus accepted the command byte. Every
  powered device accepts command bytes regardless of its address, so this
  only says whether the bus is empty, not whether address N exists.
- Error 8 on a 0x0d: no device is currently addressed to listen. This
  discriminates address N but requires sending at least one data byte to
  the instrument.

Recommended non-intrusive probe for address N (IEEE-488.1 acceptor
handshake sampled via 5.13). Bench-verified on the GPIB-USB-HS with a
Keithley 2400 at PAD 3, 2026-09-18: BSR read 0x01 (REN only) for empty
addresses and had NDAC set for address 3; the probe over 1..30 returned
exactly [3]:

1. 0x0c `3f 20+N` (UNL, LAD N). Error 5 here means the bus is empty; stop.
2. 0x06 (go to standby, ATN false).
3. Register read of BSR (5.13). NDAC (bit 0x20) asserted means a listener
   at N is present; released means none.
4. 0x01 (take control), then 0x0c `3f` (UNL).

Observed 2026-09-19 (§10.6.1): the adapter has a probe instruction, `02 P
S 00` (S = 0x60 | secondary or 0x00), reply status block + `01 00 00 00`
for present / `00 00 00 00` for absent, about 1.8 ms. NI issues it at
every session open and, for an absent address, 50 times at 104 ms
intervals (each but the first preceded by `40 3f 20+N 04`) before opening
the session anyway.

### 5.17 Pass control (low priority)

Command bytes `40+N 09` (TAD N, TCT) hand control to device N; the
adapter then ceases to be CIC. Not exercised by either source; how the
adapter reports the transition (CIC clearing in ibsta) is uncertain.

### 5.18 Mapping for a pyvisa-py GPIB session

| VISA operation | Protocol |
|----------------|----------|
| open | attach per 2.8 (includes IFC pulse, REN on, take control) |
| write | 0x0c `3f 40+C 20+N [60+S]`; 0x0d with EOI flag 0x08 (send_end) |
| read | 0x0c `3f 20+C 40+N [60+S]`; 0x06; 0x0a with `m`/`e` from the session's read termination; END -> stop, else loop until count |
| read_stb | 5.9 (includes the 0x06) |
| clear | 5.8 selected device clear |
| assert_trigger | 0x0c `3f 20+N 08` (GET) |
| send_ifc | 5.5 |
| control_ren | 5.6 plus, for GTL, 0x0c `3f 20+N 01` |
| control_atn | 5.4 |
| gpib_command | 5.3 in 16-byte chunks |
| timeout attribute | section 7 code in every 0x0a/0x0c/0x0d; host wait per 7.2 |
| close | 2.9 |

Observed 2026-09-19 (§10): NI's own mapping differs in these places --
addressing is `40+C 3f 20+N [60+S]` / `3f 20+C 40+N [60+S]` with the
adapter's address first; no 0x06 is sent; reads above 1024 bytes use 0x0b
(data on 0x88), a 2050-byte write used 0x0e (data on 0x06); read_stb is
the 0x10 instruction; open includes the 0x02 probe and the bank-2
0x03..0x07 writes and omits take-control; close writes bank-2 0x04 := 0
and the 2.9 shutdown comes at process end; enable/disable of the SRQ
event and the termination-character attributes write nothing but bank-2
0x03 := 1. Settled the same day: the switches are at 1024 / 1025 bytes
for reads and 2048 / 2049 for writes (§10.1.1, §10.5.2); control_ren on
an instrument session is the REN write, plus the 0x02 probe for the
"address" modes, plus `0c .. 11` for the LLO modes, and `40+C 3f 20+N 01`
for the GTL modes (§10.7.4); errors on the raw paths are in §10.6.5-10.6.7.

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
optional):

| Purpose | Command bytes |
|---------|---------------|
| Controller talks, instrument listens (before 0x0d) | `3f 40+C 20+N [60+S]` |
| Instrument talks, controller listens (before 0x06 + 0x0a) | `3f 20+C 40+N [60+S]` (optionally `5f` first) |
| Serial poll | `3f 20+C 18 40+N [60+S]` ... 0x06 ... read 1 byte ... `19 5f` |
| Selected device clear | `3f 20+N [60+S] 04` |
| Trigger | `3f 20+N [60+S] 08` |
| Go to local | `3f 20+N [60+S] 01` |
| Pass control | `40+N [60+S] 09` |

The adapter's own address C is the value written to ADR in 2.6 row 18
(0 by default). The adapter must be CIC for any of this; if ibsta lacks
CIC, pulse IFC (5.5) or take control (5.4). Observed 2026-09-19
(§10.2.3): NI orders the talk case `40+C 3f 20+N [60+S]` (MTA before
UNL); the listen case, SDC, trigger and the `60+S` placement are as in
the table (SDC and trigger with `40+C` prepended). A 0x0c while not CIC
returns error 7 (§10.6.4). SDC and trigger through a secondary address
and go-to-local were captured later the same day and agree with the table
in the same way: `40+C 3f 20+N 60+S 04`, `.. 60+S 08`, `40+C 3f 20+N 01`
(§10.2.3).

---

## 7. Timeouts

### 7.1 Device timeout code byte

Present in 0x0a, 0x0c, 0x0d (and 0x07). The code for a requested timeout
T (in microseconds) is the smallest row with T <= limit:

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
(T10us = 1 ... T100s = 15). The 0x02 code for 1000 s has been
bench-confirmed on a USB-B; NI's own driver is reported to send 0xff
there. Requests above 1000 s fall back to 0xf0.

Observed 2026-09-19 (§10.1.9): NI-VISA 22.5 maps VI_ATTR_TMO_VALUE 100 ms
-> 0xf9, 300 -> 0xfa, 1000 -> 0xfb, 2000 and 3000 -> 0xfc, 10 s -> 0xfd,
20 s and 30 s -> 0xfe, 100 s -> 0xff, 300 s -> 0x01, 1000 s -> 0x02,
infinite -> 0xf0, exactly the table above; the report of 0xff for 1000 s
is wrong. The code the driver programs at session open, before the
application sets a timeout, is 0xfb. Measured device-side expiry on the
GPIB-USB-HS: code 0xfc -> 4.20 s (three cases), code 0xfe -> 33.55 s
(§10.1.8). The code does not bound a whole instruction: 20480-byte
chunks that took 4.0 s each completed with error 0 under code 0xfc.

### 7.2 Host-side USB wait

The device answers a 0x0a, 0x0c or 0x0d only after the operation
completes or its own timeout expires, so the host's wait on the bulk IN
transfer must exceed the device timeout by a margin. Recommended:

| Transfer | Host wait |
|----------|-----------|
| bulk IN after 0x0a, 0x0c, 0x0d with device timeout T > 0 | T + max(2 s, 0.5 × T) |
| bulk IN after 0x0a, 0x0c, 0x0d with T = 0 (code 0xf0) | a long finite wait chosen by the application; on expiry send the stop request (5.11) and read the reply |
| bulk OUT of any instruction; bulk IN after 0x01, 0x06, 0x07, 0x08, 0x09, 0x0f | 1 s minimum |
| control requests | 1 s (100 ms per readiness-query attempt) |

Reads that may legitimately wait on a slow instrument should use the
upper end of the margin (T + 50 %) so that a device-side timeout is
reported through the status block rather than as a USB error. Observed
2026-09-19 (§10.1.8): the device expires 12-40 % after the nominal value
(4.20 s for the 3 s code, 33.55 s for the 30 s code), which the margins
above cover; the 0x88 data transfer of a 0x0b read completes 0.4-0.5 ms
before its 0x84 reply, and for a zero-byte result completes with zero
bytes, so both transfers must be waited for. OUT transfers are paced by
the bus as well: the 2049 data bytes of a 0x0e took 368 ms to complete on
0x06 and the tail of a 2080-byte 0x0d message 103 ms on 0x02 (§10.5.2),
so the 1 s row above is too short for a long write to a slow listener;
let the OUT wait follow the device timeout.

---

## 8. Known pitfalls

1. **Padding and termination.** Every instruction block must be
   zero-padded to a 4-byte boundary and every message must end with
   `04 00 00 00`. A message without the termination block gets no reply.
2. **One message, one reply.** Never queue a second bulk OUT before the
   reply to the first has been read. On a reply size mismatch (e.g. 12
   expected, other received) dump the bytes; the pipes are then out of
   step and the safest recovery is the stop request (5.11), a drain read,
   and a fresh register initialisation.
3. **EOS bytes on reads.** Nonzero EOS mode or character with REOS clear
   -> error 4 on every read. When the session disables read termination,
   send `00 00`. Observed 2026-09-19 (§10.1.6): NI sends mode 0x00 with a
   nonzero character on every unterminated read and never gets error 4
   (adapter initialised with AUXRA 0x99); sending `00 00` remains safe.
4. **16-byte command chunks.** A 0x0c instruction carrying 17 or more
   command bytes fails on the USB-B (error 4); keep the chunk limit on
   all models.
5. **Count encodings are negative.** Instruction counts are -(length);
   reply counts are (transferred - requested). Getting the sign wrong
   yields a 65535-byte transfer request.
6. **Read reply buffer.** Size the receive buffer from the wire layout:
   ceil(N/30) 32-byte blocks (or ceil(N/15) 16-byte blocks) plus the
   28-byte trailer, the larger of the two rounded up to the endpoint's
   max packet size; a smaller request is truncated by libusb with an
   overflow error. Observed 2026-09-19 (§10.1.5): when the 0x0a is
   batched behind other blocks, add their replies plus 16 bytes of `11 00
   00 00` padding; a 0x0b read needs a buffer of N on 0x88 and 56 bytes on
   0x84 for NI's five-block message.
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
    observed to be reliable; if it is used, arm it with the monitor mask
    (2.5) and read at least wMaxPacketSize per transfer.
12. **Take control on an empty bus** can return error 5; treat as
    harmless at initialisation.
13. **Error 2 on read** means ATN is still asserted; send 0x06 first.
    Error 3 means the addressing command bytes were not sent (or were
    rejected); resend them. Observed 2026-09-19 (§10.1.5): a 0x0a or 0x0b
    placed directly after the addressing 0x0c in the same message did not
    return error 2 (83 cases); error 7 means the adapter is not CIC
    (§10.6.4).
14. **Register-write reply** is exactly 16 bytes and byte 8 must equal the
    number of writes sent; a smaller value means the device stopped at a
    bad (bank, addr) pair.
15. **Unknown registers.** Bank 3 addr 0x10, bank 2 addrs 0x00-0x02 and
    bank 1 addr 0x0f are written with fixed values at init because NI's
    driver does; their meaning is unknown. Do not omit them. Observed
    2026-09-19 (§10.2.4): bank 2 addrs 0x03-0x07 are written per session
    (0x03 := 1 constantly, 0x04 := 1/0 open/close, 0x05 := PAD, 0x06 :=
    SAD byte, 0x07 := timeout code); the instructions carry the same
    information themselves, and whether any of these writes is required
    for the instructions to work is not established.
16. Absence of serial/parallel poll or SRQ handling in a known working
    implementation is not evidence those instructions fail.
17. **A hung adapter.** Observed on GPIB-USB-HS 01CEE482, 2026-09-18: the
    adapter answered every control request normally (serial number,
    readiness, status and stop), accepted bulk OUT messages on 0x02 until
    about 4 KB had been queued (and about 1 KB on 0x06), then NAKed, and
    never sent a byte on 0x84, 0x88 or 0x81. Nothing on the USB side
    cleared it: not the stop request, the monitor mask, clear-halt,
    SET_CONFIGURATION 0/1, nor a USB bus reset (which empties the
    endpoint FIFOs but does not restart the firmware). Unplugging and
    replugging the adapter fixed it at once. A driver should treat "the
    initialisation message was accepted but no reply arrived within 2 s,
    nor after a stop request" as this condition and tell the user to
    power-cycle the adapter.
18. **Settle after IFC / REN before the first addressed command.** Observed
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
27 pcaps (22 from a first batch; read_thresholds, write_thresholds,
raw_errors, sad_poll and ren_device from a second batch the same day).
They record the behaviour of our own adapter under the vendor
driver; no program source was consulted for them. Where they contradict
the sources above the observed behaviour is stated next to the original
text, never in its place.

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
`docs/design/captures/ni_usb_gpib_2026-09-19/` (27 pcaps: 22 in a first
batch and, the same day, five aimed at what the first batch left open --
read_thresholds, write_thresholds, raw_errors, sad_poll, ren_device; see
its README). Provenance of each fact below is `(name.pcap t DIR n B)`:
pcap, seconds from the first packet of that pcap, direction (OUT = bulk OUT
0x02, IN84 = bulk IN 0x84, IN88 = bulk IN 0x88, OUT06 = bulk OUT 0x06,
INTR = interrupt IN 0x81, CTRL = control request), payload length.
Decode with `python usbpcap_dump.py <name>.pcap 2 --full`.

Everything in this section was recorded with the adapter initialised by
NI's 26-write sequence (10.3.1), which differs from 2.6 in one value; where
a fact may depend on that (EOS handling, 10.1.6), it is said so.

### 10.1 Read path

#### 10.1.1 Two read instructions

NI uses two read instructions, chosen by the requested count:

| Requested count | Instruction | Data returned on | Observed counts |
|-----------------|-------------|------------------|-----------------|
| 1 .. 1024 | 0x0a (5.2) | bulk IN 0x84, framed in 0x36 / 0x37 blocks | 1, 2, 8, 10, 15, 16, 30, 31, 32, 60, 63, 64, 65, 100, 127, 128, 200, 255, 256, 511, 512, 1023, 1024 (counts.pcap 0.4288 .. 12.8188; partial.pcap; eosmodes.pcap; eos.pcap 1.5455) |
| 4096 .. 20480 | 0x0b (new) | bulk IN 0x88, raw, unframed, padded to an even length (10.1.3) | 4096 (counts.pcap 13.4323), 20480 (counts.pcap 14.0488; every 0x0b in idn, clear, eos, trac, srq, timeouts, nolistener, two_sessions, longwrite, readtimeout_long, terminate) |

The switch lies between 1024 and 4096; no count in between was captured.
20480 is pyvisa's default chunk size, so every `viRead` issued by pyvisa's
`read()` used 0x0b.

Observed in read_thresholds.pcap: the boundary is 1024 / 1025. `*IDN?`
followed by `viRead` with each of nine counts; every one was sent as 0x0b
in the 40-byte message of 10.1.2 (`m e t` = `00 0a fc`), the 82 bytes came
raw on bulk IN 0x88 and the 56-byte reply on 0x84:

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
00 00`) and 1025 the first 0x0b, the rule NI appears to follow is:
**requested count <= 1024 -> 0x0a, 16-bit count, data framed on 0x84;
requested count >= 1025 -> 0x0b, 32-bit count, data raw on 0x88.** Only
the requested count decides: all nine reads returned the same 82 bytes.
Nothing changes at 2048 / 2049 (the write boundary, 10.5.2) or at 4096.
In each of these nine reads the host submitted its IN transfer on 0x88
first and the one on 0x84 second, both within 0.1 ms of the OUT and
before any data had come back (0.3160 OUT, 0.3161 IN88, 0.3161 IN84).

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
error 4 (36 in 0x0a form and 44 in 0x0b form across the captures; every
one of them returned error 0 or, when nothing was pending, 0x0a). This
contradicts the "both must be 0x00, else error 4" rule of 5.2 and 8.3, at
least with NI's initialisation (AUXRA 0x99, 10.3.1).

#### 10.1.7 Count-limited read leaving data in the instrument

`viRead(10)` then `viRead(200)` on one 82-byte message (partial.pcap):
the first returned 10 bytes with count 0, no END (0.5366); the second was a
fresh 32-byte message including the addressing 0x0c and returned the
remaining 72 bytes with END (0.5456 IN84 156 B: three 0x37 blocks, tail
`e0 0c`, count `80 ff` = -128 = 72 - 200). Re-addressing an instrument
that still holds data does not disturb it. `viClear` (10.5.3) between
rounds in counts.pcap discarded the remainder each time.

#### 10.1.8 Timeouts on reads

A read with nothing to read returned error 0x0a after 4.196 s with code
0xfc (partial.pcap 1.0462 OUT -> 5.2418 IN84; nolistener.pcap 5.6164 ->
9.8126 in 0x0b form; board_io.pcap 1.9105 -> 6.1058) and after 33.55 s
with code 0xfe (eos.pcap 1.5682 -> 35.1236). Both exceed the nominal 3 s
and 30 s of 7.1. The code does not bound the whole instruction: with code
0xfc every 20480-byte chunk of the 61 768-byte read took 4.0 s and
completed with error 0 (readtimeout_long.pcap 0.5227 -> 4.5212, 4.5223 ->
8.5142, 8.5152 -> 12.4405). What the code bounds (a per-byte or handshake
interval) cannot be read off the wire.

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

The code NI programs at session open, before the application touches
`VI_ATTR_TMO_VALUE`, is 0xfb (every INSTR open, e.g. open.pcap 0.0070 OUT
32 B). The 0x0c addressing blocks NI emits inside INSTR operations carry
0xfd regardless of the session timeout (237 of 250 0x0c blocks captured,
including the 100 ms session, timeouts.pcap 7.4820); the 13 with 0xfc are
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
0x0a, 0x0b or 0x10, of the first bank-2 configuration message of a session
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
| 0x03 | 0x01 (481 writes) | 10.2.5 |
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
(open.pcap 0.0000 OUT 88 B; byte-identical in all 27 captures, each a new
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

The three register values read at open: bank-1 0x0d always 0x00 (all 50
reads in the captures); bank-1 0x0c equal to the low byte of the current
ibsta in every read (0x00 before IFC, 0x30 after IFC, 0x64 / 0x74 later:
nolistener.pcap 10.8702, srq.pcap 2.0333), which does not match the ADR0
label of 3.4; bank-1 0x1f = BSR: 0x00 before IFC with REN off (0x20, NDAC
alone, in six captures, e.g. ren_device.pcap 0.0017), 0xa0 (ATN, NDAC)
after IFC, 0xa1 with REN, 0x31 (NDAC, NRFD, REN) at the second open
of nolistener.pcap (10.8702) after a read had left ATN false.

#### 10.3.3 Close

`viClose` of the last session on the address: `09 01 00 02 03 01 00 00 |
09 01 00 02 04 00 00 00 | 04 00 00 00` (20 B), reply 28 B (open.pcap
1.0126). Of a session that is not the last: the 12-byte bank-2 0x03 write
only (two_sessions.pcap 1.2908). About 0.5 s after the last close, at the
end of the process, the shutdown of 2.9 byte for byte: `09 02 00 01 0a 02
03 10 00 00 00 00 04 00 00 00` (open.pcap 1.5764 OUT 16 B), reply `09 ff
ff 00 cc cc ff ff 02 00 00 00 04..` -- ibsta reads 0xffff after the chip
reset (every capture; 0x1010 in board_io.pcap 9.6195, see 10.7.2).
Correction from the second batch: not every capture. The shutdown reply
was `09 00 00 00 00 00 00 00 02 ..`, ibsta 0x0000, in read_thresholds.pcap
(5.2342) and in counts.pcap (20.5265), the two captures whose last bus
operation was the SDC of a `viClear` (ibsta 0x0078 before the close). An
implementation must not test the shutdown reply's ibsta.

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
byte 4 = 0x00 in all 83 writes captured. `f` = 0x08 with `VI_ATTR_SEND_END_EN`
true, 0x00 with it false (write.pcap 0.5133 vs 1.0173 OUT 40 B, `*CLS\r\n`).
The terminator is ordinary data: `*CLS\r\n` = 6 bytes, count `fa ff`;
`viWrite` of `*CLS` without terminator = 4 bytes, `fc ff`, block 12 bytes,
message 36 (write.pcap 1.5201). Reply block `0d 00 28 00 00 00 ff ff`:
ibsta CIC|TACS with ATN clear, error 0, count 0. Whether `e` in the 0x0d
header has any effect (e.g. EOI on the character) was not tested; every
write carried EOI or not per `f` alone.

#### 10.5.2 Long write (0x0e, new; longwrite.pcap)

A 2050-byte `viWrite` (2048 bytes + `\r\n`) was sent as
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
count `00 00 00 00`. The switch from 0x0d lies between 17 bytes (trac.pcap
13.0009, 0x0d) and 2050 bytes; no length in between was captured. 0x0e is
to 0x0d what 0x0b is to 0x0a, and the endpoint pairs 0x02/0x84 (framed) and
0x06/0x88 (raw) are the "alternate endpoints" of 1.2.

Observed in write_thresholds.pcap: the boundary is 2048 / 2049. `viWrite`
of 18, 24, 32, 48, 63, 64, 65, 100, 128, 255, 256, 257, 512, 1024, 1025
and 2048 bytes (`*CLS;` repeated, `\n` last, EOI on) each went as one
framed 0x0d on 0x02; 2049 bytes went as 0x0e with the data on 0x06. The
rule NI appears to follow: **length <= 2048 -> 0x0d, length >= 2049 ->
0x0e.** The write boundary is not the read boundary (1024 / 1025, 10.1.1).

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
`P` = 0x18 primary address, `S` = 0x00 (secondary byte, presumably 0x60|S
when present -- not captured), `t` = timeout code 0xfe; `x` = 0x00 in three
polls of a fresh session (stb.pcap 0.5134, 0.7167, 0.9191) and 0x01 in the
poll after an SRQ had been serviced (srq_poll.pcap 2.5361), meaning not
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
`10 01 00 00 18 61 fc 00` (0.5152 OUT 24 B) -- `S` = 0x61 = 0x60 | 1,
which settles the presumption above -- and the reply's 0x3a block echoes
both address bytes: `3a 18 61 04 | 39 00 74 00 00 00 ff ff` (0.5170 IN84
36 B; status byte 4, the value `viReadSTB` returned). `x` was 0x00 here
and in raw_errors.pcap (10.8046); its meaning stays not established. When
the poll fails the 0x3a block is missing from the reply (10.6.6).

### 10.6 Errors and addressing

#### 10.6.1 Presence probe (0x02, new) and an absent device

`02 P S 00`, `S` = 0x00 or the secondary byte 0x61 (nolistener.pcap
10.8801 `02 18 61 00`). Reply 16 B: 8-byte status block id 0x02 with
error 0 and count field written to `00 00`, then `01 00 00 00` if a device
at that address answered, `00 00 00 00` if not, then `04 00 00 00`
(open.pcap 0.0085 OUT / 0.0103 IN84: present, 1.8 ms; nolistener.pcap
0.0235 OUT / 0.0252 IN84: absent, 1.7 ms). What the adapter does on the
bus during those 1.7 ms
is not visible in USB captures; the count field being rewritten shows a
data-type operation. For `GPIB0::5::INSTR` with nothing at 5, NI probed
50 times at 104 ms intervals (0.0235 .. 5.1099), from the second probe on
each preceded by `0c fc 00 fd 40 3f 25 04` (MTA 0, UNL, LAD 5, SDC; 49
times), then opened the session anyway
(nolistener.stdout: `viOpen` returned after 5.2 s) and wrote the bank-2
configuration for address 5 (5.1127).

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
  `ABORT_PIPE`, in any of the 27 pcaps).
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
  `aa 55`; after a 0x02 probe it is `00 00`.
- **ibsta after chip reset** (shutdown reply) reads 0xffff.
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
  only completions in the 27 pcaps; none was cancelled or timed out by
  the host.

### 10.9 What an implementer must do to interoperate (from this section)

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
  the single-block cases of 3.5.
- Expect error 7 for 0x01 / 0x06 / 0x0c while not CIC, error 8 for a write
  with no listener, error 0x0a for a device timeout, and device timeouts
  that run 12-40 % longer than the nominal 7.1 value.
- The interrupt push on SRQ is 8 bytes `30 18 00 sb ..` with the status
  byte at offset 3; the adapter polls the device itself and releases SRQ,
  so a later explicit serial poll returns the status byte with RQS clear.

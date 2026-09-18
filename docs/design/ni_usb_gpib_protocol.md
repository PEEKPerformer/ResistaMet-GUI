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
purpose of the alternate endpoints.

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

### 2.9 Shutdown

One register-write instruction with two writes: bank 1 addr 0x0a value
0x02 (AUXMR chip reset), then bank 3 addr 0x10 value 0x00. Then release
the interface. (Sequence captured from NI's Windows driver.)

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
of opcodes in one message is untested.

### 3.2 Host -> device opcodes

| Opcode | Instruction | Fixed header bytes after the opcode | Payload | Reply |
|--------|-------------|--------------------------------------|---------|-------|
| 0x01 | Take control (adapter becomes active controller with ATN true) | `s 00 00` where s = 0x01 synchronous, 0x00 asynchronous | none | 12-byte status reply |
| 0x03 | Reserved / unknown; never observed in use | - | - | - |
| 0x04 | Termination block | `00 00 00` | none | n/a (also appears at the end of replies) |
| 0x06 | Go to standby (ATN false) | `00 00 00` | none | 12-byte status reply, id 0x06 |
| 0x07 | Parallel poll | `t 00 00` where t = timeout code (only 0xf0 observed) | none | status block + 1 result byte |
| 0x08 | Register read | `n` (count of reads) | n × (`bank addr`) | see 3.5 |
| 0x09 | Register write | `n 00` (count of writes) | n × (`bank addr value`) | 16-byte register-write reply |
| 0x0a | Read data from bus | `m e t cl ch 00 00` (see 5.2) | an embedded 0x09 block, see 5.2 | data blocks + status, see 5.2 |
| 0x0c | Command bytes (adapter drives ATN true for the duration) | `c 00 t` (c = 8-bit count code, t = timeout code) | up to 16 command bytes | 12-byte status reply |
| 0x0d | Write data to bus | `cl ch t 00 00 f 00` (see 5.1) | data bytes | 12-byte status reply |
| 0x0f | Interface clear pulse (IFC) | `00 00 00` | none | 12-byte status reply |

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

### 3.4 Register-access blocks

Register write block (opcode 0x09):

```
09 nn 00 | b1 a1 v1 | b2 a2 v2 | ... | pad to x4
```
- `nn` = number of (bank, addr, value) triplets (1 byte; 26 is known to work).
- `b` = bank: 1 = TNT4882 controller chip, 2 = unknown bank, 3 = unknown
  device-level bank.
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
| 0x0c | ADR                  | ADR0 |
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
| 0x35 | register-read end: `35 k 00 00`; k appears to be a count (uncertain: total or last-chunk; k mod 3 equals the number of reads mod 3) | 4 bytes |
| 0x36 | read-data block: id + 15 data bytes | 16 bytes |
| 0x37 | extended read-data block: `37 00` + 30 data bytes | 32 bytes |
| 0x38 | read-data status block (4.1) | 8 bytes |
| 0x04 | termination `04 00 00 00` | 4 bytes |

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
| 4 | 2 | count | **little-endian**, two's complement: (transferred - requested). Bytes not transferred = (0x10000 - count) & 0xffff; bytes transferred = requested - that. 0 when everything was transferred. **Meaningful only in replies to 0x0a, 0x0c, 0x0d.** Observed on the GPIB-USB-HS: replies to 0x01 and 0x06 carry `aa 55` here, and replies to 0x09 and 0x0f carry the count left over from the last data operation (`ff ff` after a reset, `52 ff` after a read that transferred 82 of 256). |
| 6 | 2 | unused | observed 0x00 0x00 (sources); on the GPIB-USB-HS `ff ff` in most replies, `01 00` in a read reply that filled the requested count, `00 00` after a successful 0x0d |

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
| 0x08 | no listener addressed | data write as controller with no listener addressed |
| 0x0a | device-side timeout | the device timeout (section 7) expired; the partial count is valid |
| other (6, 7, >= 11) | unknown | treat as a generic I/O error |

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
to work.

### 5.1 Write data (0x0d)

```
0d cl ch t 00 00 f 00 <data...> <pad to x4> 04 00 00 00
```
- `cl ch` = -(length) 16-bit little-endian (3.3). Max length 0xffff.
- `t` = timeout code.
- `f` = 0x08 to assert EOI with the last byte, 0x00 otherwise.
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
- The embedded 0x09 block (two AUXMR writes: 0x51 holdoff immediately,
  0x55 clear END) is always present; its status follows the read status
  in the reply, so it is evidently executed after the read. It stops the
  talker at the byte boundary and clears the END latch for the next read.
  Total message length: 24 bytes.
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
  of the last block is stale data, not zeros.
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
  is not required once the adapter is CIC but is harmless.

### 5.4 Take control (0x01) / go to standby (0x06)

- Take control: `01 s 00 00 04 00 00 00`, s = 0x01 synchronous (wait for
  the current handshake to finish), 0x00 asynchronous. Reply: status
  reply. After success ibsta should show CIC (0x20) and ATN (0x10).
- Go to standby: `06 00 00 00 04 00 00 00`. Reply: status reply with id
  0x06. ATN false; the previously addressed talker may now source data.

### 5.5 Interface clear -- IFC pulse (0x0f)

`0f 00 00 00 04 00 00 00`. Reply: status reply. The device pulses IFC
(IEEE-488.1 requires >= 100 us; the pulse length is the device's) and the
adapter becomes CIC. Only meaningful when the adapter is system
controller (2.6 row 16 = 0x03). There is no separate "assert IFC" /
"release IFC" instruction; AUXMR 0x1e / 0x16 can be written via register
writes for a manual pulse if ever needed.

### 5.6 Remote enable on / off

Register write, one write: bank 1 addr 0x0a value 0x1f (REN on) or 0x17
(REN off). Reply: 16-byte register-write reply. A device enters remote
state when REN is true and it is addressed to listen.

### 5.7 Take / release system control

Register write:
- Take: `01 1c 03` (CMDR set SC), `01 0a 16` (clear IFC) -- 2 writes.
- Release: `01 0a 17` (clear REN), `01 0a 16` (clear IFC), `01 0a 14`
  (disable system control), `01 1c 02` (CMDR clear SC) -- 4 writes.

### 5.8 Device clear

Command bytes (5.3):
- Selected device clear of address N: `3f 20+N 04` (UNL, LAD N, SDC).
- Universal device clear: `14` (DCL).
Then re-address before the next data transfer.

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
then read the bulk reply.

### 5.12 Status query

Control request 0x21 with wValue 0x0200 (wLength 8) returns the current
8-byte status block (ibsta) without disturbing the bus. Poll this for
SRQI (0x1000) when implementing a wait-for-SRQ, or use the interrupt
endpoint with the monitor mask (2.5). With a mask set, the interrupt IN
endpoint delivers one 8-byte status block whenever a monitored bit becomes
set.

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

Register write AUXMR = 0x05.

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
CIC, pulse IFC (5.5) or take control (5.4).

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
reported through the status block rather than as a USB error.

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
   send `00 00`.
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
   overflow error.
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
    rejected); resend them.
14. **Register-write reply** is exactly 16 bytes and byte 8 must equal the
    number of writes sent; a smaller value means the device stopped at a
    bad (bank, addr) pair.
15. **Unknown registers.** Bank 3 addr 0x10, bank 2 addrs 0x00-0x02 and
    bank 1 addr 0x0f are written with fixed values at init because NI's
    driver does; their meaning is unknown. Do not omit them.
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

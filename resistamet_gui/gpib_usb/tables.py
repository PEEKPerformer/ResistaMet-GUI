"""Constants of the NI GPIB-USB protocol, transcribed from the specification.

The numbers here are the facts an implementer needs and nothing more: which
USB ids are which adapter and where their endpoints are (§1), the control
requests (§2.2-2.4), the 26-write register initialisation and its T1 rows
(§2.6-2.9), the ibsta bits and error codes (§4), the device timeout table
(§7.1) with the expiry each of two adapters was timed at under its codes
(§7.3), and the IEEE-488.1 command bytes (§6). No parsing, no I/O; the codec
that turns these into messages lives in ``protocol``.

Section numbers refer to ``docs/design/ni_usb_gpib_protocol.md``.
"""
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# --------------------------------------------------------------------------
# §1 devices
# --------------------------------------------------------------------------

VENDOR_ID = 0x3923

PID_USB_B = 0x702A               # firmware loaded
PID_USB_B_PRE_FIRMWARE = 0x702B  # not usable until firmware is uploaded
PID_HS = 0x709B
PID_HS_PLUS = 0x7618
PID_KUSB_488A = 0x725C
PID_MC_USB_488 = 0x725D


@dataclass(frozen=True)
class Model:
    """What differs between adapters: endpoints and the attach extras (§1.2, §2.4)."""

    name: str
    product_id: int
    endpoint_out: int        # libusb address of the primary bulk OUT
    endpoint_in: int         # libusb address of the primary bulk IN (0x80 | n)
    endpoint_interrupt: int  # the SRQ push arrives here (§2.5, §10.4.2)
    needs_firmware: bool     # enumerates but cannot be driven
    readiness_poll: bool     # §2.3 applies (not on the USB-B)
    hs_plus_extras: bool     # the three extra control requests of §2.4
    #: The alternate bulk pair of §1.2: raw data of 0x0e writes goes out here
    #: and raw data of 0x0b reads comes in here (§10.1.3, §10.5.2). None where
    #: the model lacks one of the two (the USB-B has only an alternate IN), in
    #: which case only the framed 0x0a / 0x0d paths are used.
    endpoint_out_raw: Optional[int] = None
    endpoint_in_raw: Optional[int] = None
    #: NI's driver was captured driving this model (§10), so the raw use of its
    #: alternate pair and NI's other instructions are established on it. The
    #: GPIB-USB-HS only: for the others the endpoint numbers are inherited and
    #: what the alternate pair carries is not established (§1.2, §11.4).
    ni_captured: bool = False

    @property
    def raw_endpoints(self) -> bool:
        return self.endpoint_out_raw is not None and self.endpoint_in_raw is not None


MODELS: Dict[int, Model] = {
    PID_USB_B: Model('GPIB-USB-B', PID_USB_B, 0x02, 0x82, 0x84, False, False, False),
    PID_USB_B_PRE_FIRMWARE: Model('GPIB-USB-B (no firmware)', PID_USB_B_PRE_FIRMWARE,
                                  0x02, 0x82, 0x84, True, False, False),
    # The raw pair was observed on the HS (§10). The KUSB-488A and USB-488 are
    # said to share its endpoints and protocol, the HS+ has its own alternate
    # pair (§1.1, §1.2); all three are inherited, and none takes NI's instructions.
    PID_HS: Model('GPIB-USB-HS', PID_HS, 0x02, 0x84, 0x81, False, True, False, 0x06, 0x88, ni_captured=True),
    PID_HS_PLUS: Model('GPIB-USB-HS+', PID_HS_PLUS, 0x01, 0x82, 0x83, False, True, True, 0x04, 0x85),
    PID_KUSB_488A: Model('KUSB-488A', PID_KUSB_488A, 0x02, 0x84, 0x81, False, True, False, 0x06, 0x88),
    PID_MC_USB_488: Model('USB-488', PID_MC_USB_488, 0x02, 0x84, 0x81, False, True, False, 0x06, 0x88),
}

# --------------------------------------------------------------------------
# §2.2 control requests (all device-to-host)
# --------------------------------------------------------------------------

REQUEST_TYPE_VENDOR_DEVICE = 0xC0     # IN | Vendor | Device
REQUEST_TYPE_VENDOR_INTERFACE = 0xC1  # IN | Vendor | Interface (0xf8 only)
REQUEST_TYPE_VENDOR_DEVICE_OUT = 0x40  # OUT | Vendor | Device (0x3b only, §10.4.2)


@dataclass(frozen=True)
class ControlRequest:
    """One row of the §2.2 table."""

    request: int
    value: int
    index: int
    length: int
    request_type: int = REQUEST_TYPE_VENDOR_DEVICE


SERIAL_NUMBER_QUERY = ControlRequest(0x41, 0x0000, 0x0000, 16)
READINESS_QUERY = ControlRequest(0x40, 0x0000, 0x0000, 16)
STOP_REQUEST = ControlRequest(0x20, 0x0000, 0x0000, 8)
STATUS_QUERY = ControlRequest(0x21, 0x0200, 0x0000, 8)
#: Host-to-device, no data: sent after every interrupt push and before the
#: interrupt read is re-armed (§2.2, §10.4.2). Its function is not established.
SRQ_ACKNOWLEDGE = ControlRequest(0x3B, 0x0000, 0x0000, 0, REQUEST_TYPE_VENDOR_DEVICE_OUT)
#: NI keeps one interrupt read of this size outstanding (§2.5, §10.4.2).
INTERRUPT_READ_LENGTH = 64

#: HS+ only, issued in this order after the readiness poll (§2.4), with the
#: reply each is expected to produce.
HS_PLUS_INIT_REQUESTS: Tuple[Tuple[ControlRequest, bytes], ...] = (
    (ControlRequest(0x48, 0x0000, 0x0000, 16),
     bytes.fromhex('48f3300000000000' '0000000000000000')),
    (ControlRequest(0x4B, 0x0001, 0x0000, 2), bytes.fromhex('4b00')),
    (ControlRequest(0xF8, 0x0000, 0x0001, 9, REQUEST_TYPE_VENDOR_INTERFACE),
     bytes.fromhex('f80100000001000000')),
)

#: Observed on GPIB-USB-HS 01CEE482 (bcdDevice 0x101): the readiness reply is
#: ``40 01 00 01 30 01 19 08 00 00 65``, i.e. bytes 6/7/10 differ from the
#: values the specification lists; nonzero still means ready.
READINESS_ATTEMPTS = 50
READINESS_INTERVAL_S = 0.1
READINESS_USB_TIMEOUT_MS = 100
CONTROL_TIMEOUT_MS = 1000

# --------------------------------------------------------------------------
# §4.2 ibsta bits (NI-488.2)
# --------------------------------------------------------------------------

IBSTA_ERR = 0x8000
IBSTA_TIMO = 0x4000
IBSTA_END = 0x2000
IBSTA_SRQI = 0x1000
IBSTA_RQS = 0x0800
IBSTA_SPOLL = 0x0400
IBSTA_EVENT = 0x0200
IBSTA_CMPL = 0x0100
IBSTA_LOK = 0x0080
IBSTA_REM = 0x0040
IBSTA_CIC = 0x0020
IBSTA_ATN = 0x0010
IBSTA_TACS = 0x0008
IBSTA_LACS = 0x0004
IBSTA_DTAS = 0x0002
IBSTA_DCAS = 0x0001

# --------------------------------------------------------------------------
# §4.3 error codes
# --------------------------------------------------------------------------

ERR_SUCCESS = 0x00
ERR_STOPPED = 0x01
ERR_ATN_ASSERTED = 0x02
ERR_NOT_ADDRESSED = 0x03
ERR_EOS_REJECTED = 0x04
ERR_NO_ACCEPTOR = 0x05
ERR_NOT_CIC = 0x07
ERR_NO_LISTENER = 0x08
ERR_TIMEOUT = 0x0A

ERROR_LABELS: Dict[int, str] = {
    ERR_SUCCESS: 'success',
    ERR_STOPPED: 'cut short by a stop request',
    ERR_ATN_ASSERTED: 'read attempted while ATN true',
    ERR_NOT_ADDRESSED: 'not addressed',
    ERR_EOS_REJECTED: 'EOS configuration rejected / command chunk too long',
    ERR_NO_ACCEPTOR: 'no acceptor on the bus',
    ERR_NOT_CIC: 'not controller in charge',
    ERR_NO_LISTENER: 'no listener addressed',
    ERR_TIMEOUT: 'device-side timeout',
}


def error_label(code: int) -> str:
    return ERROR_LABELS.get(code, 'unknown')


# --------------------------------------------------------------------------
# §7.1 device timeout codes
# --------------------------------------------------------------------------

TIMEOUT_DISABLED_CODE = 0xF0

#: (limit in seconds, code), smallest limit first.
TIMEOUT_TABLE: Tuple[Tuple[float, int], ...] = (
    (10e-6, 0xF1), (30e-6, 0xF2), (100e-6, 0xF3), (300e-6, 0xF4),
    (1e-3, 0xF5), (3e-3, 0xF6), (10e-3, 0xF7), (30e-3, 0xF8),
    (100e-3, 0xF9), (300e-3, 0xFA), (1.0, 0xFB), (3.0, 0xFC),
    (10.0, 0xFD), (30.0, 0xFE), (100.0, 0xFF), (300.0, 0x01), (1000.0, 0x02),
)
#: The longest finite device timeout the table offers.
TIMEOUT_MAX_S = TIMEOUT_TABLE[-1][0]

#: §7.3: how long GPIB-USB-HS 013CC9DF waits under a code before it ends the
#: instruction itself with error 0x0a, in seconds, timed on the wire with
#: NI's driver from the submission of the OUT to the completion of the reply.
#: The limits above are nominal; these are one unit's figures. Where a code
#: was timed more than once the longest is kept here, for host waits (0xfc:
#: seven cases, 4.195316-4.196156; 0xfb and 0xfe include a 0x0b cut off
#: with data arriving, 1.050232 and 33.555506), and the shortest below, for
#: choosing a code. Each is a power of two in microseconds plus 0.8-1.9 ms,
#: but no rounding of the nominal value gives all ten exponents, so the
#: figures are table facts, not computed.
TIMEOUT_EXPIRY_MEASURED_S: Dict[int, float] = {
    0xF5: 0.002285,    # nominal 1 ms
    0xF6: 0.005465,    # nominal 3 ms
    0xF7: 0.017785,    # nominal 10 ms
    0xF8: 0.034093,    # nominal 30 ms
    0xF9: 0.132455,    # nominal 100 ms
    0xFA: 0.263887,    # nominal 300 ms: the one code that expires early
    0xFB: 1.050232,    # nominal 1 s
    0xFC: 4.196156,    # nominal 3 s
    0xFD: 16.778423,   # nominal 10 s
    0xFE: 33.555506,   # nominal 30 s
}
#: The shortest of the same unit's figures per code (§7.3).
TIMEOUT_EXPIRY_MEASURED_SHORTEST_S: Dict[int, float] = {
    0xF5: 0.002285,
    0xF6: 0.005348,
    0xF7: 0.017719,
    0xF8: 0.034088,
    0xF9: 0.132272,
    0xFA: 0.263436,
    0xFB: 1.049837,
    0xFC: 4.195316,
    0xFD: 16.778260,
    0xFE: 33.555258,
}

#: §7.3, "A second unit expires at other times": how long GPIB-USB-HS
#: 01CEE482 waits under a code, in seconds, timed on the bench with this
#: driver's messages (2026-09-21). Exact to the millisecond across repeats,
#: the same for counts 1, 10 and 20480, and not powers of two: every figure
#: is 1.25 times a round one -- 0.1, 0.3, 1, 3, 16 and 33 s -- which is the
#: nominal limit up to 0xfc and, to the second, the other unit's power of two
#: for 0xfd and 0xfe. The 0xf9 figure is a session total; its wire was not
#: logged. Whether the unit, its firmware or the message differs is not
#: established, so a host wait outlasts both tables and a code is chosen
#: from the shorter of the two. The comment on each row is the ratio to the
#: 013CC9DF figure above. The codes below 0xf9 were not timed on this unit.
TIMEOUT_EXPIRY_BENCH_S: Dict[int, float] = {
    0xF9: 0.127,       # nominal 100 ms; 0.96; a session total, the wire not logged
    0xFA: 0.375,       # nominal 300 ms; 1.42
    0xFB: 1.250,       # nominal 1 s; 1.19
    0xFC: 3.750,       # nominal 3 s; 0.89
    0xFD: 20.000,      # nominal 10 s; 1.19
    0xFE: 41.250,      # nominal 30 s; 1.23
}
#: The factor between the second unit's expiry and its round figure (§7.3).
TIMEOUT_EXPIRY_BENCH_RATIO = 1.25

#: §7.3, inference and not measurement, for the codes nobody timed: the
#: smallest power of two in microseconds not below the nominal limit. It is
#: the larger of the specification's two candidates, which §7.2 says a host
#: wait should assume. No code timed on 013CC9DF exceeded it; 01CEE482 does
#: under 0xfb, 0xfd and 0xfe, so ``timeout_expiry_s`` does not use it bare.
#: The four codes it predicted before they were timed, 0xf5-0xf8, came out as
#: it said and are in the measured table.
TIMEOUT_EXPIRY_INFERRED_S: Dict[int, float] = {
    0xF1: 16e-6, 0xF2: 32e-6, 0xF3: 128e-6, 0xF4: 512e-6,
    0xFF: 134.217728, 0x01: 536.870912, 0x02: 1073.741824,
}
#: The other candidate of §7.3's inference table: the power of two nearest the
#: nominal limit on a logarithmic scale. For 0xf1, 0xf4 and 0x01 it falls below
#: the nominal limit, as the measured 0xfa does (0.2635 s for 300 ms).
TIMEOUT_EXPIRY_INFERRED_NEAREST_S: Dict[int, float] = {
    0xF1: 8e-6, 0xF2: 32e-6, 0xF3: 128e-6, 0xF4: 256e-6,
    0xFF: 134.217728, 0x01: 268.435456, 0x02: 1073.741824,
}

#: The nominal limit of §7.1 by code.
TIMEOUT_NOMINAL_S: Dict[int, float] = {code: limit for limit, code in TIMEOUT_TABLE}


def _power_of_two_not_below(seconds: float) -> float:
    """The smallest power of two in microseconds not below ``seconds`` (§7.3's inference rule)."""
    return (1 << (round(seconds * 1e6) - 1).bit_length()) / 1e6


def _check_timeout_code(code: int) -> None:
    if code not in TIMEOUT_NOMINAL_S:
        raise ValueError('0x%02x is not a device timeout code' % code)


def timeout_expiry_s(code: int) -> Optional[float]:
    """The longest either timed adapter may wait under ``code`` before ending the instruction (§7.3).

    For host waits (§7.2), which must outlast it. Two GPIB-USB-HS units
    were timed and disagree: 013CC9DF under NI's driver
    (``TIMEOUT_EXPIRY_MEASURED_S``) and 01CEE482 under this one
    (``TIMEOUT_EXPIRY_BENCH_S``). §7.3 says a host wait must outlast both
    until the cause is established, so for a code both were timed under
    this is the larger figure. Where 01CEE482 was not timed -- 0xf5-0xf8,
    timed on 013CC9DF only, and the codes nobody timed -- its figure is
    estimated as 1.25 times the larger of the nominal limit (§7.1) and
    the power of two of §7.3's inference column, the rule §7.2 gives, and
    the larger of that and any measured figure is returned. That rule is
    the second unit's pattern made safe for the first: its round figure
    was the nominal limit for 0xf9-0xfc and, to the second, the first
    unit's power of two for 0xfd and 0xfe, and applied to the six codes
    both units were timed under it gives a figure not below either unit's
    expiry (0xfd: 1.25 x 16.78 = 20.97 s against the 20.0 s measured; the
    bare power of two, 16.78 s, falls short, and 1.25 x nominal, 12.5 s,
    further). None for the disabled code 0xf0, which never expires.
    """
    if code == TIMEOUT_DISABLED_CODE:
        return None
    _check_timeout_code(code)
    timed = [table[code] for table in (TIMEOUT_EXPIRY_MEASURED_S, TIMEOUT_EXPIRY_BENCH_S) if code in table]
    if code not in TIMEOUT_EXPIRY_BENCH_S:
        nominal = TIMEOUT_NOMINAL_S[code]
        timed.append(TIMEOUT_EXPIRY_BENCH_RATIO * max(_power_of_two_not_below(nominal), nominal))
    return max(timed)


def timeout_expiry_least_s(code: int) -> Optional[float]:
    """The least any adapter is known to wait under ``code`` before ending the instruction (§7.3).

    For choosing a code: a VISA timeout is the least time to wait before
    reporting one, so the code for a timeout must not expire before it.
    For a code that was timed, the shortest figure of every unit timed
    under it (0xf5-0xf8 were timed on 013CC9DF only). For a code nobody
    timed, the smallest of its nominal limit and the two powers of two of
    §7.3's inference table: §7.2's estimate, the larger power of two, is
    an upper figure for host waits, and the one measured code that ends
    early, 0xfa, ended at the smaller candidate, below nominal and far
    below the larger (0.262 s against 0.524 s). None for the disabled
    code 0xf0.
    """
    if code == TIMEOUT_DISABLED_CODE:
        return None
    _check_timeout_code(code)
    timed = [table[code] for table in (TIMEOUT_EXPIRY_MEASURED_SHORTEST_S, TIMEOUT_EXPIRY_BENCH_S)
             if code in table]
    if timed:
        return min(timed)
    return min(TIMEOUT_NOMINAL_S[code], TIMEOUT_EXPIRY_INFERRED_S[code], TIMEOUT_EXPIRY_INFERRED_NEAREST_S[code])

# --------------------------------------------------------------------------
# §2.6 / §2.7 / §2.9 register sequences
# --------------------------------------------------------------------------

#: (AUXRI write, AUXRB write, KEYREG write) per requested T1 (§2.7).
T1_ROWS: Tuple[Tuple[int, Tuple[int, int, int]], ...] = (
    (350, (0xE9, 0xA4, 0x20)),
    (500, (0xE9, 0xA4, 0x00)),
    (1100, (0xE9, 0xA0, 0x00)),
)
T1_DEFAULT_ROW: Tuple[int, int, int] = (0xE1, 0xA0, 0x00)  # > 1100 ns -> 2000 ns


def t1_writes(t1_ns: int) -> Tuple[Tuple[int, int, int], ...]:
    """Register writes #12-#14 of §2.6 for the requested T1 delay."""
    for limit, (auxri, auxrb, keyreg) in T1_ROWS:
        if t1_ns <= limit:
            break
    else:
        auxri, auxrb, keyreg = T1_DEFAULT_ROW
    return ((1, 0x0A, auxri), (1, 0x0A, auxrb), (1, 0x17, keyreg))


def register_init_writes(own_address: int = 0, system_controller: bool = True,
                         t1_ns: int = 2000,
                         secondary: Optional[int] = None) -> Tuple[Tuple[int, int, int], ...]:
    """The 26 (bank, addr, value) writes of §2.6, in order.

    ``secondary`` is the adapter's own secondary address (rows 20-22). The
    controller never passes one: the adapter answers to a primary address only.
    """
    if not 0 <= own_address <= 30:
        raise ValueError('own primary address %d outside 0..30' % own_address)
    if secondary is not None and not 0 <= secondary <= 31:
        raise ValueError('own secondary address %d outside 0..31' % secondary)
    adr_second = 0xE0 if secondary is None else 0x80 | secondary
    admr = 0x31 if secondary is None else 0x32
    bank2_sad = 0x00 if secondary is None else 0x60 | secondary
    auxri, auxrb, keyreg = t1_writes(t1_ns)
    # spec gap: write #3 carries the BIN bit only when an 8-bit EOS compare is
    # already configured; whether that bit does anything is unknown, so 0x81
    # is always sent and reads carry the compare mode themselves (§5.2).
    return (
        (3, 0x10, 0x00),                            # 1  device-level register, purpose unknown
        (1, 0x1C, 0x22),                            # 2  CMDR soft reset
        (1, 0x0A, 0x81),                            # 3  AUXRA: holdoff on all data
        (1, 0x06, 0x81),                            # 4  same value, see §2.6 note (a)
        (1, 0x0D, 0x01),                            # 5  HSSEL one-chip mode
        (1, 0x0A, 0x02),                            # 6  AUXMR chip reset
        (1, 0x1D, 0x80),                            # 7  IMR0: bit 7 set, enables off
        (1, 0x02, 0x00),                            # 8  IMR1 off
        (1, 0x04, 0x00),                            # 9  IMR2 off
        (1, 0x12, 0x00),                            # 10 IMR3 off
        (1, 0x0A, 0x51),                            # 11 AUXMR holdoff handshake immediately
        auxri,                                      # 12 AUXRI (T1)
        auxrb,                                      # 13 AUXRB (T1)
        keyreg,                                     # 14 KEYREG (T1)
        (1, 0x0A, 0x48),                            # 15 AUXRG: NTNL
        (1, 0x1C, 0x03 if system_controller else 0x02),  # 16 CMDR set/clear system controller
        (1, 0x0A, 0x16),                            # 17 AUXMR clear IFC
        (1, 0x0C, own_address),                     # 18 ADR own primary address
        (2, 0x00, own_address),                     # 19 bank-2 mirror of the primary address
        (1, 0x0C, adr_second),                      # 20 ADR second register (secondary/disabled)
        (1, 0x08, admr),                            # 21 ADMR mode 1 or mode 2
        (2, 0x01, bank2_sad),                       # 22 bank-2 mirror of the secondary address
        (2, 0x02, 0xFD),                            # 23 purpose unknown
        (1, 0x0F, 0x11),                            # 24 not in the register map; purpose unknown
        (1, 0x0A, 0x00),                            # 25 AUXMR immediate execute pon
        (1, 0x0A, 0x01),                            # 26 AUXMR clear parallel-poll flag
    )


REGISTER_INIT_COUNT = 26

#: §2.9, sent as one register-write instruction before releasing the interface.
SHUTDOWN_WRITES: Tuple[Tuple[int, int, int], ...] = (
    (1, 0x0A, 0x02),  # AUXMR chip reset
    (3, 0x10, 0x00),  # device-level register, purpose unknown
)

REN_ON_WRITE: Tuple[int, int, int] = (1, 0x0A, 0x1F)    # §5.6
REN_OFF_WRITE: Tuple[int, int, int] = (1, 0x0A, 0x17)   # §5.6

#: §5.13 bus line status register and its bits.
BSR_REGISTER: Tuple[int, int] = (1, 0x1F)
BSR_REN = 0x01
BSR_IFC = 0x02
BSR_SRQ = 0x04
BSR_EOI = 0x08
BSR_NRFD = 0x10
BSR_NDAC = 0x20
BSR_DAV = 0x40
BSR_ATN = 0x80

#: §2.4 USB-B serial number: bank 3, LSB first.
USB_B_SERIAL_REGISTERS: Tuple[Tuple[int, int], ...] = ((3, 0x0B), (3, 0x0A), (3, 0x09), (3, 0x08))

# --------------------------------------------------------------------------
# §6 IEEE-488.1 command bytes
# --------------------------------------------------------------------------

CMD_GTL = 0x01
CMD_SDC = 0x04
CMD_GET = 0x08
CMD_TCT = 0x09
CMD_LLO = 0x11
CMD_DCL = 0x14
CMD_SPE = 0x18
CMD_SPD = 0x19
CMD_UNL = 0x3F
CMD_UNT = 0x5F


def listen_address(pad: int) -> int:
    _check_primary(pad)
    return 0x20 + pad


def talk_address(pad: int) -> int:
    _check_primary(pad)
    return 0x40 + pad


def secondary_address(sad: int) -> int:
    if not 0 <= sad <= 31:
        raise ValueError('secondary address %d outside 0..31' % sad)
    return 0x60 + sad


def _check_primary(pad: int) -> None:
    if not 0 <= pad <= 30:
        raise ValueError('primary address %d outside 0..30' % pad)


def _with_secondary(byte: int, sad: Optional[int]) -> bytes:
    return bytes((byte,)) if sad is None else bytes((byte, secondary_address(sad)))


def address_listener_command(controller: int, pad: int, sad: Optional[int] = None) -> bytes:
    """Controller talks, instrument listens: ``3f 40+C 20+N [60+S]``."""
    return bytes((CMD_UNL, talk_address(controller))) + _with_secondary(listen_address(pad), sad)


def address_talker_command(controller: int, pad: int, sad: Optional[int] = None) -> bytes:
    """Instrument talks, controller listens: ``3f 20+C 40+N [60+S]``."""
    return bytes((CMD_UNL, listen_address(controller))) + _with_secondary(talk_address(pad), sad)


def serial_poll_enable_command(controller: int, pad: int, sad: Optional[int] = None) -> bytes:
    """``3f 20+C 18 40+N [60+S]`` -- the IEEE-488.1 serial-poll sequence of §5.9, §6.

    With ``SERIAL_POLL_DISABLE_COMMAND`` the serial poll that ran on the
    bench (``device_ops.serial_poll``); NI's driver polls with the 0x10
    instruction instead (§10.5.4).
    """
    return (bytes((CMD_UNL, listen_address(controller), CMD_SPE))
            + _with_secondary(talk_address(pad), sad))


SERIAL_POLL_DISABLE_COMMAND = bytes((CMD_SPD, CMD_UNT))


def address_listener_command_ni(controller: int, pad: int, sad: Optional[int] = None) -> bytes:
    """Controller talks, instrument listens, in NI's order: ``40+C 3f 20+N [60+S]`` (§10.2.3).

    What NI puts before every 0x0d and 0x0e of an instrument session
    (``40 3f 38`` for PAD 24, idn.pcap 0.5133); the talker form is the same
    as ``address_talker_command``.
    """
    return bytes((talk_address(controller), CMD_UNL)) + _with_secondary(listen_address(pad), sad)


#: The timeout code of every addressing 0x0c inside NI's instrument-session
#: messages, whatever the session's timeout (§10.1.9: 354 of 367 blocks).
NI_ADDRESSING_CODE = 0xFD
#: Bank-2 register 0x03 := 1, the last block of every NI instrument-session
#: message (§10.2.5). Sent alone, it arms one interrupt push for a service
#: request (§10.11); what else it means is not established.
BANK2_SESSION_MARK_WRITE: Tuple[int, int, int] = (2, 0x03, 0x01)


def bank2_session_writes(pad: int, sad: Optional[int], code: int) -> Tuple[Tuple[int, int, int], ...]:
    """NI's bank-2 session configuration (§10.2.4): 0x04 := 1, 0x05 := PAD, 0x06 := SAD byte, 0x07 := code."""
    _check_primary(pad)
    return ((2, 0x04, 0x01), (2, 0x05, pad),
            (2, 0x06, 0x00 if sad is None else secondary_address(sad)), (2, 0x07, code))


def addressed_command(pad: int, command: int, sad: Optional[int] = None,
                      controller: Optional[int] = None) -> bytes:
    """``[40+C] 3f 20+N [60+S] <command>`` for SDC, GET, GTL, LLO.

    With ``controller`` the adapter's own talk address leads, the order NI
    sends (``40 3f 38 01`` for go to local, §10.2.3, §10.7.4); without it the
    sequence is the §6 table's.
    """
    lead = b'' if controller is None else bytes((talk_address(controller),))
    return lead + bytes((CMD_UNL,)) + _with_secondary(listen_address(pad), sad) + bytes((command,))

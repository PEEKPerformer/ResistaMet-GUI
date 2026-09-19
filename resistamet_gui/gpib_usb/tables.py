"""Constants of the NI GPIB-USB protocol, transcribed from the specification.

The numbers here are the facts an implementer needs and nothing more: which
USB ids are which adapter and where their endpoints are (§1), the control
requests (§2.2-2.4), the 26-write register initialisation and its T1 rows
(§2.6-2.9), the ibsta bits and error codes (§4), the device timeout table
(§7.1), and the IEEE-488.1 command bytes (§6). No parsing, no I/O; the codec
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

    @property
    def raw_endpoints(self) -> bool:
        return self.endpoint_out_raw is not None and self.endpoint_in_raw is not None


MODELS: Dict[int, Model] = {
    PID_USB_B: Model('GPIB-USB-B', PID_USB_B, 0x02, 0x82, 0x84, False, False, False),
    PID_USB_B_PRE_FIRMWARE: Model('GPIB-USB-B (no firmware)', PID_USB_B_PRE_FIRMWARE,
                                  0x02, 0x82, 0x84, True, False, False),
    # The raw pair was observed on the HS (§10); the KUSB-488A and USB-488 share
    # its endpoints and protocol (§1.1), the HS+ has its own alternate pair (§1.2).
    PID_HS: Model('GPIB-USB-HS', PID_HS, 0x02, 0x84, 0x81, False, True, False, 0x06, 0x88),
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

#: §7.3: how long a GPIB-USB-HS really waits under a code before it ends the
#: instruction itself with error 0x0a, in seconds, timed on the wire with
#: NI's driver. The limits above are nominal; these are what a host wait has
#: to outlast. Where a code was timed more than once the longest figure is
#: kept (0xfc: six cases, 4.195316-4.196156; 0xfe: two). Each is a power of
#: two in microseconds plus 0.8-1.9 ms, but no rounding of the nominal value
#: gives all six exponents, so the figures are table facts, not computed.
TIMEOUT_EXPIRY_MEASURED_S: Dict[int, float] = {
    0xF9: 0.132272,    # nominal 100 ms
    0xFA: 0.263541,    # nominal 300 ms: the one code that expires early
    0xFB: 1.049837,    # nominal 1 s
    0xFC: 4.196156,    # nominal 3 s
    0xFD: 16.778423,   # nominal 10 s
    0xFE: 33.555345,   # nominal 30 s
}
#: The most a reply was seen to trail the power of two behind its expiry, in
#: twelve timed-out instructions (§7.2).
TIMEOUT_EXPIRY_JITTER_S = 1.9e-3

#: §7.3, inference and not measurement: for the codes nobody timed, the
#: smallest power of two in microseconds not below the nominal limit. It is
#: the larger of the specification's two candidates, which §7.2 says a host
#: wait should assume, and no measured code exceeded it.
TIMEOUT_EXPIRY_INFERRED_S: Dict[int, float] = {
    0xF1: 16e-6, 0xF2: 32e-6, 0xF3: 128e-6, 0xF4: 512e-6,
    0xF5: 1024e-6, 0xF6: 4096e-6, 0xF7: 16384e-6, 0xF8: 32768e-6,
    0xFF: 134.217728, 0x01: 536.870912, 0x02: 1073.741824,
}


def timeout_expiry_s(code: int) -> Optional[float]:
    """The adapter's own wait under ``code``: measured if it was, else inferred (§7.3).

    None for the disabled code 0xf0, which never expires.
    """
    if code == TIMEOUT_DISABLED_CODE:
        return None
    measured = TIMEOUT_EXPIRY_MEASURED_S.get(code)
    if measured is not None:
        return measured
    try:
        return TIMEOUT_EXPIRY_INFERRED_S[code]
    except KeyError:
        raise ValueError('0x%02x is not a device timeout code' % code) from None

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


def addressed_command(pad: int, command: int, sad: Optional[int] = None,
                      controller: Optional[int] = None) -> bytes:
    """``[40+C] 3f 20+N [60+S] <command>`` for SDC, GET, GTL, LLO.

    With ``controller`` the adapter's own talk address leads, the order NI
    sends (``40 3f 38 01`` for go to local, §10.2.3, §10.7.4); without it the
    sequence is the §6 table's.
    """
    lead = b'' if controller is None else bytes((talk_address(controller),))
    return lead + bytes((CMD_UNL,)) + _with_secondary(listen_address(pad), sad) + bytes((command,))

import platform
import re
import time
from dataclasses import dataclass
from typing import Optional

import pyvisa


@dataclass(frozen=True)
class ModelSpec:
    """Documented capabilities of one Keithley 2400-series model.

    Source/measure limits come from the Keithley datasheets; ``family``
    distinguishes the original 2400 SCPI surface (2400/2410/2420/2425/2430/
    2440) from the 2450's TSP+SCPI surface, which diverges in some places.
    """
    model: str
    max_source_v: float
    max_source_i: float
    max_power_w: float
    family: str
    notes: str = ""


# Keyed by the four-digit model number that appears in the IDN string,
# e.g. "MODEL 2420" -> "2420". Sourced from the 2400-series datasheets.
# Add new entries when community submissions land hardware traces.
_MODELS: dict[str, ModelSpec] = {
    "2400": ModelSpec("2400", 200.0, 1.05, 22.0, family="2400"),
    "2401": ModelSpec("2401", 20.0,  1.05, 22.0, family="2400",
                       notes="Low-voltage variant of the 2400 (20V max)"),
    "2410": ModelSpec("2410", 1100.0, 1.05, 22.0, family="2400",
                       notes="High-voltage model — special handling for >100V"),
    "2420": ModelSpec("2420", 60.0,  3.05, 22.0, family="2400"),
    "2425": ModelSpec("2425", 100.0, 3.05, 22.0, family="2400"),
    "2430": ModelSpec("2430", 100.0, 3.05, 22.0, family="2400",
                       notes="Pulse mode supports up to 10A (5W avg)"),
    "2440": ModelSpec("2440", 40.0,  5.05, 22.0, family="2400"),
    "2450": ModelSpec("2450", 200.0, 1.05, 22.0, family="2450",
                       notes="Touchscreen successor — TSP+SCPI surface; "
                             "some FORM/STAT details may differ from 2400 family"),
}

_IDN_MODEL_RE = re.compile(r"MODEL\s+(\d{4})", re.IGNORECASE)


def humanize_connection_error(exc: BaseException, address: str = "") -> str:
    """Translate a raw pyvisa exception into a user-facing one-liner.

    The lab uses ResistaMet without staff hand-holding; raw pyvisa
    messages ("VI_ERROR_RSRC_NFOUND (0xBFFF0011): Insufficient location
    information…") cause panic. We map the common cases by VI error code
    when available, by message substring otherwise, and always preserve
    the address in the returned text so it stays searchable.

    Returns a single line ending in advice ("turn the instrument on",
    "check the cable", etc.). Callers should still keep the original
    traceback in logs for diagnostics.
    """
    code = getattr(exc, 'error_code', None)
    msg = str(exc)
    msg_lower = msg.lower()
    addr_hint = f" ({address})" if address else ""

    # Pure address-mismatch: our own list_resources() check raised
    # RuntimeError before pyvisa ever opened a session.
    if isinstance(exc, RuntimeError) and 'not found' in msg_lower:
        return (
            f"Instrument at {address or 'the configured address'} was not "
            f"detected. Check that it's powered on, the GPIB/USB cable is "
            f"firmly seated, and the address matches the front-panel setting. "
            f"Click OK to pick a different address from the detected instruments."
        )

    if isinstance(exc, pyvisa.errors.LibraryError) or 'no visa library' in msg_lower:
        if platform.system() == 'Darwin':
            return (
                "NI-VISA is not supported on macOS — the Keithley 2400 family "
                "needs a Windows host running NI-VISA, or a Prologix-style "
                "GPIB-to-USB adapter with pyvisa-py. Prologix has not been "
                "verified with ResistaMet. For routine use, run on the lab "
                "Windows PC instead."
            )
        return (
            "NI-VISA isn't installed on this PC. Download it from "
            "ni.com/visa, install, reboot, then try again."
        )

    # Timeouts and resource-not-found come through as VisaIOError with
    # specific codes. Map the codes we actually see on the bench.
    if code is not None:
        if code == pyvisa.constants.StatusCode.error_resource_not_found:
            return (
                f"No instrument responded at {address or 'the configured address'}. "
                f"Power on the Keithley, check the cable, or pick a different "
                f"address from the GPIB dialog."
            )
        if code == pyvisa.constants.StatusCode.error_timeout:
            return (
                f"Timeout while talking to the instrument{addr_hint}. It may "
                f"be hung — try power-cycling it. If this is the first "
                f"connection attempt, the GPIB address may also be wrong."
            )
        if code == pyvisa.constants.StatusCode.error_resource_busy:
            return (
                f"The instrument{addr_hint} is busy. Another program "
                f"(Kickstart, LabVIEW, an older ResistaMet window) probably "
                f"has it open. Close that program and try again."
            )

    # Fallback substring matches for older pyvisa builds where error_code
    # isn't populated.
    if 'timeout' in msg_lower:
        return (
            f"Timeout while talking to the instrument{addr_hint}. Power-cycle "
            f"the Keithley or verify the GPIB address."
        )
    if 'rsrc_nfound' in msg_lower or 'insufficient location' in msg_lower:
        return (
            f"No instrument responded at {address or 'the configured address'}. "
            f"Power on the Keithley, check the cable, or pick a different "
            f"address from the GPIB dialog."
        )

    # Last resort: surface the raw message but framed so the user knows
    # what to try.
    return (
        f"Could not connect to the instrument{addr_hint}. {msg}. "
        f"Check power, cabling, and address; if it persists, share this "
        f"message with whoever set the rig up."
    )


def parse_model_from_idn(idn: str) -> Optional[ModelSpec]:
    """Parse a Keithley *IDN? string and return the matching :class:`ModelSpec`.

    Returns ``None`` if the IDN doesn't match a documented 2400-family model.
    Callers should treat ``None`` as "unknown — proceed with defaults."
    """
    if not idn:
        return None
    m = _IDN_MODEL_RE.search(idn)
    if not m:
        return None
    return _MODELS.get(m.group(1))


def known_models() -> tuple[str, ...]:
    """Return the model numbers we have a :class:`ModelSpec` entry for."""
    return tuple(_MODELS.keys())


class VisaInstrument:
    def __init__(self, resource: str, timeout_ms: int = 5000):
        self.resource_str = resource
        self.timeout = timeout_ms
        self.rm: Optional[pyvisa.ResourceManager] = None
        self.dev = None

    def connect(self):
        self.rm = pyvisa.ResourceManager()
        resources = self.rm.list_resources()
        if self.resource_str not in resources:
            raise RuntimeError(f"Instrument at '{self.resource_str}' not found. Available: {', '.join(resources)}")
        self.dev = self.rm.open_resource(self.resource_str)
        self.dev.timeout = self.timeout
        # Common VISA settings (some backends infer terminations):
        try:
            self.dev.read_termination = '\n'
            self.dev.write_termination = '\n'
        except Exception:
            pass
        return self

    def idn(self) -> str:
        return self.query("*IDN?").strip()

    def reset_and_clear(self):
        self.write("*RST"); time.sleep(0.5)
        self.write("*CLS")

    def write(self, cmd: str):
        return self.dev.write(cmd)

    def query(self, cmd: str) -> str:
        return self.dev.query(cmd)

    def close(self):
        # Close only our own device session. Do NOT close the ResourceManager:
        # pyvisa caches one ResourceManager per VISA library process-wide, and
        # rm.close() terminates every session opened through it — including
        # other live instruments (e.g. an auxiliary sensor co-logging alongside
        # the Keithley). See pyvisa highlevel.ResourceManager.close: "this will
        # also terminate connections obtained from other ResourceManager
        # instances."
        try:
            if self.dev:
                self.dev.close()
        finally:
            self.dev = None
            self.rm = None


class Keithley2400(VisaInstrument):
    def detect_model(self) -> Optional[ModelSpec]:
        """Identify the connected instrument from its *IDN? response.

        Returns a :class:`ModelSpec` for documented 2400-family members,
        or ``None`` if the model number is not in the known table — in
        which case callers should proceed with conservative defaults
        rather than refusing to operate.
        """
        try:
            return parse_model_from_idn(self.idn())
        except Exception:
            return None

    def enable_autozero(self, on: bool = True):
        self.write(f":SYST:AZER:STAT {'ON' if on else 'OFF'}")

    def set_4wire(self, on: bool):
        self.write(":SYST:RSEN ON" if on else ":SYST:RSEN OFF")

    def setup_resistance(self, test_current: float, v_comp: float, nplc: float, auto_range: bool, four_wire: bool):
        self.set_4wire(four_wire)
        self.write(":SENS:FUNC:CONC OFF")
        self.write(":SENS:FUNC 'RES'")
        # Disable auto-ohms before configuring source/compliance
        # (auto-ohms is ON by default after selecting RES function
        # and rejects :SOUR:CURR:RANG, :SOUR:CURR, :SENS:VOLT:PROT)
        self.write(":SENS:RES:MODE MAN")
        self.write(":SOUR:FUNC CURR")
        self.write(f":SOUR:CURR:RANG {abs(test_current)}")
        self.write(f":SOUR:CURR {test_current}")
        self.write(f":SENS:VOLT:PROT {v_comp}")
        self.write(f":SENS:RES:NPLC {nplc}")
        if auto_range:
            self.write(":SENS:RES:MODE AUTO")
        else:
            rmax = v_comp / abs(test_current) if abs(test_current) > 0 else 210e6
            self.write(f":SENS:RES:RANG {rmax}")
        # Include STAT for hardware compliance detection (bit 3)
        self.write(":FORM:ELEM RES,STAT")

    def setup_source_voltage(self, voltage: float, i_comp: float, nplc: float, auto_range_curr: bool):
        self.set_4wire(False)
        self.write(":SENS:FUNC:CONC OFF")
        self.write(":SENS:FUNC 'CURR:DC'")
        self.write(":SOUR:FUNC VOLT")
        self.write(f":SOUR:VOLT:RANG {abs(voltage)}")
        self.write(f":SOUR:VOLT {voltage}")
        self.write(f":SENS:CURR:PROT {i_comp}")
        self.write(":SENS:CURR:RANG:AUTO ON" if auto_range_curr else ":SENS:CURR:RANG:AUTO OFF")
        if not auto_range_curr:
            self.write(f":SENS:CURR:RANG {i_comp}")
        self.write(f":SENS:CURR:NPLC {nplc}")
        # Keithley 2400 series returns elements in fixed order: VOLT, CURR, STAT
        self.write(":FORM:ELEM VOLT,CURR,STAT")

    def setup_source_current(self, current: float, v_comp: float, nplc: float, auto_range_volt: bool):
        self.set_4wire(False)
        self.write(":SENS:FUNC:CONC OFF")
        self.write(":SENS:FUNC 'VOLT:DC'")
        self.write(":SOUR:FUNC CURR")
        self.write(f":SOUR:CURR:RANG {abs(current)}")
        self.write(f":SOUR:CURR {current}")
        self.write(f":SENS:VOLT:PROT {v_comp}")
        self.write(":SENS:VOLT:RANG:AUTO ON" if auto_range_volt else ":SENS:VOLT:RANG:AUTO OFF")
        if not auto_range_volt:
            self.write(f":SENS:VOLT:RANG {v_comp}")
        self.write(f":SENS:VOLT:NPLC {nplc}")
        self.write(":FORM:ELEM VOLT,CURR,STAT")

    def setup_sweep(self, source_func: str, start: float, stop: float, step: float,
                     compliance: float, nplc: float, source_delay: float = 0.0):
        """Configure a linear staircase sweep.

        Args:
            source_func: 'VOLT' or 'CURR'
            start: Sweep start value
            stop: Sweep stop value
            step: Sweep step size (always positive)
            compliance: Compliance limit for the measured function
            nplc: Integration time
            source_delay: Delay per step in seconds
        """
        self.write(":SENS:FUNC:CONC OFF")
        if source_func == 'VOLT':
            self.write(":SOUR:FUNC VOLT")
            self.write(":SENS:FUNC 'CURR:DC'")
            self.write(f":SENS:CURR:PROT {compliance}")
            self.write(f":SENS:CURR:NPLC {nplc}")
            self.write(f":SOUR:VOLT:START {start}")
            self.write(f":SOUR:VOLT:STOP {stop}")
            self.write(f":SOUR:VOLT:STEP {step}")
            self.write(":SOUR:VOLT:MODE SWE")
        else:
            self.write(":SOUR:FUNC CURR")
            self.write(":SENS:FUNC 'VOLT:DC'")
            self.write(f":SENS:VOLT:PROT {compliance}")
            self.write(f":SENS:VOLT:NPLC {nplc}")
            self.write(f":SOUR:CURR:START {start}")
            self.write(f":SOUR:CURR:STOP {stop}")
            self.write(f":SOUR:CURR:STEP {step}")
            self.write(":SOUR:CURR:MODE SWE")
        self.write(":SOUR:SWE:SPAC LIN")
        self.write(":SOUR:SWE:RANG AUTO")
        points = int(round(abs(stop - start) / step)) + 1 if step != 0 else 1
        self.write(f":TRIG:COUN {points}")
        self.write(f":SOUR:DEL {source_delay}")
        self.write(":FORM:ELEM VOLT,CURR,STAT")
        return points

    def common_fast(self):
        self.write(":TRIG:DEL 0")
        self.write(":SOUR:DEL:AUTO ON")


"""Stateful in-memory Keithley 2400-series simulator.

Calibrated against captured SCPI traces from a Keithley 2420 (firmware C30,
GPIB::24, 60Hz line). The traces are committed under tests/fixtures/scpi_traces/
and each one is replayed through this simulator by ``test_fake_matches_hardware``
to confirm fidelity.

The simulator does NOT attempt to reproduce every Keithley response byte-for-byte.
What it DOES guarantee:

    1. Every SCPI command our codebase sends is accepted, mutates the right
       state, and produces the same kind of response (numeric vs string vs
       comma-list) as the real instrument.
    2. ``:READ?`` synthesizes V/I/R from a configurable DUT model (Ohm's law +
       compliance clamp) and returns elements in the *canonical* FORM:ELEM
       order (VOLT, CURR, RES, TIME, STAT) regardless of argument order — a
       documented 2400-series quirk.
    3. Compliance is signaled by setting bit 3 of the STAT element. The other
       STAT bits are returned at empirically-observed baseline values per
       function so traces remain comparable.
    4. The auto-ohms quirk: after ``:SENS:FUNC 'RES'``, attempts to write
       ``:SOUR:CURR``/``:SOUR:CURR:RANG``/``:SENS:VOLT:PROT`` queue error 825
       until ``:SENS:RES:MODE MAN`` is sent.
    5. ``:INIT:CONT ON`` queues error -113 (does not exist on this series).
    6. Sweep mode: when ``:SOUR:VOLT:MODE SWE`` (or CURR) is set, ``:READ?``
       returns ``TRIG:COUN`` triples computed from start/stop/step.
    7. Failure injection (``fail_next_query``) for retry-loop tests.

What it does NOT model:
    - Per-function range auto-selection beyond a coarse "auto" flag
    - Real measurement noise (returns clean Ohm's-law values)
    - Source-delay / NPLC timing (returns instantly)
    - Thermoelectric EMF (set ``dut_voltage_offset`` to fake it)
    - Filter timing (filter ON returns the same value as filter OFF)
"""
from __future__ import annotations

import math
import random
import re
from collections import deque
from typing import Optional

import pyvisa
import pyvisa.errors


# Canonical FORM:ELEM order. Argument *order* in :FORM:ELEM is ignored by the
# real instrument — the response always orders elements as below. We match
# that quirk.
_CANONICAL_ELEMS = ("VOLT", "CURR", "RES", "TIME", "STAT")

# Compliance bit in the FORM:ELEM STAT element. Confirmed by hardware diff:
#   stat_no_compliance ^ stat_compliance == 8 == bit 3.
# Note: this is NOT the same as the Measurement Event Register's compliance
# bit (+114 / bit 14). The FORM:ELEM STAT element uses a different bit layout.
_STAT_BIT_COMPLIANCE = 1 << 3

# Per-function STAT baseline values observed on the Keithley 2420 (firmware
# C30) with our 100Ω DUT. These are the bits that the instrument sets to
# encode "current source range, function active, output state" and similar
# steady-state info. We OR in bit 3 when in compliance.
_STAT_BASELINE = {
    "source_v": 4215812,    # source V, measure I, output ON
    "source_i": 4215812,
    "resistance": 4236292,  # RES function adds different bits (RES result valid)
    "sweep": 21508,         # sweep mode trigger model is different
    "default": 4215812,
}

# IDN string from the bench Keithley. Override in tests if needed.
DEFAULT_IDN = (
    "KEITHLEY INSTRUMENTS INC.,MODEL 2420,1230523,"
    "C30   Mar 17 2006 09:29:29/A02  /H/L"
)


def _idn_for_model(model: str) -> str:
    """Synthesize a 2400-family-shaped IDN string for the given model number.

    Used by ``FakeKeithley(model=...)`` so production code's
    ``detect_model()`` can identify the simulated instrument exactly the
    way it would identify a real one.
    """
    return (
        f"KEITHLEY INSTRUMENTS INC.,MODEL {model},9999999,"
        f"C30   Mar 17 2006 09:29:29/A02  /SIM"
    )


def _format_keithley(value: float) -> str:
    """Format a float as the Keithley 2400 does: ``+1.234567E+01``.

    Sign always present, 6 fractional digits, two-digit exponent with sign.
    """
    if math.isnan(value):
        return "+9.910000E+37"  # NAN/overflow magic per Keithley docs
    s = f"{value:+.6E}"
    # Python's default exponent format uses a 2-digit exponent on POSIX and
    # most Windows builds; that matches the hardware. Defensive normalize.
    head, exp = s.split("E")
    sign = "+" if exp[0] != "-" else "-"
    digits = exp.lstrip("+-").lstrip("0") or "0"
    if len(digits) == 1:
        digits = "0" + digits
    return f"{head}E{sign}{digits}"


def _format_bool(b: bool) -> str:
    return "1" if b else "0"


def _parse_bool(token: str) -> bool:
    t = token.strip().upper()
    if t in ("ON", "1", "TRUE"):
        return True
    if t in ("OFF", "0", "FALSE"):
        return False
    raise ValueError(f"not a bool token: {token!r}")


def _parse_float(token: str) -> float:
    return float(token.strip())


# Registry of *settable* properties keyed by SCPI prefix. Each entry maps to
# (state_key, kind). Used to keep the dispatcher concise.
_SETTABLE: dict[str, tuple[str, str]] = {
    ":SYST:RSEN":             ("syst_rsen", "bool"),
    ":SYST:AZER:STAT":        ("syst_azer", "azer"),       # ON/OFF/ONCE
    ":SENS:FUNC:CONC":        ("sens_func_conc", "bool"),
    ":SENS:VOLT:NPLC":        ("sens_volt_nplc", "float"),
    ":SENS:CURR:NPLC":        ("sens_curr_nplc", "float"),
    ":SENS:RES:NPLC":         ("sens_res_nplc", "float"),
    ":SENS:RES:OCOM":         ("sens_res_ocom", "bool"),
    ":SENS:VOLT:PROT":        ("sens_volt_prot", "float"),
    ":SENS:CURR:PROT":        ("sens_curr_prot", "float"),
    ":SENS:VOLT:RANG:AUTO":   ("sens_volt_rang_auto", "bool"),
    ":SENS:CURR:RANG:AUTO":   ("sens_curr_rang_auto", "bool"),
    ":SENS:VOLT:RANG":        ("sens_volt_rang", "float"),
    ":SENS:CURR:RANG":        ("sens_curr_rang", "float"),
    ":SENS:RES:RANG":         ("sens_res_rang", "float"),
    ":SENS:AVER:TCON":        ("sens_aver_tcon", "tcon"),
    ":SENS:AVER:COUN":        ("sens_aver_coun", "int"),
    ":SOUR:VOLT:RANG":        ("sour_volt_rang", "float"),
    ":SOUR:CURR:RANG":        ("sour_curr_rang", "float"),
    ":SOUR:VOLT:MODE":        ("sour_volt_mode", "mode"),
    ":SOUR:CURR:MODE":        ("sour_curr_mode", "mode"),
    ":SOUR:VOLT:START":       ("sour_volt_start", "float"),
    ":SOUR:VOLT:STOP":        ("sour_volt_stop", "float"),
    ":SOUR:VOLT:STEP":        ("sour_volt_step", "float"),
    ":SOUR:CURR:START":       ("sour_curr_start", "float"),
    ":SOUR:CURR:STOP":        ("sour_curr_stop", "float"),
    ":SOUR:CURR:STEP":        ("sour_curr_step", "float"),
    ":SOUR:SWE:SPAC":         ("sour_swe_spac", "str"),
    ":SOUR:SWE:RANG":         ("sour_swe_rang", "str"),
    ":SOUR:SWE:DIR":          ("sour_swe_dir", "str"),
    ":SOUR:DEL:AUTO":         ("sour_del_auto", "bool"),
    ":SOUR:DEL":              ("sour_del", "float"),
    ":TRIG:COUN":             ("trig_coun", "int"),
    ":TRIG:DEL":              ("trig_del", "float"),
    ":OUTP:SMOD":             ("outp_smod", "smod"),
}


# Queries that are simple state read-outs of a settable property.
_QUERY_TO_KEY: dict[str, tuple[str, str]] = {
    ":SYST:LFR?":              ("syst_lfr", "int_str"),
    ":SYST:RSEN?":             ("syst_rsen", "bool_str"),
    ":SYST:AZER:STAT?":        ("syst_azer", "azer_str"),
    ":SENS:FUNC?":             ("sens_func", "raw"),
    ":SENS:FUNC:CONC?":        ("sens_func_conc", "bool_str"),
    ":SENS:VOLT:NPLC?":        ("sens_volt_nplc", "nplc"),
    ":SENS:CURR:NPLC?":        ("sens_curr_nplc", "nplc"),
    ":SENS:RES:NPLC?":         ("sens_res_nplc", "nplc"),
    ":SENS:RES:MODE?":         ("sens_res_mode", "raw"),
    ":SENS:RES:OCOM?":         ("sens_res_ocom", "bool_str"),
    ":SENS:VOLT:PROT?":        ("sens_volt_prot", "k_float"),
    ":SENS:CURR:PROT?":        ("sens_curr_prot", "k_float"),
    ":SENS:VOLT:RANG?":        ("sens_volt_rang", "rang"),
    ":SENS:CURR:RANG?":        ("sens_curr_rang", "k_float"),
    ":SENS:VOLT:RANG:AUTO?":   ("sens_volt_rang_auto", "bool_str"),
    ":SENS:CURR:RANG:AUTO?":   ("sens_curr_rang_auto", "bool_str"),
    ":SENS:AVER?":             ("sens_aver", "bool_str"),
    ":SENS:AVER:TCON?":        ("sens_aver_tcon", "raw"),
    ":SENS:AVER:COUN?":        ("sens_aver_coun", "int_str"),
    ":SOUR:FUNC?":             ("sour_func", "raw"),
    ":SOUR:VOLT?":             ("sour_volt", "k_float"),
    ":SOUR:CURR?":             ("sour_curr", "k_float"),
    ":SOUR:VOLT:RANG?":        ("sour_volt_rang", "rang"),
    ":SOUR:CURR:RANG?":        ("sour_curr_rang", "k_float"),
    ":SOUR:VOLT:MODE?":        ("sour_volt_mode", "raw"),
    ":SOUR:CURR:MODE?":        ("sour_curr_mode", "raw"),
    ":SOUR:VOLT:START?":       ("sour_volt_start", "k_float"),
    ":SOUR:VOLT:STOP?":        ("sour_volt_stop", "k_float"),
    ":SOUR:CURR:START?":       ("sour_curr_start", "k_float"),
    ":SOUR:CURR:STOP?":        ("sour_curr_stop", "k_float"),
    ":SOUR:DEL?":              ("sour_del", "del"),
    ":SOUR:DEL:AUTO?":         ("sour_del_auto", "bool_str"),
    ":TRIG:COUN?":             ("trig_coun", "int_str"),
    ":TRIG:DEL?":              ("trig_del", "del"),
    ":OUTP?":                  ("outp", "bool_str"),
    ":OUTP:SMOD?":             ("outp_smod", "raw"),
}


class FakeKeithley:
    """A drop-in replacement for ``pyvisa.resources.MessageBasedResource``.

    Implements the subset of ``write``/``query``/``close`` plus the settable
    attributes (``timeout``, ``read_termination``, ``write_termination``)
    that the ResistaMet codebase actually uses.
    """

    def __init__(
        self,
        dut_resistance_ohms: float = 100.0,
        dut_voltage_offset: float = 0.0,
        idn: Optional[str] = None,
        model: Optional[str] = None,
        dut_resistance_callable=None,
        noise_rsd: float = 0.0,
    ):
        """Construct a fake instrument.

        Args:
            dut_resistance_ohms: Resistance the fake DUT presents to Ohm's law.
            dut_voltage_offset: Offset added to V (simulates thermoelectric EMF).
            idn: Override the *IDN? response verbatim. Mutually exclusive
                with ``model``.
            model: Four-digit model number ("2400", "2410", "2420", "2450",
                ...). When set, the fake generates an IDN with that model
                and the production code's ``detect_model()`` will identify
                it. Defaults to "2420" when neither ``idn`` nor ``model``
                is given (matches the bench reference instrument).
            dut_resistance_callable: Optional zero-arg callable returning a
                fresh resistance value on each :READ?. Lets tests simulate a
                DUT whose effective R changes during a run (e.g., sample
                heating, thermistor-like behavior). When set, supersedes
                ``dut_resistance_ohms`` for read-time computation.
            noise_rsd: Relative-standard-deviation of Gaussian noise added
                to the *measured* side of each reading (I in source-V mode,
                V in source-I mode). 0.0 disables (default — keeps every
                replay/fixture-driven test bit-exact). Typical demo value
                is 1e-3 (0.1% RSD) which makes the live trace look alive
                without crossing the σ derived from the datasheet specs.
        """
        if idn is not None and model is not None:
            raise ValueError("pass either idn= or model=, not both")
        if idn is None:
            idn = _idn_for_model(model) if model is not None else DEFAULT_IDN
        self._dut_r_static = dut_resistance_ohms
        self._dut_r_callable = dut_resistance_callable
        self.dut_voltage_offset = dut_voltage_offset
        self._idn = idn
        self._noise_rsd = float(noise_rsd)
        # Seeded so test runs that opt in are still deterministic per-process.
        self._noise_rng = random.Random(0xC0FFEE)

        # PyVISA-compatible knobs
        self.timeout = 5000
        self.read_termination = "\n"
        self.write_termination = "\n"

        # Failure injection
        self._fail_queries_remaining = 0
        self._fail_skip_remaining = 0
        self._fail_exception: Optional[BaseException] = None
        self._pre_inject_skip = 0   # set by FakeResourceManager.fail_next_open

        # Last-issued command (handy in tests)
        self.last_command: Optional[str] = None
        self.command_log: list[tuple[str, str]] = []  # (op, cmd) — not the response

        # State
        self._reset()

    @property
    def dut_resistance(self) -> float:
        if self._dut_r_callable is not None:
            return float(self._dut_r_callable())
        return self._dut_r_static

    @dut_resistance.setter
    def dut_resistance(self, value: float) -> None:
        self._dut_r_static = float(value)
        self._dut_r_callable = None

    # ------------------------------------------------------------------ state

    def _reset(self) -> None:
        """Restore the *RST default state observed on real hardware."""
        self.state: dict = {
            "syst_lfr": 60,
            "syst_rsen": False,
            "syst_azer": "ON",   # ON / OFF / ONCE
            "sens_func": '"CURR:DC"',
            "sens_func_conc": True,
            "sens_volt_nplc": 1.0,
            "sens_curr_nplc": 1.0,
            "sens_res_nplc": 1.0,
            "sens_res_mode": "AUTO",
            "sens_res_ocom": False,
            "sens_volt_prot": 21.0,
            "sens_curr_prot": 1.05e-4,
            "sens_volt_rang": 21.0,
            "sens_curr_rang": 1.05e-4,
            "sens_res_rang": 2.1e5,
            "sens_volt_rang_auto": True,
            "sens_curr_rang_auto": True,
            "sens_aver": False,
            "sens_aver_tcon": "REP",
            "sens_aver_coun": 10,
            "sour_func": "VOLT",
            "sour_volt": 0.0,
            "sour_curr": 0.0,
            "sour_volt_rang": 21.0,
            "sour_curr_rang": 1.05e-4,
            "sour_volt_mode": "FIX",
            "sour_curr_mode": "FIX",
            "sour_volt_start": 0.0,
            "sour_volt_stop": 0.0,
            "sour_volt_step": 0.0,
            "sour_curr_start": 0.0,
            "sour_curr_stop": 0.0,
            "sour_curr_step": 0.0,
            "sour_swe_spac": "LIN",
            "sour_swe_rang": "BEST",
            "sour_swe_dir": "UP",
            "sour_del": 0.001,
            "sour_del_auto": True,
            "trig_coun": 1,
            "trig_del": 0.0,
            "form_elem": list(_CANONICAL_ELEMS),
            "outp": False,
            "outp_smod": "NORM",
        }
        self._error_queue: deque[tuple[int, str]] = deque()

    # ------------------------------------------------------------ public API

    def write(self, cmd: str):
        cmd = cmd.strip()
        self.last_command = cmd
        self.command_log.append(("write", cmd))
        self._dispatch_write(cmd)
        return None

    def query(self, cmd: str) -> str:
        cmd = cmd.strip()
        # Honor pre-inject skip first (so we can let setup queries succeed
        # before the failure window opens for the polling loop).
        if self._fail_queries_remaining > 0:
            if self._pre_inject_skip > 0:
                self._pre_inject_skip -= 1
            else:
                self._fail_queries_remaining -= 1
                assert self._fail_exception is not None
                raise self._fail_exception
        self.last_command = cmd
        self.command_log.append(("query", cmd))
        return self._dispatch_query(cmd)

    def close(self) -> None:
        # Mirror real-device cleanup; no real resources to free.
        pass

    # ------------------------------------------------------- failure injection

    def fail_next_query(self, n: int = 1, exception: Optional[BaseException] = None) -> None:
        """Make the next ``n`` query calls raise instead of returning data.

        Used by retry-loop tests in ``test_workers_against_fake.py``.
        """
        if exception is None:
            exception = pyvisa.errors.VisaIOError(-1073807339)  # VI_ERROR_TMO
        self._fail_queries_remaining = n
        self._fail_exception = exception

    # ----------------------------------------------------------- dispatchers

    def _dispatch_write(self, cmd: str) -> None:
        upper = cmd.upper()

        # IEEE common commands
        if upper == "*RST":
            self._reset()
            return
        if upper == "*CLS":
            self._error_queue.clear()
            return
        if upper.startswith("*"):
            # Other common commands not modeled, no-op
            return

        # :SYST subsystem
        if upper.startswith(":SYST:ERR"):
            # only valid as query
            self._queue_error(-113, "Undefined header")
            return

        # :SENS:FUNC 'CURR:DC' / 'VOLT:DC' / 'RES'
        m = re.match(r":SENS:FUNC\s+(.+)", cmd, re.IGNORECASE)
        if m and not m.group(0).rstrip().endswith("?"):
            arg = m.group(1).strip()
            if arg.upper() in ("ON", "OFF") or "CONC" in upper:
                # not the function selector — fall through to settable
                pass
            else:
                # arg is e.g. "'RES'" or '"VOLT:DC"' — preserve the quoting
                self.state["sens_func"] = arg.replace("'", '"')
                # Selecting RES function defaults res_mode back to AUTO (the quirk!)
                if "RES" in arg.upper():
                    self.state["sens_res_mode"] = "AUTO"
                return

        # :SENS:RES:MODE MAN/AUTO
        m = re.match(r":SENS:RES:MODE\s+(MAN|AUTO)", cmd, re.IGNORECASE)
        if m:
            self.state["sens_res_mode"] = m.group(1).upper()
            return

        # :SOUR:FUNC VOLT/CURR
        m = re.match(r":SOUR:FUNC\s+(VOLT|CURR)", cmd, re.IGNORECASE)
        if m:
            self.state["sour_func"] = m.group(1).upper()
            return

        # :SOUR:VOLT <num>  or  :SOUR:CURR <num>
        m = re.match(r":SOUR:(VOLT|CURR)\s+([+\-0-9.eE]+)\s*$", cmd)
        if m:
            sub, val = m.group(1).upper(), m.group(2)
            try:
                fval = float(val)
            except ValueError:
                self._queue_error(-220, "Parameter error")
                return
            # Auto-ohms quirk: in RES function with mode AUTO, source/range
            # changes are rejected with error 825.
            if (sub == "CURR" and self._sense_func_short() == "RES"
                    and self.state["sens_res_mode"] == "AUTO"):
                self._queue_error(825, "Invalid with auto-ohms on")
                return
            self.state[f"sour_{sub.lower()}"] = fval
            return

        # :OUTP ON/OFF
        m = re.match(r":OUTP(?::STAT(?:e)?)?\s+(ON|OFF|0|1)\s*$", cmd, re.IGNORECASE)
        if m:
            self.state["outp"] = m.group(1).upper() in ("ON", "1")
            return

        # :SENS:AVER ON/OFF
        m = re.match(r":SENS:AVER\s+(ON|OFF|0|1)\s*$", cmd, re.IGNORECASE)
        if m:
            self.state["sens_aver"] = m.group(1).upper() in ("ON", "1")
            return

        # :FORM:ELEM <list>
        m = re.match(r":FORM:ELEM\s+(.+)", cmd, re.IGNORECASE)
        if m and not cmd.rstrip().endswith("?"):
            requested = [s.strip().upper() for s in m.group(1).split(",")]
            allowed = set(_CANONICAL_ELEMS)
            if not all(r in allowed for r in requested):
                self._queue_error(-148, "Character data not allowed")
                return
            # Quirk: instrument re-orders to canonical order; argument set is
            # honored, argument order is ignored.
            self.state["form_elem"] = [e for e in _CANONICAL_ELEMS if e in requested]
            return

        # Auto-ohms quirk: in RES function with mode AUTO, several source/
        # compliance commands are rejected with error 825.
        _AUTO_OHMS_REJECTED = (":SOUR:CURR:RANG", ":SOUR:CURR", ":SENS:VOLT:PROT")
        if (self._sense_func_short() == "RES"
                and self.state["sens_res_mode"] == "AUTO"):
            for blocked in _AUTO_OHMS_REJECTED:
                if upper.startswith(blocked + " ") or upper == blocked:
                    self._queue_error(825, "Invalid with auto-ohms on")
                    return

        # Settable property table (the bulk of writes)
        for prefix, (key, kind) in _SETTABLE.items():
            if upper.startswith(prefix.upper() + " "):
                arg = cmd[len(prefix):].strip()
                self._apply_settable(key, kind, arg)
                return
            if upper == prefix.upper():
                self._queue_error(-109, "Missing parameter")
                return

        # :INIT:CONT — documented-not-supported on 2400 series
        if upper.startswith(":INIT:CONT"):
            self._queue_error(-113, "Undefined header")
            return

        # Unknown command
        self._queue_error(-113, "Undefined header")

    def _apply_settable(self, key: str, kind: str, arg: str) -> None:
        try:
            if kind == "bool":
                self.state[key] = _parse_bool(arg)
            elif kind == "float":
                self.state[key] = _parse_float(arg)
            elif kind == "int":
                self.state[key] = int(_parse_float(arg))
            elif kind == "azer":
                up = arg.upper()
                if up not in ("ON", "OFF", "ONCE", "0", "1"):
                    self._queue_error(-148, "Character data not allowed")
                    return
                self.state[key] = "ON" if up in ("ON", "1") else ("OFF" if up == "0" else up)
            elif kind == "tcon":
                up = arg.upper()
                if up not in ("REP", "REPEAT", "MOV", "MOVING"):
                    self._queue_error(-148, "Character data not allowed")
                    return
                self.state[key] = "REP" if up.startswith("REP") else "MOV"
            elif kind == "mode":
                up = arg.upper()
                if up not in ("FIX", "FIXED", "SWE", "SWEEP", "LIST"):
                    self._queue_error(-148, "Character data not allowed")
                    return
                self.state[key] = "FIX" if up.startswith("FIX") else ("SWE" if up.startswith("SWE") else "LIST")
            elif kind == "smod":
                up = arg.upper()
                if up not in ("NORM", "NORMAL", "ZERO", "HIMP", "GUAR"):
                    self._queue_error(-148, "Character data not allowed")
                    return
                self.state[key] = up[:4]
            elif kind == "str":
                self.state[key] = arg.strip().upper()
            else:
                self.state[key] = arg
        except (ValueError, TypeError):
            self._queue_error(-220, "Parameter error")

    # ----------------------------------------------------------- query side

    def _dispatch_query(self, cmd: str) -> str:
        upper = cmd.upper()

        if upper == "*IDN?":
            return self._idn

        if upper == "*OPC?":
            return "1"

        if upper == ":SYST:ERR?":
            if not self._error_queue:
                return '0,"No error"'
            code, msg = self._error_queue.popleft()
            # Real instrument formats positive codes without sign, negative
            # with leading "-". Python's str(int) already does that.
            return f'{code},"{msg}"'

        if upper == ":READ?":
            return self._compute_read()

        if upper == ":FORM:ELEM?":
            return ",".join(self.state["form_elem"])

        if upper == ":STAT:MEAS:COND?":
            # Best-effort — measurement event register isn't fully modeled,
            # we just report 0 unless the last READ saw compliance.
            return "0"

        # Generic settable-property read
        if upper in _QUERY_TO_KEY:
            key, kind = _QUERY_TO_KEY[upper]
            return self._format_state(self.state[key], kind)

        self._queue_error(-113, "Undefined header")
        return ""

    def _format_state(self, value, kind: str) -> str:
        if kind == "bool_str":
            return _format_bool(bool(value))
        if kind == "int_str":
            return str(int(value))
        if kind == "raw":
            return str(value)
        if kind == "azer_str":
            return _format_bool(value == "ON")
        if kind == "k_float":
            return _format_keithley(float(value))
        if kind == "nplc":
            return f"{float(value):.2f}"
        if kind == "rang":
            return f"{float(value):.2f}"
        if kind == "del":
            return f"{float(value):.5f}"
        return str(value)

    # ------------------------------------------------------------- READ? body

    def _sense_func_short(self) -> str:
        """Return canonical short form of the active sense function: VOLT/CURR/RES."""
        f = self.state["sens_func"].strip().strip("'\"").upper()
        if f.startswith("VOLT"):
            return "VOLT"
        if f.startswith("CURR"):
            return "CURR"
        if f.startswith("RES"):
            return "RES"
        return f

    def _compute_one_point(self, source_value: float) -> tuple[float, float, float, bool]:
        """Compute one (V, I, R, in_compliance) tuple for the current source value.

        Hardware behavior verified against captured traces:
            - Source-V mode: VOLT element = source setpoint (echoed back), even
              in compliance. CURR element = measured/clamped current.
            - Source-I mode: CURR element = source setpoint (echoed back), even
              in compliance. VOLT element = measured/clamped voltage.
            - Compliance is signaled via STAT bit 3, NOT via clamping the echoed
              source value.
        """
        in_compliance = False
        r = self.dut_resistance
        if self.state["sour_func"] == "VOLT":
            requested_v = source_value
            ideal_i = (requested_v - self.dut_voltage_offset) / r if r != 0 else float("inf")
            i_limit = abs(self.state["sens_curr_prot"])
            if abs(ideal_i) > i_limit:
                in_compliance = True
                measured_i = math.copysign(i_limit, ideal_i)
            else:
                measured_i = ideal_i
            measured_i = self._apply_noise(measured_i)
            actual_v = requested_v             # echoed source setpoint
            actual_i = measured_i              # measured / clamped (+ noise)
        else:  # source CURR
            requested_i = source_value
            ideal_v = requested_i * r + self.dut_voltage_offset
            v_limit = abs(self.state["sens_volt_prot"])
            if abs(ideal_v) > v_limit:
                in_compliance = True
                measured_v = math.copysign(v_limit, ideal_v)
            else:
                measured_v = ideal_v
            measured_v = self._apply_noise(measured_v)
            actual_v = measured_v              # measured / clamped (+ noise)
            actual_i = requested_i             # echoed source setpoint
        actual_r = (actual_v / actual_i) if actual_i != 0 else float("nan")
        return actual_v, actual_i, actual_r, in_compliance

    def _apply_noise(self, value: float) -> float:
        """Add Gaussian noise (relative-std-dev = self._noise_rsd) to value.

        No-op when noise_rsd is 0 — preserves bit-exact behavior for the
        replay tests that diff against captured SCPI traces.
        """
        if self._noise_rsd <= 0.0 or not math.isfinite(value):
            return value
        sigma = abs(value) * self._noise_rsd
        return value + self._noise_rng.gauss(0.0, sigma)

    def _compute_read(self) -> str:
        sour_func = self.state["sour_func"]
        in_sweep = (
            (sour_func == "VOLT" and self.state["sour_volt_mode"] == "SWE") or
            (sour_func == "CURR" and self.state["sour_curr_mode"] == "SWE")
        )

        if in_sweep:
            return self._compute_sweep_read()

        # Single point
        source_value = (
            self.state["sour_volt"] if sour_func == "VOLT" else self.state["sour_curr"]
        )
        v, i, r, comp = self._compute_one_point(source_value)
        baseline = _STAT_BASELINE.get(self._mode_tag(), _STAT_BASELINE["default"])
        stat = baseline | (_STAT_BIT_COMPLIANCE if comp else 0)
        return self._format_elements(v, i, r, stat)

    def _compute_sweep_read(self) -> str:
        sour_func = self.state["sour_func"]
        if sour_func == "VOLT":
            start = self.state["sour_volt_start"]
            stop = self.state["sour_volt_stop"]
            step = self.state["sour_volt_step"]
        else:
            start = self.state["sour_curr_start"]
            stop = self.state["sour_curr_stop"]
            step = self.state["sour_curr_step"]

        n_points = self.state["trig_coun"]
        if n_points <= 0:
            n_points = 1

        # Generate evenly spaced points from start to stop
        if n_points == 1:
            sweep_values = [start]
        else:
            # Use TRIG:COUN as authoritative point count (matches real instrument)
            sweep_values = [start + (stop - start) * k / (n_points - 1) for k in range(n_points)]
            # If a step was specified, prefer it for the spacing
            if step != 0 and (stop - start) != 0:
                direction = 1 if stop >= start else -1
                sweep_values = [start + direction * abs(step) * k for k in range(n_points)]

        baseline = _STAT_BASELINE["sweep"]
        chunks = []
        for sv in sweep_values:
            v, i, r, comp = self._compute_one_point(sv)
            stat = baseline | (_STAT_BIT_COMPLIANCE if comp else 0)
            chunks.append(self._format_elements(v, i, r, stat))
        return ",".join(chunks)

    def _mode_tag(self) -> str:
        sf = self._sense_func_short()
        if sf == "RES":
            return "resistance"
        if self.state["sour_func"] == "VOLT":
            return "source_v"
        return "source_i"

    def _format_elements(self, v: float, i: float, r: float, stat: int) -> str:
        elements = self.state["form_elem"]
        out = []
        for elem in elements:
            if elem == "VOLT":
                out.append(_format_keithley(v))
            elif elem == "CURR":
                out.append(_format_keithley(i))
            elif elem == "RES":
                out.append(_format_keithley(r))
            elif elem == "TIME":
                out.append(_format_keithley(0.0))  # not modeled
            elif elem == "STAT":
                out.append(_format_keithley(float(stat)))
        return ",".join(out)

    # ----------------------------------------------------------- error queue

    def _queue_error(self, code: int, message: str) -> None:
        # Real instrument has a 10-deep error queue with overflow code -350
        if len(self._error_queue) >= 10:
            return
        self._error_queue.append((code, message))


class FakeSerialSensor:
    """A drop-in for a streaming ASCII serial sensor (auxiliary-sensor seam).

    Duck-types the subset of ``pyvisa.resources.MessageBasedResource`` that
    :class:`resistamet_gui.sensors.SerialLineSensor` uses: settable
    ``timeout``/``read_termination``/``write_termination``, ``read()`` returning
    one ``DATA,<tip>,<cold>,<fault>,<status>`` line, plus ``flush``/``close``.

    Streams a temperature centered on ``sim_temp_c`` (the K-type tip) with the
    cold-junction ~1.5 °C above it, so ``--simulate`` exercises the co-logging
    path end to end with no hardware. Noise is seeded for per-process
    determinism, matching :class:`FakeKeithley`.
    """

    def __init__(self, sim_temp_c: float = 25.0, noise_c: float = 0.05):
        self.sim_temp_c = float(sim_temp_c)
        self._noise_c = float(noise_c)
        self._rng = random.Random(0x7EA)
        # PyVISA-compatible knobs
        self.timeout = 3000
        self.read_termination = "\n"
        self.write_termination = "\n"
        # Handy in tests
        self.read_count = 0

    def _line(self) -> str:
        tip = self.sim_temp_c + self._rng.gauss(0.0, self._noise_c)
        cold = self.sim_temp_c + 1.5 + self._rng.gauss(0.0, self._noise_c)
        return f"DATA,{tip:.3f},{cold:.3f},0,0\r\n"

    def read(self) -> str:
        self.read_count += 1
        return self._line()

    def flush(self, *_args, **_kwargs) -> None:
        pass

    def write(self, *_args, **_kwargs):
        # Read-only sensor; accept and ignore writes for duck-type safety.
        return None

    def query(self, _cmd: str) -> str:
        # Not used on the read-only path, but harmless to support.
        return self._line()

    def close(self) -> None:
        pass


# ============================================================================
# Resource manager shim — lets us monkeypatch pyvisa with one line.
# ============================================================================

class FakeResourceManager:
    """Minimal stand-in for ``pyvisa.ResourceManager``.

    Reports a single GPIB resource (configurable via ``gpib_address``) and
    returns a fresh :class:`FakeKeithley` for every ``open_resource`` call.
    The returned object's ``dut_resistance``/``dut_voltage_offset`` can be
    tweaked by tests after opening.
    """

    def __init__(
        self,
        gpib_address: str = "GPIB0::24::INSTR",
        dut_resistance_ohms: float = 100.0,
        dut_voltage_offset: float = 0.0,
        idn: Optional[str] = None,
        model: Optional[str] = None,
        noise_rsd: float = 0.0,
        aux_address: str = "ASRL6::INSTR",
        sim_temp_c: float = 25.0,
    ):
        if idn is not None and model is not None:
            raise ValueError("pass either idn= or model=, not both")
        self._addr = gpib_address
        self._dut_r = dut_resistance_ohms
        self._dut_v = dut_voltage_offset
        self._noise_rsd = float(noise_rsd)
        self._idn = idn if idn is not None else (
            _idn_for_model(model) if model is not None else DEFAULT_IDN
        )
        # Auxiliary streaming sensor (e.g. an Arduino thermocouple on ASRL).
        # Exposed alongside the GPIB SMU so the pluggable-sensor path is
        # exercisable under --simulate with no hardware.
        self._aux_addr = aux_address
        self._sim_temp_c = float(sim_temp_c)
        self.opened: list[FakeKeithley] = []

        # Pre-injection: applied to the next opened FakeKeithley. Used by
        # tests that want the very first :READ? on a fresh device to fail —
        # avoids racing the cross-thread signal that would set it later.
        self._pre_inject_fail_n = 0
        self._pre_inject_fail_skip = 0
        self._pre_inject_exception: Optional[BaseException] = None

    def list_resources(self) -> tuple[str, ...]:
        return (self._addr, self._aux_addr)

    def open_resource(self, resource_name: str, **_kwargs):
        # Auxiliary serial sensor branch — checked before the GPIB SMU so a
        # streaming-sensor open under --simulate gets a FakeSerialSensor.
        if resource_name == self._aux_addr:
            return FakeSerialSensor(sim_temp_c=self._sim_temp_c)
        if resource_name != self._addr:
            raise pyvisa.errors.VisaIOError(-1073807343)  # VI_ERROR_RSRC_NFOUND
        dev = FakeKeithley(
            dut_resistance_ohms=self._dut_r,
            dut_voltage_offset=self._dut_v,
            idn=self._idn,
            noise_rsd=self._noise_rsd,
        )
        if self._pre_inject_fail_n > 0:
            dev._pre_inject_skip = self._pre_inject_fail_skip
            dev.fail_next_query(
                n=self._pre_inject_fail_n,
                exception=self._pre_inject_exception,
            )
            # Reset so subsequent opens don't inherit the injection
            self._pre_inject_fail_n = 0
            self._pre_inject_fail_skip = 0
        self.opened.append(dev)
        return dev

    def close(self) -> None:
        pass

    def fail_next_open(self, n: int, *, skip_first: int = 0,
                        exception: Optional[BaseException] = None) -> None:
        """Schedule the next opened FakeKeithley to fail its first ``n``
        queries (after ``skip_first`` successful ones).

        Used to avoid a cross-thread race when injecting failures into a
        worker that hasn't connected yet.
        """
        self._pre_inject_fail_n = n
        self._pre_inject_fail_skip = skip_first
        self._pre_inject_exception = exception

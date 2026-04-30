#!/usr/bin/env python3
"""Community SCPI-trace capture for any Keithley 2400-series instrument.

This is a friendly, self-contained capture tool — drop it on any machine
with PyVISA + a connected Keithley sourcemeter and it produces a set of
JSON SCPI traces you can submit back to ResistaMet-GUI to expand
cross-model fidelity coverage.

Usage:
    pip install pyvisa pyvisa-py
    python community_capture.py             # auto-detect + interactive
    python community_capture.py --address GPIB0::24::INSTR --dut 100

What it does:
    1. Lists VISA resources, picks (or prompts for) a Keithley 2400-family
       instrument.
    2. Reads *IDN?, identifies model + firmware.
    3. Asks which reference DUT is wired (100Ω, 10kΩ, or 1MΩ recommended;
       4-wire Kelvin connection).
    4. Runs a polarity sanity check — refuses to capture if the magnitude
       is wrong.
    5. Runs an appropriate scenario set for that DUT.
    6. Writes JSON traces to ./scpi_traces_<model>_<serial>/.
    7. Prints instructions for opening an issue with the traces attached.

The captured traces will be reviewed before merge. They become part of the
project's cross-model validation corpus, helping ensure the simulator
faithfully reproduces every supported instrument.

Repository: https://github.com/PEEKPerformer/ResistaMet-GUI
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

try:
    import pyvisa
except ImportError:
    sys.exit(
        "pyvisa is required. Install with:\n"
        "    pip install pyvisa pyvisa-py\n"
        "(also install NI-VISA or a backend if you don't already have one)"
    )


# --------------------------------------------------------- model spec table
# Mirrors resistamet_gui.instrument._MODELS — kept in sync manually so this
# script remains self-contained and runnable without the project installed.

@dataclass(frozen=True)
class ModelSpec:
    model: str
    max_source_v: float
    max_source_i: float
    max_power_w: float
    family: str


_MODELS: dict[str, ModelSpec] = {
    "2400": ModelSpec("2400", 200.0, 1.05, 22.0, "2400"),
    "2401": ModelSpec("2401", 20.0,  1.05, 22.0, "2400"),
    "2410": ModelSpec("2410", 1100.0, 1.05, 22.0, "2400"),
    "2420": ModelSpec("2420", 60.0,  3.05, 22.0, "2400"),
    "2425": ModelSpec("2425", 100.0, 3.05, 22.0, "2400"),
    "2430": ModelSpec("2430", 100.0, 3.05, 22.0, "2400"),
    "2440": ModelSpec("2440", 40.0,  5.05, 22.0, "2400"),
    "2450": ModelSpec("2450", 200.0, 1.05, 22.0, "2450"),
}


def parse_model(idn: str) -> tuple[Optional[ModelSpec], Optional[str]]:
    """Return (spec, serial) parsed from a Keithley *IDN? response.

    Either component may be None if the IDN doesn't match the expected
    Keithley shape ``KEITHLEY INSTRUMENTS INC.,MODEL <NNNN>,<serial>,...``.
    """
    if not idn:
        return None, None
    m = re.search(r"MODEL\s+(\d{4})\s*,\s*(\S+)", idn, re.IGNORECASE)
    if not m:
        return None, None
    return _MODELS.get(m.group(1)), m.group(2)


# ----------------------------------------------------------- trace format

@dataclass
class TraceEvent:
    op: str
    cmd: str
    response: Optional[str] = None


@dataclass
class Trace:
    name: str
    description: str
    instrument_idn: str
    model: Optional[str]
    serial: Optional[str]
    captured_at: str
    dut_resistance_ohms: float
    events: list = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


class _Tracer:
    def __init__(self, dev, trace: Trace):
        self._dev = dev
        self.trace = trace

    def write(self, cmd):
        self.trace.events.append(TraceEvent(op="write", cmd=cmd))
        return self._dev.write(cmd)

    def query(self, cmd):
        r = self._dev.query(cmd).strip()
        self.trace.events.append(TraceEvent(op="query", cmd=cmd, response=r))
        return r


# ---------------------------------------------------------- scenario configs
# Each DUT gets a small set of representative scenarios — chosen to exercise
# different source-current and source-voltage ranges without ever pushing
# the instrument or DUT outside safe operating territory.

_DUT_SCENARIOS = {
    100.0: dict(
        test_current=1e-3,
        source_v_value=0.1,
        source_v_range=0.2,
        source_i_value=1e-3,
        source_i_range=1e-3,
        compliance_in_v=0.5,
        compliance_in_v_range=1.0,
        compliance_in_i_limit=1e-3,
        compliance_in_i_range=1e-3,
        compliance_out_i_limit=100e-3,
        compliance_out_i_range=100e-3,
        sweep_stop=0.5,
        sweep_step=0.125,
    ),
    10_000.0: dict(
        test_current=100e-6,
        source_v_value=1.0,
        source_v_range=2.0,
        source_i_value=100e-6,
        source_i_range=100e-6,
        compliance_in_v=5.0,
        compliance_in_v_range=6.0,
        compliance_in_i_limit=100e-6,
        compliance_in_i_range=100e-6,
        compliance_out_i_limit=1e-3,
        compliance_out_i_range=1e-3,
        sweep_stop=1.0,
        sweep_step=0.25,
    ),
    1_000_000.0: dict(
        test_current=1e-6,
        source_v_value=1.0,
        source_v_range=2.0,
        source_i_value=1e-6,
        source_i_range=1e-6,
        compliance_in_v=5.0,
        compliance_in_v_range=6.0,
        compliance_in_i_limit=1e-6,
        compliance_in_i_range=1e-6,
        compliance_out_i_limit=10e-6,
        compliance_out_i_range=10e-6,
        sweep_stop=1.0,
        sweep_step=0.25,
    ),
}


# ----------------------------------------------------------- VISA helpers

def _drain(dev) -> None:
    while True:
        if dev.query(":SYST:ERR?").strip().startswith(("0,", "+0,")):
            return


def _new_trace(name: str, desc: str, *, idn: str, spec: Optional[ModelSpec],
                serial: Optional[str], dut_ohms: float) -> Trace:
    return Trace(
        name=name, description=desc, instrument_idn=idn,
        model=spec.model if spec else None,
        serial=serial,
        captured_at=_dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        dut_resistance_ohms=dut_ohms,
    )


def _reset(t: _Tracer) -> None:
    t.write("*RST")
    time.sleep(0.5)
    t.write("*CLS")


# ----------------------------------------------------------- scenarios

def scn_resistance(dev, ctx, cfg) -> Trace:
    tr = _new_trace(
        f"resistance_4wire_{_si(cfg['test_current'])}_into_{_si_ohm(ctx['dut'])}",
        f"Resistance mode, 4-wire, {_si(cfg['test_current'])}A test current.",
        **ctx,
    )
    t = _Tracer(dev, tr); _drain(dev); _reset(t)
    t.write(":SYST:RSEN ON")
    t.write(":SENS:FUNC:CONC OFF")
    t.write(":SENS:FUNC 'RES'")
    t.write(":SENS:RES:MODE MAN")
    t.write(":SOUR:FUNC CURR")
    t.write(f":SOUR:CURR:RANG {cfg['test_current']}")
    t.write(f":SOUR:CURR {cfg['test_current']}")
    t.write(":SENS:VOLT:PROT 5")
    t.write(":SENS:RES:NPLC 1")
    t.write(":SENS:RES:MODE AUTO")
    t.write(":FORM:ELEM RES,STAT")
    t.write(":OUTP ON"); time.sleep(0.5)
    for _ in range(3):
        t.query(":READ?"); time.sleep(0.1)
    t.write(":OUTP OFF")
    return tr


def scn_source_v(dev, ctx, cfg) -> Trace:
    tr = _new_trace(
        f"source_v_{_si(cfg['source_v_value'])}V_into_{_si_ohm(ctx['dut'])}",
        f"Voltage source mode, {cfg['source_v_value']}V into the DUT.",
        **ctx,
    )
    t = _Tracer(dev, tr); _drain(dev); _reset(t)
    t.write(":SYST:RSEN ON")
    t.write(":SENS:FUNC:CONC OFF")
    t.write(":SENS:FUNC 'CURR:DC'")
    t.write(":SOUR:FUNC VOLT")
    t.write(f":SOUR:VOLT:RANG {cfg['source_v_range']}")
    t.write(f":SOUR:VOLT {cfg['source_v_value']}")
    t.write(f":SENS:CURR:PROT {cfg['compliance_out_i_limit']}")
    t.write(":SENS:CURR:RANG:AUTO ON")
    t.write(":SENS:CURR:NPLC 1")
    t.write(":FORM:ELEM VOLT,CURR,STAT")
    t.write(":OUTP ON"); time.sleep(0.5)
    for _ in range(3):
        t.query(":READ?"); time.sleep(0.1)
    t.write(":OUTP OFF")
    return tr


def scn_source_i(dev, ctx, cfg) -> Trace:
    tr = _new_trace(
        f"source_i_{_si(cfg['source_i_value'])}A_into_{_si_ohm(ctx['dut'])}",
        f"Current source mode, {_si(cfg['source_i_value'])}A into the DUT.",
        **ctx,
    )
    t = _Tracer(dev, tr); _drain(dev); _reset(t)
    t.write(":SYST:RSEN ON")
    t.write(":SENS:FUNC:CONC OFF")
    t.write(":SENS:FUNC 'VOLT:DC'")
    t.write(":SOUR:FUNC CURR")
    t.write(f":SOUR:CURR:RANG {cfg['source_i_range']}")
    t.write(f":SOUR:CURR {cfg['source_i_value']}")
    t.write(":SENS:VOLT:PROT 5")
    t.write(":SENS:VOLT:RANG:AUTO ON")
    t.write(":SENS:VOLT:NPLC 1")
    t.write(":FORM:ELEM VOLT,CURR,STAT")
    t.write(":OUTP ON"); time.sleep(0.5)
    for _ in range(3):
        t.query(":READ?"); time.sleep(0.1)
    t.write(":OUTP OFF")
    return tr


def scn_compliance_in(dev, ctx, cfg) -> Trace:
    tr = _new_trace(
        f"compliance_v_in_compliance_{_si_ohm(ctx['dut'])}",
        f"Compliance hit: {cfg['compliance_in_v']}V source with "
        f"{_si(cfg['compliance_in_i_limit'])}A current limit. STAT bit 3 expected set.",
        **ctx,
    )
    t = _Tracer(dev, tr); _drain(dev); _reset(t)
    t.write(":SYST:RSEN ON")
    t.write(":SENS:FUNC:CONC OFF")
    t.write(":SENS:FUNC 'CURR:DC'")
    t.write(":SOUR:FUNC VOLT")
    t.write(f":SOUR:VOLT:RANG {cfg['compliance_in_v_range']}")
    t.write(f":SOUR:VOLT {cfg['compliance_in_v']}")
    t.write(f":SENS:CURR:PROT {cfg['compliance_in_i_limit']}")
    t.write(f":SENS:CURR:RANG {cfg['compliance_in_i_range']}")
    t.write(":SENS:CURR:NPLC 1")
    t.write(":FORM:ELEM VOLT,CURR,STAT")
    t.write(":OUTP ON"); time.sleep(0.5)
    t.query(":READ?")
    t.write(":OUTP OFF")
    return tr


def scn_quirk_auto_ohms(dev, ctx, cfg) -> Trace:
    tr = _new_trace(
        "quirk_auto_ohms_rejects_source",
        "Documented 2400-family quirk: after :SENS:FUNC 'RES', auto-ohms is ON "
        "and rejects :SOUR:CURR* / :SENS:VOLT:PROT with error 825.",
        **ctx,
    )
    t = _Tracer(dev, tr); _drain(dev); _reset(t)
    t.write(":SENS:FUNC 'RES'")
    t.query(":SYST:ERR?")
    t.write(":SOUR:CURR 1e-3")
    t.query(":SYST:ERR?")
    t.write(":SENS:RES:MODE MAN")
    t.query(":SYST:ERR?")
    t.write(":SOUR:CURR 1e-3")
    t.query(":SYST:ERR?")
    return tr


def scn_quirk_form_elem(dev, ctx, cfg) -> Trace:
    tr = _new_trace(
        "quirk_form_elem_canonical_order",
        "Documented quirk: FORM:ELEM argument order is silently re-ordered "
        "to canonical VOLT,CURR,RES,TIME,STAT.",
        **ctx,
    )
    t = _Tracer(dev, tr); _drain(dev); _reset(t)
    t.write(":FORM:ELEM CURR,VOLT,STAT")
    t.query(":FORM:ELEM?")
    t.write(":FORM:ELEM STAT,RES,VOLT")
    t.query(":FORM:ELEM?")
    return tr


def scn_baseline_reset(dev, ctx, cfg) -> Trace:
    tr = _new_trace(
        "baseline_reset_state",
        "Default settings after *RST — used to verify model-level defaults match.",
        **ctx,
    )
    t = _Tracer(dev, tr); _drain(dev); _reset(t)
    for q in (":SYST:LFR?", ":SYST:RSEN?", ":SENS:FUNC?", ":SENS:FUNC:CONC?",
              ":SENS:RES:MODE?", ":SOUR:FUNC?", ":SOUR:VOLT?", ":SOUR:CURR?",
              ":TRIG:COUN?", ":FORM:ELEM?", ":OUTP?", ":OUTP:SMOD?"):
        t.query(q)
    return tr


SCENARIOS = [
    scn_baseline_reset,
    scn_resistance, scn_source_v, scn_source_i, scn_compliance_in,
    scn_quirk_auto_ohms, scn_quirk_form_elem,
]


def _si(value: float) -> str:
    """Format a current/voltage in lab-friendly SI-prefixed shorthand."""
    if value == 0:
        return "0"
    abs_v = abs(value)
    for prefix, factor in (("M", 1e6), ("k", 1e3), ("", 1.0),
                            ("m", 1e-3), ("u", 1e-6), ("n", 1e-9)):
        if abs_v >= factor:
            return f"{value/factor:g}{prefix}"
    return f"{value:g}"


def _si_ohm(r: float) -> str:
    if r >= 1e6:
        return f"{r/1e6:g}Mohm"
    if r >= 1e3:
        return f"{r/1e3:g}kohm"
    return f"{r:g}ohm"


# ----------------------------------------------------------- main flow

def _select_resource(rm) -> str:
    resources = rm.list_resources()
    keithleys = []
    for r in resources:
        if "GPIB" not in r and "USB" not in r:
            continue
        try:
            d = rm.open_resource(r)
            d.timeout = 2000
            try:
                d.read_termination = "\n"; d.write_termination = "\n"
            except Exception:
                pass
            try:
                idn = d.query("*IDN?").strip()
                if "KEITHLEY" in idn.upper():
                    keithleys.append((r, idn))
            except Exception:
                pass
            d.close()
        except Exception:
            pass
    if not keithleys:
        sys.exit(f"No Keithley instruments found. Available resources: {resources}")
    if len(keithleys) == 1:
        print(f"Using {keithleys[0][0]}: {keithleys[0][1]}")
        return keithleys[0][0]
    print("Multiple Keithley instruments found — pick one:")
    for i, (addr, idn) in enumerate(keithleys):
        print(f"  [{i}] {addr}  {idn}")
    choice = input("Enter index: ").strip()
    return keithleys[int(choice)][0]


def _polarity_check(dev, dut_ohms: float) -> bool:
    """Quick 4-wire R read at the DUT's nominal test current.

    Refuses to proceed if magnitude is wrong (suggests open / wrong DUT)
    or sign is inverted (suggests miswired Kelvin connection).
    """
    cfg = _DUT_SCENARIOS[dut_ohms]
    test_i = cfg["test_current"]
    print(f"\nRunning polarity check at {_si(test_i)}A...")
    _drain(dev)
    dev.write("*RST"); time.sleep(0.5); dev.write("*CLS")
    dev.write(":SYST:RSEN ON")
    dev.write(":SENS:FUNC:CONC OFF")
    dev.write(":SENS:FUNC 'RES'")
    dev.write(":SENS:RES:MODE MAN")
    dev.write(":SOUR:FUNC CURR")
    dev.write(f":SOUR:CURR:RANG {test_i}")
    dev.write(f":SOUR:CURR {test_i}")
    dev.write(":SENS:VOLT:PROT 5")
    dev.write(":SENS:RES:NPLC 1")
    dev.write(":SENS:RES:MODE AUTO")
    dev.write(":FORM:ELEM RES,STAT")
    dev.write(":OUTP ON"); time.sleep(0.5)
    readings = []
    for _ in range(3):
        try:
            r = float(dev.query(":READ?").split(",")[0])
        except Exception:
            r = float("nan")
        readings.append(r); time.sleep(0.1)
    dev.write(":OUTP OFF")
    avg = sum(readings) / len(readings)
    print(f"  reading: {avg:.2f} ohm  (expected ~{dut_ohms:g})")
    if avg < 0:
        print("  POLARITY INVERTED — swap one pair of FORCE leads at the DUT and rerun.")
        return False
    expected_low = dut_ohms * 0.7
    expected_high = dut_ohms * 1.3
    if not (expected_low < avg < expected_high):
        print(f"  MAGNITUDE OUTSIDE ±30% — confirm the DUT is {dut_ohms:g}Ω and Kelvin contacts are clean.")
        return False
    print("  OK — proceeding to capture.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--address", help="VISA resource (e.g. GPIB0::24::INSTR). Auto-detect if omitted.")
    parser.add_argument("--dut", type=float, choices=sorted(_DUT_SCENARIOS),
                         help="DUT resistance in ohms (100, 10000, or 1000000). Prompt if omitted.")
    parser.add_argument("--output-dir", type=Path, default=None,
                         help="Where to write JSON traces. Default: ./scpi_traces_<model>_<serial>/")
    parser.add_argument("--skip-polarity-check", action="store_true",
                         help="Don't refuse if the polarity check looks wrong (advanced).")
    args = parser.parse_args()

    rm = pyvisa.ResourceManager()
    addr = args.address or _select_resource(rm)
    dev = rm.open_resource(addr)
    dev.timeout = 30000
    try:
        dev.read_termination = "\n"; dev.write_termination = "\n"
    except Exception:
        pass

    idn = dev.query("*IDN?").strip()
    spec, serial = parse_model(idn)
    print(f"\nIDN: {idn}")
    if spec is None:
        print(f"  WARNING: model not in known table — will still capture, but please")
        print(f"  include the IDN in your submission so we can add the spec.")
    else:
        print(f"  Model: {spec.model}  family: {spec.family}  "
              f"max V/I: {spec.max_source_v:g}V / {spec.max_source_i:g}A")

    if args.dut is None:
        print("\nWhich reference DUT is wired (4-wire Kelvin)?")
        print("  [1] 100 Ω  (mA / 0.1 V regime)")
        print("  [2] 10 kΩ  (100 µA / 1 V regime)")
        print("  [3] 1 MΩ   (µA / 1 V regime)")
        choice = input("Choice [1-3]: ").strip()
        dut_ohms = {"1": 100.0, "2": 10_000.0, "3": 1_000_000.0}.get(choice)
        if dut_ohms is None:
            sys.exit("Invalid choice.")
    else:
        dut_ohms = args.dut

    if not _polarity_check(dev, dut_ohms) and not args.skip_polarity_check:
        sys.exit(1)

    cfg = _DUT_SCENARIOS[dut_ohms]
    ctx = dict(idn=idn, spec=spec, serial=serial, dut=dut_ohms)

    out_dir = args.output_dir or Path(
        f"scpi_traces_{spec.model if spec else 'unknown'}_{serial or 'unknown'}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nWriting traces to {out_dir}/\n")

    n_ok = n_fail = 0
    try:
        for scn in SCENARIOS:
            name = scn.__name__.removeprefix("scn_")
            print(f"  capturing {name}...", end=" ", flush=True)
            try:
                trace = scn(dev, ctx, cfg)
                target = out_dir / f"{trace.name}.json"
                target.write_text(trace.to_json() + "\n", encoding="utf-8")
                print(f"ok ({len(trace.events)} events)")
                n_ok += 1
            except Exception as e:
                print(f"FAIL: {type(e).__name__}: {e}")
                n_fail += 1
                try: dev.write(":OUTP OFF"); dev.write("*CLS")
                except Exception: pass
    finally:
        try: dev.write(":OUTP OFF")
        except Exception: pass
        dev.close()
        rm.close()

    print(f"\nDone. {n_ok} ok / {n_fail} failed")
    if n_fail == 0:
        print(f"\nTo submit your traces:")
        print(f"  1. Zip the directory: {out_dir}")
        print(f"  2. Open an issue at:")
        print(f"     https://github.com/PEEKPerformer/ResistaMet-GUI/issues/new"
              f"?template=keithley_compatibility.yml")
        print(f"  3. Attach the zip; fill in IDN and DUT info.")
        print(f"\nThank you for helping validate ResistaMet across the 2400 family!")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

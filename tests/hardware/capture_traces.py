"""Capture golden SCPI traces from a real Keithley 2400-series sourcemeter.

Run from a host with PyVISA + a configured GPIB backend, with the sourcemeter
wired to the calibration DUT (default: 100Ω resistor, 4-wire Kelvin).

    GPIB0::24::INSTR is the default address for our lab. Override via
    KEITHLEY_GPIB env var.

Each scenario produces one trace file under tests/fixtures/scpi_traces/.
The traces are the ground truth that the FakeKeithley simulator must match.

This script is intentionally not a pytest test — capturing requires hardware
and writes to the working tree. The pytest harness re-runs captures via the
test_recapture_traces module and diffs against checked-in golden files.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import time
from pathlib import Path
from typing import Callable

import pyvisa

# Allow running this file directly from the Windows host where the package
# layout may not be installed; fall back to direct import via sys.path.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
from fakes.scpi_tracer import ScpiTracer  # noqa: E402
from fakes.trace_format import Trace, TraceEvent, trace_dir  # noqa: E402


GPIB_ADDR = os.environ.get("KEITHLEY_GPIB", "GPIB0::24::INSTR")
DUT_OHMS = float(os.environ.get("KEITHLEY_DUT_OHMS", "100.0"))


# --- helpers ----------------------------------------------------------------

def _drain_errors(tracer: ScpiTracer) -> None:
    """Drain the instrument's error queue without recording each query.

    Errors from prior aborted runs would otherwise pollute scenario traces.
    """
    while True:
        resp = tracer._dev.query(":SYST:ERR?").strip()
        if resp.startswith(("0,", "+0,")):
            return


def _hard_reset(tracer: ScpiTracer) -> None:
    tracer.write("*RST")
    time.sleep(0.5)
    tracer.write("*CLS")


def _new_trace(name: str, description: str, idn: str) -> Trace:
    return Trace(
        name=name,
        description=description,
        instrument_idn=idn,
        captured_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        dut_resistance_ohms=DUT_OHMS,
    )


# --- scenarios --------------------------------------------------------------
#
# Each scenario is a function (device, idn) -> Trace. Inside, the function
# wraps the device with a ScpiTracer, runs SCPI, returns the populated trace.
# The capture loop persists each trace to disk.

def scenario_baseline_reset_state(dev, idn: str) -> Trace:
    trace = _new_trace(
        "baseline_reset_state",
        "*RST then query a battery of settings to capture the documented default state.",
        idn,
    )
    tracer = ScpiTracer(dev, trace)
    _drain_errors(tracer)
    _hard_reset(tracer)
    queries = [
        ":SYST:LFR?", ":SYST:RSEN?", ":SYST:AZER:STAT?",
        ":SENS:FUNC?", ":SENS:FUNC:CONC?",
        ":SENS:VOLT:NPLC?", ":SENS:CURR:NPLC?", ":SENS:RES:NPLC?",
        ":SENS:RES:MODE?", ":SENS:RES:OCOM?",
        ":SENS:VOLT:PROT?", ":SENS:CURR:PROT?",
        ":SENS:VOLT:RANG?", ":SENS:CURR:RANG?",
        ":SENS:VOLT:RANG:AUTO?", ":SENS:CURR:RANG:AUTO?",
        ":SENS:AVER?", ":SENS:AVER:TCON?", ":SENS:AVER:COUN?",
        ":SOUR:FUNC?", ":SOUR:VOLT?", ":SOUR:CURR?",
        ":SOUR:VOLT:RANG?", ":SOUR:CURR:RANG?",
        ":SOUR:VOLT:MODE?", ":SOUR:CURR:MODE?",
        ":SOUR:DEL?", ":SOUR:DEL:AUTO?",
        ":TRIG:COUN?", ":TRIG:DEL?",
        ":FORM:ELEM?", ":OUTP?", ":OUTP:SMOD?",
    ]
    for q in queries:
        tracer.query(q)
    return trace


def scenario_resistance_4wire(dev, idn: str) -> Trace:
    trace = _new_trace(
        "resistance_4wire_1ma_100ohm",
        "Resistance mode, 4-wire, 1mA test current. 3 readings on a 100Ω DUT.",
        idn,
    )
    tracer = ScpiTracer(dev, trace)
    _drain_errors(tracer)
    _hard_reset(tracer)
    tracer.write(":SYST:RSEN ON")
    tracer.write(":SENS:FUNC:CONC OFF")
    tracer.write(":SENS:FUNC 'RES'")
    tracer.write(":SENS:RES:MODE MAN")        # required before configuring source
    tracer.write(":SOUR:FUNC CURR")
    tracer.write(":SOUR:CURR:RANG 1e-3")
    tracer.write(":SOUR:CURR 1e-3")
    tracer.write(":SENS:VOLT:PROT 5")
    tracer.write(":SENS:RES:NPLC 1")
    tracer.write(":SENS:RES:MODE AUTO")
    tracer.write(":FORM:ELEM RES,STAT")
    tracer.write(":OUTP ON")
    time.sleep(0.2)
    for _ in range(3):
        tracer.query(":READ?")
        time.sleep(0.1)
    tracer.write(":OUTP OFF")
    return trace


def scenario_source_v(dev, idn: str) -> Trace:
    trace = _new_trace(
        "source_v_0p1v_into_100ohm",
        "Voltage source mode, 0.1V into 100Ω, expect ~1mA. 3 readings.",
        idn,
    )
    tracer = ScpiTracer(dev, trace)
    _drain_errors(tracer)
    _hard_reset(tracer)
    tracer.write(":SYST:RSEN ON")
    tracer.write(":SENS:FUNC:CONC OFF")
    tracer.write(":SENS:FUNC 'CURR:DC'")
    tracer.write(":SOUR:FUNC VOLT")
    tracer.write(":SOUR:VOLT:RANG 0.2")
    tracer.write(":SOUR:VOLT 0.1")
    tracer.write(":SENS:CURR:PROT 0.1")
    tracer.write(":SENS:CURR:RANG:AUTO ON")
    tracer.write(":SENS:CURR:NPLC 1")
    tracer.write(":FORM:ELEM VOLT,CURR,STAT")
    tracer.write(":OUTP ON")
    time.sleep(0.2)
    for _ in range(3):
        tracer.query(":READ?")
        time.sleep(0.1)
    tracer.write(":OUTP OFF")
    return trace


def scenario_source_i(dev, idn: str) -> Trace:
    trace = _new_trace(
        "source_i_1ma_into_100ohm",
        "Current source mode, 1mA into 100Ω, expect ~0.1V. 3 readings.",
        idn,
    )
    tracer = ScpiTracer(dev, trace)
    _drain_errors(tracer)
    _hard_reset(tracer)
    tracer.write(":SYST:RSEN ON")
    tracer.write(":SENS:FUNC:CONC OFF")
    tracer.write(":SENS:FUNC 'VOLT:DC'")
    tracer.write(":SOUR:FUNC CURR")
    tracer.write(":SOUR:CURR:RANG 1e-3")
    tracer.write(":SOUR:CURR 1e-3")
    tracer.write(":SENS:VOLT:PROT 5")
    tracer.write(":SENS:VOLT:RANG:AUTO ON")
    tracer.write(":SENS:VOLT:NPLC 1")
    tracer.write(":FORM:ELEM VOLT,CURR,STAT")
    tracer.write(":OUTP ON")
    time.sleep(0.2)
    for _ in range(3):
        tracer.query(":READ?")
        time.sleep(0.1)
    tracer.write(":OUTP OFF")
    return trace


def scenario_four_point(dev, idn: str) -> Trace:
    trace = _new_trace(
        "four_point_1ma_into_100ohm",
        "Four-point probe configuration (current source, voltage measure). 3 readings.",
        idn,
    )
    tracer = ScpiTracer(dev, trace)
    _drain_errors(tracer)
    _hard_reset(tracer)
    tracer.write(":SYST:RSEN OFF")          # 4PP cabling is separate from RSEN
    tracer.write(":SENS:FUNC:CONC OFF")
    tracer.write(":SENS:FUNC 'VOLT:DC'")
    tracer.write(":SOUR:FUNC CURR")
    tracer.write(":SOUR:CURR:RANG 1e-3")
    tracer.write(":SOUR:CURR 1e-3")
    tracer.write(":SENS:VOLT:PROT 5")
    tracer.write(":SENS:VOLT:RANG:AUTO ON")
    tracer.write(":SENS:VOLT:NPLC 1")
    tracer.write(":FORM:ELEM VOLT,CURR,STAT")
    tracer.write(":OUTP ON")
    time.sleep(0.2)
    for _ in range(3):
        tracer.query(":READ?")
        time.sleep(0.1)
    tracer.write(":OUTP OFF")
    return trace


def scenario_sweep_v_up(dev, idn: str) -> Trace:
    trace = _new_trace(
        "sweep_v_0_to_0p5v_5pts_into_100ohm",
        "Voltage sweep 0→0.5V, step 0.125V (5 points). Expect linear I = V/100Ω.",
        idn,
    )
    tracer = ScpiTracer(dev, trace)
    _drain_errors(tracer)
    _hard_reset(tracer)
    tracer.write(":SENS:FUNC:CONC OFF")
    tracer.write(":SOUR:FUNC VOLT")
    tracer.write(":SENS:FUNC 'CURR:DC'")
    tracer.write(":SENS:CURR:PROT 0.1")
    tracer.write(":SENS:CURR:NPLC 1")
    tracer.write(":SOUR:VOLT:START 0.0")
    tracer.write(":SOUR:VOLT:STOP 0.5")
    tracer.write(":SOUR:VOLT:STEP 0.125")
    tracer.write(":SOUR:VOLT:MODE SWE")
    tracer.write(":SOUR:SWE:SPAC LIN")
    tracer.write(":SOUR:SWE:RANG AUTO")
    tracer.write(":TRIG:COUN 5")
    tracer.write(":SOUR:DEL 0.01")
    tracer.write(":FORM:ELEM VOLT,CURR,STAT")
    # Long timeout for the sweep
    prior = dev.timeout
    dev.timeout = 30000
    try:
        tracer.write(":OUTP ON")
        tracer.query(":READ?")
        tracer.write(":OUTP OFF")
    finally:
        dev.timeout = prior
    return trace


def scenario_compliance_in(dev, idn: str) -> Trace:
    trace = _new_trace(
        "compliance_v_in_compliance",
        "Source 0.5V into 100Ω with 1mA current compliance. I clamps, V≈0.1V, STAT bit 3 set.",
        idn,
    )
    tracer = ScpiTracer(dev, trace)
    _drain_errors(tracer)
    _hard_reset(tracer)
    tracer.write(":SYST:RSEN ON")
    tracer.write(":SENS:FUNC:CONC OFF")
    tracer.write(":SENS:FUNC 'CURR:DC'")
    tracer.write(":SOUR:FUNC VOLT")
    tracer.write(":SOUR:VOLT:RANG 1.0")
    tracer.write(":SOUR:VOLT 0.5")
    tracer.write(":SENS:CURR:PROT 1e-3")
    tracer.write(":SENS:CURR:RANG 1e-3")
    tracer.write(":SENS:CURR:NPLC 1")
    tracer.write(":FORM:ELEM VOLT,CURR,STAT")
    tracer.write(":OUTP ON")
    time.sleep(0.2)
    tracer.query(":READ?")
    tracer.write(":OUTP OFF")
    return trace


def scenario_compliance_out(dev, idn: str) -> Trace:
    trace = _new_trace(
        "compliance_v_not_in_compliance",
        "Source 0.5V into 100Ω with 100mA current compliance. Hits 5mA, STAT bit 3 clear.",
        idn,
    )
    tracer = ScpiTracer(dev, trace)
    _drain_errors(tracer)
    _hard_reset(tracer)
    tracer.write(":SYST:RSEN ON")
    tracer.write(":SENS:FUNC:CONC OFF")
    tracer.write(":SENS:FUNC 'CURR:DC'")
    tracer.write(":SOUR:FUNC VOLT")
    tracer.write(":SOUR:VOLT:RANG 1.0")
    tracer.write(":SOUR:VOLT 0.5")
    tracer.write(":SENS:CURR:PROT 100e-3")
    tracer.write(":SENS:CURR:RANG 100e-3")
    tracer.write(":SENS:CURR:NPLC 1")
    tracer.write(":FORM:ELEM VOLT,CURR,STAT")
    tracer.write(":OUTP ON")
    time.sleep(0.2)
    tracer.query(":READ?")
    tracer.write(":OUTP OFF")
    return trace


def scenario_quirk_auto_ohms(dev, idn: str) -> Trace:
    trace = _new_trace(
        "quirk_auto_ohms_rejects_source",
        "After :SENS:FUNC 'RES', auto-ohms is ON and rejects source/compliance commands "
        "with error 825. The fix is :SENS:RES:MODE MAN.",
        idn,
    )
    tracer = ScpiTracer(dev, trace)
    _drain_errors(tracer)
    _hard_reset(tracer)
    tracer.write(":SENS:FUNC 'RES'")
    tracer.query(":SYST:ERR?")               # expect 0
    tracer.write(":SOUR:CURR:RANG 1e-3")
    tracer.query(":SYST:ERR?")               # expect 825
    tracer.write(":SOUR:CURR 1e-3")
    tracer.query(":SYST:ERR?")               # expect 825
    tracer.write(":SENS:VOLT:PROT 5")
    tracer.query(":SYST:ERR?")               # expect 825
    tracer.write(":SENS:RES:MODE MAN")
    tracer.query(":SYST:ERR?")               # expect 0
    tracer.write(":SOUR:CURR 1e-3")
    tracer.query(":SYST:ERR?")               # expect 0
    return trace


def scenario_quirk_init_cont(dev, idn: str) -> Trace:
    trace = _new_trace(
        "quirk_init_cont_unsupported",
        ":INIT:CONT subsystem does not exist on the 2400 series. Should produce -113.",
        idn,
    )
    tracer = ScpiTracer(dev, trace)
    _drain_errors(tracer)
    _hard_reset(tracer)
    tracer.write(":INIT:CONT ON")
    tracer.query(":SYST:ERR?")               # expect -113
    return trace


def scenario_quirk_form_elem_order(dev, idn: str) -> Trace:
    trace = _new_trace(
        "quirk_form_elem_canonical_order",
        "FORM:ELEM argument order is silently re-ordered to canonical "
        "(VOLT,CURR,RES,TIME,STAT). Argument SET is honored, ORDER is ignored.",
        idn,
    )
    tracer = ScpiTracer(dev, trace)
    _drain_errors(tracer)
    _hard_reset(tracer)
    tracer.write(":FORM:ELEM CURR,VOLT,STAT")
    tracer.query(":FORM:ELEM?")              # expect VOLT,CURR,STAT
    tracer.write(":FORM:ELEM STAT,RES,VOLT")
    tracer.query(":FORM:ELEM?")              # expect VOLT,RES,STAT
    return trace


def scenario_quirk_concurrent_default_on(dev, idn: str) -> Trace:
    trace = _new_trace(
        "quirk_concurrent_default_on",
        "After *RST, :SENS:FUNC:CONC is ON — instrument measures all functions. "
        "Every Keithley programming example starts with :SENS:FUNC:CONC OFF.",
        idn,
    )
    tracer = ScpiTracer(dev, trace)
    _drain_errors(tracer)
    _hard_reset(tracer)
    tracer.query(":SENS:FUNC:CONC?")         # expect 1
    return trace


def scenario_filter_repeat(dev, idn: str) -> Trace:
    trace = _new_trace(
        "filter_repeat_x10",
        "Hardware averaging filter (REP, count 10) enabled — :SENS:AVER ON returns averaged readings.",
        idn,
    )
    tracer = ScpiTracer(dev, trace)
    _drain_errors(tracer)
    _hard_reset(tracer)
    tracer.write(":SYST:RSEN ON")
    tracer.write(":SENS:FUNC:CONC OFF")
    tracer.write(":SENS:FUNC 'CURR:DC'")
    tracer.write(":SOUR:FUNC VOLT")
    tracer.write(":SOUR:VOLT 0.1")
    tracer.write(":SENS:CURR:PROT 0.1")
    tracer.write(":SENS:CURR:NPLC 1")
    tracer.write(":SENS:AVER:TCON REP")
    tracer.write(":SENS:AVER:COUN 10")
    tracer.write(":SENS:AVER ON")
    tracer.write(":FORM:ELEM VOLT,CURR,STAT")
    tracer.write(":OUTP ON")
    time.sleep(0.3)
    tracer.query(":READ?")
    tracer.write(":OUTP OFF")
    tracer.query(":SENS:AVER?")              # expect 1
    tracer.query(":SENS:AVER:TCON?")         # expect REP
    tracer.query(":SENS:AVER:COUN?")         # expect 10
    return trace


def scenario_offset_compensated_ohms(dev, idn: str) -> Trace:
    trace = _new_trace(
        "offset_compensated_ohms",
        ":SENS:RES:OCOM ON cancels thermoelectric EMF in resistance mode.",
        idn,
    )
    tracer = ScpiTracer(dev, trace)
    _drain_errors(tracer)
    _hard_reset(tracer)
    tracer.write(":SYST:RSEN ON")
    tracer.write(":SENS:FUNC:CONC OFF")
    tracer.write(":SENS:FUNC 'RES'")
    tracer.write(":SENS:RES:MODE MAN")
    tracer.write(":SOUR:FUNC CURR")
    tracer.write(":SOUR:CURR 1e-3")
    tracer.write(":SENS:VOLT:PROT 5")
    tracer.write(":SENS:RES:OCOM ON")
    tracer.query(":SENS:RES:OCOM?")          # expect 1
    tracer.write(":FORM:ELEM RES,STAT")
    tracer.write(":OUTP ON")
    time.sleep(0.3)
    tracer.query(":READ?")
    tracer.write(":OUTP OFF")
    return trace


def scenario_outp_smod_himp(dev, idn: str) -> Trace:
    trace = _new_trace(
        "output_off_himp",
        ":OUTP:SMOD HIMP physically opens the output relay when output is OFF (DUT safety).",
        idn,
    )
    tracer = ScpiTracer(dev, trace)
    _drain_errors(tracer)
    _hard_reset(tracer)
    tracer.query(":OUTP:SMOD?")              # expect NORM (default)
    tracer.write(":OUTP:SMOD HIMP")
    tracer.query(":OUTP:SMOD?")              # expect HIMP
    return trace


SCENARIOS: list[Callable] = [
    scenario_baseline_reset_state,
    scenario_resistance_4wire,
    scenario_source_v,
    scenario_source_i,
    scenario_four_point,
    scenario_sweep_v_up,
    scenario_compliance_in,
    scenario_compliance_out,
    scenario_quirk_auto_ohms,
    scenario_quirk_init_cont,
    scenario_quirk_form_elem_order,
    scenario_quirk_concurrent_default_on,
    scenario_filter_repeat,
    scenario_offset_compensated_ohms,
    scenario_outp_smod_himp,
]


def main() -> int:
    rm = pyvisa.ResourceManager()
    print(f"Resources: {rm.list_resources()}")
    dev = rm.open_resource(GPIB_ADDR)
    dev.timeout = 10000
    try:
        dev.read_termination = "\n"
        dev.write_termination = "\n"
    except Exception:
        pass

    idn = dev.query("*IDN?").strip()
    print(f"IDN: {idn}")
    print(f"DUT: {DUT_OHMS}Ω (4-wire Kelvin)")
    print(f"Trace dir: {trace_dir()}")

    n_ok = 0
    n_fail = 0
    out_dir = trace_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        for scn in SCENARIOS:
            name = scn.__name__.removeprefix("scenario_")
            print(f"\n--- {name} ---")
            try:
                trace = scn(dev, idn)
                target = out_dir / f"{trace.name}.json"
                trace.write(target)
                n_ok += 1
                print(f"  ok  -> {target.name} ({len(trace.events)} events)")
            except Exception as e:
                n_fail += 1
                print(f"  FAIL: {type(e).__name__}: {e}")
                # Best-effort cleanup before the next scenario
                try:
                    dev.write(":OUTP OFF")
                    dev.write("*CLS")
                except Exception:
                    pass
    finally:
        try:
            dev.write(":OUTP OFF")
        except Exception:
            pass
        dev.close()
        rm.close()

    print(f"\nDone. {n_ok} ok / {n_fail} failed")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

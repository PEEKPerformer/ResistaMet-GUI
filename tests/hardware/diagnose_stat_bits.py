"""Diagnose Keithley 2400 series :READ? STAT-element bit semantics.

DUT: 100Ω resistor in 4-wire Kelvin connection.

Procedure:
    1. Source V at 0.5V with high current compliance (100mA).
       Expected: I = 0.5/100 = 5mA, no compliance.
    2. Source V at 0.5V with low current compliance (1mA).
       Expected: I clamped to 1mA, V actual = 0.1V, IS in compliance.
    3. XOR the STAT words to find which bit toggled.
    4. Also probe :STAT:MEAS:COND? at the same time to compare with the
       Measurement Event register documented at +114 / bit 14 (manual B-1).
    5. Verify the FORM:ELEM order quirk: write CURR,VOLT,STAT and confirm
       the response is still V,I,STAT (canonical order).

Run via:  ssh resistamet python - < diagnose_stat_bits.py
"""
from __future__ import annotations

import sys
import time

import pyvisa


def setup_source_v(dev, voltage: float, i_compliance: float) -> None:
    dev.write("*RST"); time.sleep(0.5)
    dev.write("*CLS")
    dev.write(":SYST:RSEN ON")           # 4-wire (DUT is wired Kelvin)
    dev.write(":SENS:FUNC:CONC OFF")
    dev.write(":SENS:FUNC 'CURR:DC'")
    dev.write(":SOUR:FUNC VOLT")
    dev.write(f":SOUR:VOLT:RANG {abs(voltage)}")
    dev.write(f":SOUR:VOLT {voltage}")
    dev.write(f":SENS:CURR:PROT {i_compliance}")
    dev.write(f":SENS:CURR:RANG {i_compliance}")
    dev.write(":SENS:CURR:NPLC 1")
    dev.write(":FORM:ELEM VOLT,CURR,STAT")


def parse_stat(reading: str) -> tuple[float, float, int]:
    parts = [p.strip() for p in reading.split(",") if p.strip()]
    return float(parts[0]), float(parts[1]), int(float(parts[-1]))


def diagnose_compliance_bit(dev) -> dict:
    print("\n=== Diagnosis 1: STAT bit for compliance ===")

    # Run 1: NOT in compliance
    setup_source_v(dev, voltage=0.5, i_compliance=100e-3)
    dev.write(":OUTP ON"); time.sleep(0.2)
    raw_ok = dev.query(":READ?").strip()
    meas_cond_ok = dev.query(":STAT:MEAS:COND?").strip()
    dev.write(":OUTP OFF")
    v_ok, i_ok, stat_ok = parse_stat(raw_ok)
    print(f"  not-in-compliance: raw='{raw_ok}'")
    print(f"    parsed: V={v_ok:.6e} I={i_ok:.6e} STAT={stat_ok} ({stat_ok:#024b})")
    print(f"    :STAT:MEAS:COND? = {meas_cond_ok}")

    time.sleep(0.5)

    # Run 2: IN compliance — same 0.5V request, but I is clamped to 1mA so V_actual=0.1V
    setup_source_v(dev, voltage=0.5, i_compliance=1e-3)
    dev.write(":OUTP ON"); time.sleep(0.2)
    raw_comp = dev.query(":READ?").strip()
    meas_cond_comp = dev.query(":STAT:MEAS:COND?").strip()
    dev.write(":OUTP OFF")
    v_comp, i_comp, stat_comp = parse_stat(raw_comp)
    print(f"  in-compliance: raw='{raw_comp}'")
    print(f"    parsed: V={v_comp:.6e} I={i_comp:.6e} STAT={stat_comp} ({stat_comp:#024b})")
    print(f"    :STAT:MEAS:COND? = {meas_cond_comp}")

    diff = stat_ok ^ stat_comp
    set_bits = [b for b in range(24) if diff & (1 << b)]
    set_in_comp_only = [b for b in range(24) if (stat_comp & (1 << b)) and not (stat_ok & (1 << b))]
    print(f"  stat XOR = {diff} ({diff:#024b})")
    print(f"  bits that differ: {set_bits}")
    print(f"  bits set ONLY in compliance: {set_in_comp_only}")

    return {
        "stat_ok": stat_ok,
        "stat_comp": stat_comp,
        "v_ok": v_ok, "i_ok": i_ok,
        "v_comp": v_comp, "i_comp": i_comp,
        "diff_bits": set_bits,
        "comp_only_bits": set_in_comp_only,
        "meas_cond_ok": meas_cond_ok,
        "meas_cond_comp": meas_cond_comp,
    }


def diagnose_form_elem_order(dev) -> dict:
    print("\n=== Diagnosis 2: FORM:ELEM argument order ===")
    setup_source_v(dev, voltage=0.1, i_compliance=10e-3)

    # Canonical order
    dev.write(":FORM:ELEM VOLT,CURR,STAT")
    elem_a = dev.query(":FORM:ELEM?").strip()
    dev.write(":OUTP ON"); time.sleep(0.2)
    raw_a = dev.query(":READ?").strip()
    dev.write(":OUTP OFF")
    print(f"  request VOLT,CURR,STAT  -> ELEM? = {elem_a}")
    print(f"    READ? = '{raw_a}'")

    time.sleep(0.3)

    # Reversed order — quirk: should still come back V,I,STAT
    dev.write(":FORM:ELEM CURR,VOLT,STAT")
    elem_b = dev.query(":FORM:ELEM?").strip()
    dev.write(":OUTP ON"); time.sleep(0.2)
    raw_b = dev.query(":READ?").strip()
    dev.write(":OUTP OFF")
    print(f"  request CURR,VOLT,STAT  -> ELEM? = {elem_b}")
    print(f"    READ? = '{raw_b}'")

    parts_a = [float(p.strip()) for p in raw_a.split(",") if p.strip()]
    parts_b = [float(p.strip()) for p in raw_b.split(",") if p.strip()]
    # If quirk holds, parts_a[0] ≈ parts_b[0] (both V)
    quirk_holds = abs(parts_a[0] - parts_b[0]) < abs(parts_a[0] - parts_b[1])
    print(f"  fixed-order quirk holds? {quirk_holds}")
    print(f"    raw_a[0]={parts_a[0]:.6e}  raw_b[0]={parts_b[0]:.6e}  raw_b[1]={parts_b[1]:.6e}")

    return {
        "elem_a": elem_a, "raw_a": raw_a,
        "elem_b": elem_b, "raw_b": raw_b,
        "quirk_holds": quirk_holds,
    }


def diagnose_auto_ohms_quirk(dev) -> dict:
    print("\n=== Diagnosis 3: Auto-ohms RES function quirk ===")
    dev.write("*RST"); time.sleep(0.5)
    dev.write("*CLS")

    # Drain error queue
    while True:
        err = dev.query(":SYST:ERR?").strip()
        if err.startswith("0,") or err.startswith("+0,"):
            break

    # Select RES function (auto-ohms is ON by default per memory)
    dev.write(":SENS:FUNC 'RES'")
    err_after_res = dev.query(":SYST:ERR?").strip()
    print(f"  after :SENS:FUNC 'RES': err = {err_after_res}")

    # Try to set source current — memory claims this should error 825
    dev.write(":SOUR:CURR:RANG 1e-3")
    err_after_range = dev.query(":SYST:ERR?").strip()
    print(f"  after :SOUR:CURR:RANG 1e-3 (auto-ohms ON): err = {err_after_range}")

    dev.write(":SOUR:CURR 1e-3")
    err_after_curr = dev.query(":SYST:ERR?").strip()
    print(f"  after :SOUR:CURR 1e-3 (auto-ohms ON): err = {err_after_curr}")

    dev.write(":SENS:VOLT:PROT 5")
    err_after_prot = dev.query(":SYST:ERR?").strip()
    print(f"  after :SENS:VOLT:PROT 5 (auto-ohms ON): err = {err_after_prot}")

    # Now disable auto-ohms — same commands should succeed
    dev.write(":SENS:RES:MODE MAN")
    err_after_man = dev.query(":SYST:ERR?").strip()
    print(f"  after :SENS:RES:MODE MAN: err = {err_after_man}")

    dev.write(":SOUR:CURR 1e-3")
    err_after_curr2 = dev.query(":SYST:ERR?").strip()
    print(f"  after :SOUR:CURR 1e-3 (auto-ohms OFF): err = {err_after_curr2}")

    return {
        "after_res": err_after_res,
        "after_range_auto_on": err_after_range,
        "after_curr_auto_on": err_after_curr,
        "after_prot_auto_on": err_after_prot,
        "after_mode_man": err_after_man,
        "after_curr_auto_off": err_after_curr2,
    }


def diagnose_init_cont_quirk(dev) -> dict:
    print("\n=== Diagnosis 4: :INIT:CONT ON quirk ===")
    dev.write("*RST"); time.sleep(0.5)
    dev.write("*CLS")
    while not dev.query(":SYST:ERR?").strip().startswith(("0,", "+0,")):
        pass
    dev.write(":INIT:CONT ON")
    err = dev.query(":SYST:ERR?").strip()
    print(f"  after :INIT:CONT ON: err = {err}")
    return {"err_after_init_cont": err}


def diagnose_reset_state(dev) -> dict:
    print("\n=== Diagnosis 5: post-*RST default state ===")
    dev.write("*RST"); time.sleep(0.5)
    dev.write("*CLS")
    queries = [
        ":SYST:LFR?",
        ":SYST:RSEN?",
        ":SYST:AZER:STAT?",
        ":SENS:FUNC?",
        ":SENS:FUNC:CONC?",
        ":SENS:VOLT:NPLC?",
        ":SENS:CURR:NPLC?",
        ":SENS:RES:NPLC?",
        ":SENS:RES:MODE?",
        ":SENS:RES:OCOM?",
        ":SENS:VOLT:PROT?",
        ":SENS:CURR:PROT?",
        ":SENS:VOLT:RANG?",
        ":SENS:CURR:RANG?",
        ":SENS:VOLT:RANG:AUTO?",
        ":SENS:CURR:RANG:AUTO?",
        ":SENS:AVER?",
        ":SENS:AVER:TCON?",
        ":SENS:AVER:COUN?",
        ":SOUR:FUNC?",
        ":SOUR:VOLT?",
        ":SOUR:CURR?",
        ":SOUR:VOLT:RANG?",
        ":SOUR:CURR:RANG?",
        ":SOUR:VOLT:MODE?",
        ":SOUR:CURR:MODE?",
        ":SOUR:DEL?",
        ":SOUR:DEL:AUTO?",
        ":TRIG:COUN?",
        ":TRIG:DEL?",
        ":FORM:ELEM?",
        ":OUTP?",
        ":OUTP:SMOD?",
    ]
    state = {}
    for q in queries:
        try:
            resp = dev.query(q).strip()
        except Exception as e:
            resp = f"<error: {e}>"
        state[q] = resp
        print(f"  {q:30s} -> {resp}")
    return state


def main() -> int:
    rm = pyvisa.ResourceManager()
    print("resources:", rm.list_resources())
    dev = rm.open_resource("GPIB0::24::INSTR")
    dev.timeout = 10000
    try:
        dev.read_termination = "\n"
        dev.write_termination = "\n"
    except Exception:
        pass
    idn = dev.query("*IDN?").strip()
    print(f"IDN: {idn}")

    try:
        results = {}
        results["compliance"] = diagnose_compliance_bit(dev)
        results["form_elem"] = diagnose_form_elem_order(dev)
        results["auto_ohms"] = diagnose_auto_ohms_quirk(dev)
        results["init_cont"] = diagnose_init_cont_quirk(dev)
        results["reset_state"] = diagnose_reset_state(dev)

        print("\n=== SUMMARY ===")
        comp = results["compliance"]
        print(f"  compliance bits: {comp['comp_only_bits']}")
        fe = results["form_elem"]
        print(f"  FORM:ELEM order quirk: {fe['quirk_holds']}")
        ao = results["auto_ohms"]
        print(f"  auto-ohms quirk: err_after_curr_auto_on = {ao['after_curr_auto_on']}")
    finally:
        try:
            dev.write(":OUTP OFF")
        except Exception:
            pass
        dev.close()
        rm.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

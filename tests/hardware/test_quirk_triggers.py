"""Hardware-tier: confirm each documented Keithley 2400-series SCPI quirk
still reproduces against the real instrument. Run before each release.

Skipped unless RESISTAMET_HARDWARE_ADDR is set (see conftest.py).
"""
from __future__ import annotations

import time


def _drain(dev):
    """Empty the instrument's error queue."""
    while True:
        resp = dev.query(":SYST:ERR?").strip()
        if resp.startswith(("0,", "+0,")):
            return


def _hard_reset(dev):
    dev.write("*RST")
    time.sleep(0.5)
    dev.write("*CLS")
    _drain(dev)


def test_compliance_bit_is_bit_3(real_instrument, dut_ohms):
    """STAT element of FORM:ELEM signals compliance via bit 3 (NOT bit 14
    like the Measurement Event Register)."""
    dev = real_instrument
    _hard_reset(dev)
    dev.write(":SYST:RSEN ON")
    dev.write(":SENS:FUNC:CONC OFF")
    dev.write(":SENS:FUNC 'CURR:DC'")
    dev.write(":SOUR:FUNC VOLT")
    dev.write(":SOUR:VOLT:RANG 1.0")
    dev.write(":SOUR:VOLT 0.5")
    dev.write(":SENS:CURR:RANG 100e-3")
    dev.write(":FORM:ELEM VOLT,CURR,STAT")

    # Not in compliance: 100mA limit, 100Ω → 5mA, well under
    dev.write(":SENS:CURR:PROT 100e-3")
    dev.write(":OUTP ON"); time.sleep(0.2)
    parts = [p.strip() for p in dev.query(":READ?").split(",")]
    stat_ok = int(float(parts[-1]))
    dev.write(":OUTP OFF")

    time.sleep(0.3)

    # In compliance: 1mA limit, can't deliver 5mA → bit 3 sets
    dev.write(":SENS:CURR:PROT 1e-3")
    dev.write(":SENS:CURR:RANG 1e-3")
    dev.write(":OUTP ON"); time.sleep(0.2)
    parts = [p.strip() for p in dev.query(":READ?").split(",")]
    stat_comp = int(float(parts[-1]))
    dev.write(":OUTP OFF")

    diff = stat_ok ^ stat_comp
    assert diff == 8, f"compliance bit moved: stat_ok={stat_ok} stat_comp={stat_comp} diff={diff}"


def test_form_elem_reorders_to_canonical(real_instrument):
    """Argument ORDER to FORM:ELEM is ignored; the response always uses
    canonical order VOLT,CURR,RES,TIME,STAT."""
    dev = real_instrument
    _hard_reset(dev)
    dev.write(":FORM:ELEM CURR,VOLT,STAT")
    elem = dev.query(":FORM:ELEM?").strip()
    assert elem == "VOLT,CURR,STAT", f"FORM:ELEM was not re-ordered: {elem!r}"

    dev.write(":FORM:ELEM STAT,RES,VOLT")
    elem = dev.query(":FORM:ELEM?").strip()
    assert elem == "VOLT,RES,STAT", f"FORM:ELEM was not re-ordered: {elem!r}"


def test_auto_ohms_rejects_source_with_825(real_instrument):
    """After :SENS:FUNC 'RES', auto-ohms is ON and rejects source/compliance
    commands with error 825."""
    dev = real_instrument
    _hard_reset(dev)
    dev.write(":SENS:FUNC 'RES'")
    err = dev.query(":SYST:ERR?").strip()
    assert err.startswith(("0,", "+0,")), f"unexpected error after :SENS:FUNC 'RES': {err}"

    dev.write(":SOUR:CURR 1e-3")
    err = dev.query(":SYST:ERR?").strip()
    assert err.startswith(("825,", "+825,")), f"expected 825, got {err}"

    # Fix and retry
    dev.write(":SENS:RES:MODE MAN")
    _drain(dev)
    dev.write(":SOUR:CURR 1e-3")
    err = dev.query(":SYST:ERR?").strip()
    assert err.startswith(("0,", "+0,")), f"expected clean after RES:MODE MAN: {err}"


def test_init_cont_unsupported_yields_113(real_instrument):
    """The :INIT:CONT subsystem does not exist on the 2400 series."""
    dev = real_instrument
    _hard_reset(dev)
    dev.write(":INIT:CONT ON")
    err = dev.query(":SYST:ERR?").strip()
    assert err.startswith("-113,"), f"expected -113 'Undefined header', got {err}"


def test_concurrent_default_on_after_rst(real_instrument):
    """After *RST, :SENS:FUNC:CONC is ON. Every Keithley programming
    example starts with :SENS:FUNC:CONC OFF for a reason."""
    dev = real_instrument
    _hard_reset(dev)
    conc = dev.query(":SENS:FUNC:CONC?").strip()
    assert conc == "1", f"expected CONC ON after *RST, got {conc}"


def test_post_rst_default_function_is_curr_dc(real_instrument):
    """After *RST, the sense function defaults to CURR:DC (not VOLT:DC)."""
    dev = real_instrument
    _hard_reset(dev)
    func = dev.query(":SENS:FUNC?").strip()
    assert "CURR" in func.upper(), f"expected CURR:DC default, got {func}"

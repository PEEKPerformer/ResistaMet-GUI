"""Tests for the Keithley2400 SCPI wrapper in resistamet_gui/instrument.py.

These tests run the wrapper against a FakeKeithley and verify both:
    1. The SCPI commands sent are correct (and in the right order — the
       :SENS:RES:MODE MAN-before-SOUR:CURR ordering is regression-tested
       because the auto-ohms quirk causes error 825 if violated).
    2. After configuration, ``:READ?`` produces the expected element layout
       (e.g. resistance mode emits two elements, source modes emit three).
"""
from __future__ import annotations

import pyvisa
import pytest

from resistamet_gui.instrument import Keithley2400, VisaInstrument


def _commands(fake) -> list[str]:
    """Extract just the write commands from the fake's command log."""
    return [cmd for op, cmd in fake.command_log if op == "write"]


def _connect_against_fake(fake_rm) -> Keithley2400:
    """Open a Keithley2400 wrapper against the fake resource manager."""
    inst = Keithley2400("GPIB0::24::INSTR")
    inst.connect()
    # Replace whatever the connect path opened with the fake's instance,
    # so subsequent assertions can introspect the same object.
    return inst


# --------------------------------------------------------------- VisaInstrument

class TestVisaInstrument:
    def test_connect_lists_resources_and_opens(self, fake_rm):
        inst = VisaInstrument("GPIB0::24::INSTR")
        inst.connect()
        assert inst.dev is not None
        # Termination should have been set
        assert inst.dev.read_termination == "\n"
        assert inst.dev.write_termination == "\n"
        inst.close()

    def test_connect_raises_for_missing_resource(self, fake_rm):
        inst = VisaInstrument("GPIB0::24::WRONGADDR")
        with pytest.raises(RuntimeError, match="not found"):
            inst.connect()

    def test_idn_round_trip(self, fake_rm):
        inst = VisaInstrument("GPIB0::24::INSTR").connect()
        try:
            assert "KEITHLEY" in inst.idn()
            assert "MODEL 2420" in inst.idn()
        finally:
            inst.close()

    def test_reset_and_clear_sequence(self, fake_rm):
        inst = VisaInstrument("GPIB0::24::INSTR").connect()
        try:
            inst.reset_and_clear()
            cmds = [c for op, c in inst.dev.command_log if op == "write"]
            assert cmds[0] == "*RST"
            assert "*CLS" in cmds
        finally:
            inst.close()


# ---------------------------------------------------------- Keithley2400 setups

class TestSetupResistance:
    def test_writes_res_mode_man_before_sour_curr(self, fake_rm):
        """Regression: auto-ohms quirk causes error 825 if SOUR:CURR is sent
        while RES function has SENS:RES:MODE AUTO. Wrapper must sequence
        :SENS:RES:MODE MAN before configuring source/compliance.
        """
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        try:
            inst.setup_resistance(test_current=1e-3, v_comp=5.0, nplc=1.0,
                                  auto_range=True, four_wire=True)
            cmds = _commands(inst.dev)
            # :SENS:RES:MODE MAN must appear before :SOUR:CURR
            res_mode_man_idx = next(i for i, c in enumerate(cmds)
                                     if c.upper().startswith(":SENS:RES:MODE MAN"))
            sour_curr_idx = next(i for i, c in enumerate(cmds)
                                  if c.upper().startswith(":SOUR:CURR ")
                                  and not c.upper().startswith(":SOUR:CURR:"))
            assert res_mode_man_idx < sour_curr_idx
            # Also: no error 825 should have been queued (i.e. all writes valid)
            err = inst.dev.query(":SYST:ERR?")
            assert err.startswith("0,")
        finally:
            inst.close()

    def test_4wire_enables_rsen(self, fake_rm):
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        try:
            inst.setup_resistance(test_current=1e-3, v_comp=5.0, nplc=1.0,
                                  auto_range=True, four_wire=True)
            assert inst.dev.state["syst_rsen"] is True
        finally:
            inst.close()

    def test_2wire_disables_rsen(self, fake_rm):
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        try:
            inst.setup_resistance(test_current=1e-3, v_comp=5.0, nplc=1.0,
                                  auto_range=True, four_wire=False)
            assert inst.dev.state["syst_rsen"] is False
        finally:
            inst.close()

    def test_form_elem_set_to_res_stat(self, fake_rm):
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        try:
            inst.setup_resistance(1e-3, 5.0, 1.0, True, True)
            elem = inst.dev.query(":FORM:ELEM?")
            # FORM:ELEM is canonical-ordered — RES,STAT preserved
            assert elem == "RES,STAT"
        finally:
            inst.close()

    def test_manual_range_uses_compliance_over_current(self, fake_rm):
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        try:
            inst.setup_resistance(1e-3, 5.0, 1.0, auto_range=False, four_wire=True)
            # RANG should be ~ v_comp / current = 5 / 1e-3 = 5000
            assert inst.dev.state["sens_res_rang"] == pytest.approx(5000.0, rel=1e-3)
        finally:
            inst.close()


class TestSetupSourceVoltage:
    def test_form_elem_volt_curr_stat(self, fake_rm):
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        try:
            inst.setup_source_voltage(voltage=1.0, i_comp=0.1, nplc=1.0,
                                       auto_range_curr=True)
            assert inst.dev.query(":FORM:ELEM?") == "VOLT,CURR,STAT"
        finally:
            inst.close()

    def test_source_volt_set(self, fake_rm):
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        try:
            inst.setup_source_voltage(voltage=1.5, i_comp=0.05, nplc=1.0,
                                       auto_range_curr=True)
            assert inst.dev.state["sour_volt"] == pytest.approx(1.5)
            assert inst.dev.state["sens_curr_prot"] == pytest.approx(0.05)
        finally:
            inst.close()

    def test_disables_4wire_for_source_v(self, fake_rm):
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        try:
            inst.setup_source_voltage(1.0, 0.1, 1.0, True)
            # Source-V mode in our wrapper always sets 2-wire
            assert inst.dev.state["syst_rsen"] is False
        finally:
            inst.close()


class TestSetupSourceCurrent:
    def test_form_elem_volt_curr_stat(self, fake_rm):
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        try:
            inst.setup_source_current(current=1e-3, v_comp=5.0, nplc=1.0,
                                       auto_range_volt=True)
            assert inst.dev.query(":FORM:ELEM?") == "VOLT,CURR,STAT"
        finally:
            inst.close()

    def test_source_curr_set(self, fake_rm):
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        try:
            inst.setup_source_current(2e-3, 5.0, 1.0, True)
            assert inst.dev.state["sour_curr"] == pytest.approx(2e-3)
            assert inst.dev.state["sens_volt_prot"] == pytest.approx(5.0)
        finally:
            inst.close()


class TestSetupSweep:
    def test_voltage_sweep_point_count(self, fake_rm):
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        try:
            n = inst.setup_sweep("VOLT", 0.0, 1.0, 0.1, 0.1, 1.0, source_delay=0.01)
            # 0 to 1.0 step 0.1 should give 11 points
            assert n == 11
            assert inst.dev.state["trig_coun"] == 11
            assert inst.dev.state["sour_volt_mode"] == "SWE"
            assert inst.dev.state["sour_volt_start"] == pytest.approx(0.0)
            assert inst.dev.state["sour_volt_stop"] == pytest.approx(1.0)
        finally:
            inst.close()

    def test_current_sweep_point_count(self, fake_rm):
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        try:
            n = inst.setup_sweep("CURR", 0.0, 1e-3, 1e-4, 5.0, 1.0, 0.0)
            assert n == 11
            assert inst.dev.state["sour_curr_mode"] == "SWE"
        finally:
            inst.close()

    def test_sweep_form_elem(self, fake_rm):
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        try:
            inst.setup_sweep("VOLT", 0.0, 0.5, 0.125, 0.1, 1.0, 0.0)
            assert inst.dev.query(":FORM:ELEM?") == "VOLT,CURR,STAT"
        finally:
            inst.close()


class TestCommonFast:
    def test_writes_trig_del_zero_and_sour_del_auto(self, fake_rm):
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        try:
            inst.common_fast()
            assert inst.dev.state["trig_del"] == 0.0
            assert inst.dev.state["sour_del_auto"] is True
        finally:
            inst.close()


# ----------------------------------------------------------- READ? integration

class TestReadAfterSetup:
    def test_resistance_read_returns_two_elements(self, fake_rm):
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        try:
            inst.setup_resistance(1e-3, 5.0, 1.0, True, True)
            inst.write(":OUTP ON")
            response = inst.query(":READ?")
            parts = response.split(",")
            assert len(parts) == 2
            r = float(parts[0])
            assert r == pytest.approx(100.0, rel=0.01)  # default DUT
        finally:
            inst.close()

    def test_source_v_read_returns_three_elements(self, fake_rm):
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        try:
            inst.setup_source_voltage(0.1, 0.1, 1.0, True)
            inst.write(":OUTP ON")
            response = inst.query(":READ?")
            parts = response.split(",")
            assert len(parts) == 3
            v = float(parts[0])
            i = float(parts[1])
            assert v == pytest.approx(0.1, rel=0.01)
            assert i == pytest.approx(0.001, rel=0.01)
        finally:
            inst.close()

    def test_compliance_sets_stat_bit_3(self, fake_rm):
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        try:
            inst.setup_source_voltage(voltage=0.5, i_comp=1e-3, nplc=1.0,
                                       auto_range_curr=False)
            inst.write(":OUTP ON")
            response = inst.query(":READ?")
            parts = response.split(",")
            stat = int(float(parts[2]))
            assert stat & (1 << 3), f"compliance bit not set: STAT={stat}"
        finally:
            inst.close()

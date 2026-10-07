"""Tests for the Keithley2400 SCPI wrapper in resistamet_gui/instrument.py.

These tests run the wrapper against a FakeKeithley and verify both:
    1. The SCPI commands sent are correct (and in the right order — the
       :SENS:RES:MODE MAN-before-SOUR:CURR ordering is regression-tested
       because the auto-ohms quirk causes error 825 if violated).
    2. After configuration, ``:READ?`` produces the expected element layout
       (a resistance run reads four elements, a source-V run three).
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


# ------------------------------------------------ the live resistance configure

class TestConfigureResistance:
    """Regression: the auto-ohms quirk queues error 825 when :SOUR:CURR,
    :SOUR:CURR:RANG or :SENS:VOLT:PROT is sent while the RES function has
    :SENS:RES:MODE AUTO. ``configure_resistance``, which every resistance run
    goes through, must select manual ohms before configuring the source.
    """

    def _configure(self, fake_rm, **overrides):
        from resistamet_gui.session.configure import configure_resistance
        from resistamet_gui.session.emitter import EventEmitter, ListSink

        settings = {
            'res_test_current': 1e-3, 'res_voltage_compliance': 5.0,
            'res_measurement_type': '4-wire', 'res_auto_range': True,
            'res_offset_comp': False, 'res_cable_null': 0.0,
        }
        settings.update(overrides)
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        configure_resistance(inst, EventEmitter(ListSink()), settings, 1.0)
        return inst

    @pytest.mark.parametrize("auto_range", [True, False])
    def test_writes_res_mode_man_before_sourcing(self, fake_rm, auto_range):
        inst = self._configure(fake_rm, res_auto_range=auto_range)
        try:
            cmds = [c.upper() for c in _commands(inst.dev)]
            man = cmds.index(":SENS:RES:MODE MAN")
            assert cmds.index(":SENS:FUNC 'RES'") < man
            sourcing = [i for i, c in enumerate(cmds)
                        if c.startswith((":SOUR:CURR", ":SENS:VOLT:PROT"))]
            assert sourcing and man < min(sourcing), cmds
            # The fake queues 825 for a source write under auto-ohms.
            assert inst.dev.query(":SYST:ERR?").startswith("0,")
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


class TestReadAfterConfigure:
    """:READ? layout and compliance after the configure a run uses."""

    def _configure(self, configure, settings):
        from resistamet_gui.session.emitter import EventEmitter, ListSink
        inst = Keithley2400("GPIB0::24::INSTR").connect()
        configure(inst, EventEmitter(ListSink()), settings, 1.0)
        return inst

    def test_resistance_read_returns_four_elements(self, fake_rm):
        from resistamet_gui.session.configure import configure_resistance
        inst = self._configure(configure_resistance, {
            'res_test_current': 1e-3, 'res_voltage_compliance': 5.0,
            'res_measurement_type': '4-wire', 'res_auto_range': True,
            'res_offset_comp': False, 'res_cable_null': 0.0,
        })
        try:
            inst.write(":OUTP ON")
            response = inst.query(":READ?")
            parts = response.split(",")
            assert len(parts) == 4  # VOLT, CURR, RES, STAT
            v, i, r = (float(x) for x in parts[:3])
            assert i == pytest.approx(1e-3, rel=0.01)
            assert v == pytest.approx(0.1, rel=0.01)
            assert r == pytest.approx(100.0, rel=0.01)  # default DUT
        finally:
            inst.close()

    def test_source_v_read_returns_three_elements(self, fake_rm):
        from resistamet_gui.session.configure import configure_source_v
        inst = self._configure(configure_source_v, {
            'vsource_voltage': 0.1, 'vsource_current_compliance': 0.1,
            'vsource_current_range_auto': True,
        })
        try:
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
        from resistamet_gui.session.configure import configure_source_v
        inst = self._configure(configure_source_v, {
            'vsource_voltage': 0.5, 'vsource_current_compliance': 1e-3,
            'vsource_current_range_auto': False,
        })
        try:
            inst.write(":OUTP ON")
            response = inst.query(":READ?")
            parts = response.split(",")
            stat = int(float(parts[2]))
            assert stat & (1 << 3), f"compliance bit not set: STAT={stat}"
        finally:
            inst.close()

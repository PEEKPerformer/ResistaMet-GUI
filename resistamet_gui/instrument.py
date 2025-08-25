import time
from typing import Optional

import pyvisa


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
        try:
            if self.dev:
                self.dev.close()
        finally:
            self.dev = None
            if self.rm:
                try:
                    self.rm.close()
                finally:
                    self.rm = None


class Keithley2400(VisaInstrument):
    def enable_autozero(self, on: bool = True):
        self.write(f":SYST:AZER:STAT {'ON' if on else 'OFF'}")

    def set_4wire(self, on: bool):
        self.write(":SYST:RSEN ON" if on else ":SYST:RSEN OFF")

    def setup_resistance(self, test_current: float, v_comp: float, nplc: float, auto_range: bool, four_wire: bool):
        self.set_4wire(four_wire)
        self.write(":SENS:FUNC 'RES'")
        self.write(":SOUR:FUNC CURR")
        self.write(":SOUR:CURR:MODE FIX")
        self.write(f":SOUR:CURR:RANG {abs(test_current)}")
        self.write(f":SOUR:CURR {test_current}")
        self.write(f":SENS:VOLT:PROT {v_comp}")
        self.write(":SENS:RES:MODE AUTO" if auto_range else ":SENS:RES:MODE MAN")
        if not auto_range:
            rmax = v_comp / abs(test_current) if abs(test_current) > 0 else 210e6
            self.write(f":SENS:RES:RANG {rmax}")
        self.write(f":SENS:RES:NPLC {nplc}")
        self.write(":FORM:ELEM RES")

    def setup_source_voltage(self, voltage: float, i_comp: float, nplc: float, auto_range_curr: bool):
        self.set_4wire(False)
        self.write(":SENS:FUNC 'CURR:DC'")
        self.write(":SOUR:FUNC VOLT")
        self.write(":SOUR:VOLT:MODE FIX")
        self.write(f":SOUR:VOLT:RANG {abs(voltage)}")
        self.write(f":SOUR:VOLT {voltage}")
        self.write(f":SENS:CURR:PROT {i_comp}")
        self.write(":SENS:CURR:RANG:AUTO ON" if auto_range_curr else ":SENS:CURR:RANG:AUTO OFF")
        if not auto_range_curr:
            self.write(f":SENS:CURR:RANG {i_comp}")
        self.write(f":SENS:CURR:NPLC {nplc}")
        self.write(":FORM:ELEM CURR,VOLT")
        self.write(":TRIG:COUN 1"); self.write(":INIT:CONT ON")

    def setup_source_current(self, current: float, v_comp: float, nplc: float, auto_range_volt: bool):
        self.set_4wire(False)
        self.write(":SENS:FUNC 'VOLT:DC'")
        self.write(":SOUR:FUNC CURR")
        self.write(":SOUR:CURR:MODE FIX")
        self.write(f":SOUR:CURR:RANG {abs(current)}")
        self.write(f":SOUR:CURR {current}")
        self.write(f":SENS:VOLT:PROT {v_comp}")
        self.write(":SENS:VOLT:RANG:AUTO ON" if auto_range_volt else ":SENS:VOLT:RANG:AUTO OFF")
        if not auto_range_volt:
            self.write(f":SENS:VOLT:RANG {v_comp}")
        self.write(f":SENS:VOLT:NPLC {nplc}")
        self.write(":FORM:ELEM VOLT,CURR")
        self.write(":TRIG:COUN 1"); self.write(":INIT:CONT ON")

    def common_fast(self):
        self.write(":TRIG:DEL 0"); self.write(":SOUR:DEL 0")


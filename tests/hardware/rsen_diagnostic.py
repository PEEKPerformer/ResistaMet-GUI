"""Hardware diagnostic for the 4-wire (RSEN) wiring of 4-point probe mode.

Run this against the lab's Keithley + Signatone S-302 setup to confirm
that 4PP is actually doing a 4-wire measurement (V sensed on inner pins)
rather than a 2-wire measurement (V sensed on Force / outer pins).

Test setup required:
- Any conductive sample on the chuck (copper plate works well precisely
  because its true sheet resistance is sub-uV — magnifying the 2-wire bug).
- A ~100 Ohm resistor in series with the I/O HI (Pin 1 / Force HI / outer
  current) lead on the S-302 back panel. This perturbs the source-loop
  resistance without touching the sense loop.

What the script does:
- Configures the 2400-series instrument the same way workers.py does for
  4PP mode, runs once with :SYST:RSEN OFF and once with :SYST:RSEN ON,
  and prints V/I for each. The diagnostic delta should be:
    * RSEN OFF (incorrect) : V dominated by source-loop resistance
      (the 100 Ohm + probe contact resistance into the sample).
    * RSEN ON (correct)    : V at the sense terminals, decoupled from
      source loop. For copper-like samples this is sub-uV.

Historical: prior to 2026-05, workers.py wrote :SYST:RSEN OFF on 4PP
setup (see commit fixing workers.py:281). On the original hardware run
with this script that day, V(OFF) = ~3200 mV vs V(ON) = ~-3 uV — a
~1e6 ratio that established the bug was a 2-wire-disguised-as-4-wire
miswiring, and that the fix is correct.

Usage:
    python tests/hardware/rsen_diagnostic.py

This script is not auto-discovered by pytest; it's a one-shot bench
verification kept here so any future RSEN regression can be re-confirmed
in minutes on the actual hardware.
"""

import time
import pyvisa


def configure_and_read(k, rsen_on: bool, source_current=1e-3, v_comp=5.0,
                       n_samples=8, nplc=5.0):
    """Configure 4PP-style measurement, take n_samples readings, return list."""
    k.write("*RST")
    time.sleep(0.3)
    k.write("*CLS")
    # Mirror workers.py 4PP setup sequence
    k.write(":SENS:FUNC:CONC OFF")
    k.write(f":SYST:RSEN {'ON' if rsen_on else 'OFF'}")
    k.write(":SENS:FUNC 'VOLT:DC'")
    k.write(":SOUR:FUNC CURR")
    k.write(f":SOUR:CURR:RANG {abs(source_current)}")
    k.write(f":SOUR:CURR {source_current}")
    k.write(f":SENS:VOLT:PROT {v_comp}")
    k.write(":SENS:VOLT:RANG:AUTO ON")
    k.write(f":SENS:VOLT:NPLC {nplc}")
    k.write(":FORM:ELEM VOLT,CURR,STAT")
    k.write(":TRIG:DEL 0")
    k.write(":SOUR:DEL:AUTO ON")

    err = k.query(":SYST:ERR?").strip()
    print(f"  post-config :SYST:ERR? -> {err}")

    k.write(":OUTP ON")
    time.sleep(0.3)  # settling

    readings = []
    for _ in range(n_samples):
        raw = k.query(":READ?").strip()
        parts = [p.strip() for p in raw.split(',')]
        try:
            v = float(parts[0])
            i = float(parts[1])
            stat = int(float(parts[-1]))
        except (ValueError, IndexError):
            v, i, stat = float('nan'), float('nan'), 0
        readings.append((v, i, stat))

    k.write(":OUTP OFF")
    return readings


def summarize(label, readings):
    vs = [v for v, _, _ in readings]
    is_ = [i for _, i, _ in readings]
    v_mean = sum(vs) / len(vs)
    i_mean = sum(is_) / len(is_)
    v_sd = (sum((v - v_mean) ** 2 for v in vs) / len(vs)) ** 0.5
    print(f"  {label}: V mean = {v_mean*1e3:+.5f} mV  (sd {v_sd*1e3:.5f} mV)   "
          f"I mean = {i_mean*1e3:+.5f} mA   n = {len(readings)}")
    return v_mean


def main():
    rm = pyvisa.ResourceManager()
    k = rm.open_resource("GPIB0::24::INSTR")
    k.timeout = 8000

    print(f"Instrument: {k.query('*IDN?').strip()}")
    print()

    try:
        print("=== Case 1: RSEN OFF (the pre-fix state) ===")
        off_readings = configure_and_read(k, rsen_on=False)
        for v, i, stat in off_readings:
            print(f"  V = {v*1e3:+.5f} mV   I = {i*1e3:+.5f} mA   stat = 0x{stat:x}")
        v_off = summarize("RSEN OFF", off_readings)

        print()
        time.sleep(1.0)

        print("=== Case 2: RSEN ON (the fixed state) ===")
        on_readings = configure_and_read(k, rsen_on=True)
        for v, i, stat in on_readings:
            print(f"  V = {v*1e3:+.5f} mV   I = {i*1e3:+.5f} mA   stat = 0x{stat:x}")
        v_on = summarize("RSEN ON", on_readings)

        print()
        print("=== Diagnostic ===")
        delta = v_off - v_on
        print(f"  V(OFF) - V(ON) = {delta*1e3:+.5f} mV")
        print(f"  Expected if bug is real: ~ +100 mV (1 mA * 100 Ohm series in HI lead)")
        if abs(delta) > 50e-3:
            print(f"  RESULT: RSEN was indeed routing V to the Force terminals.")
            print(f"          The fix (workers.py :SYST:RSEN ON for 4PP) is correct.")
        else:
            print(f"  RESULT: No significant difference observed.")
            print(f"          Check: resistor really on I/O HI (Pin 1)? probe contact?")

    finally:
        try:
            k.write(":OUTP OFF")
        except Exception:
            pass
        k.close()


if __name__ == "__main__":
    main()

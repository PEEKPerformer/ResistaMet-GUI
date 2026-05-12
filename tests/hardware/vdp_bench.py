"""SSH-friendly bench-test driver for the van der Pauw mode (ASTM F76 Method A).

Mirrors the VdpMeasurementWorker SCPI sequence but as a stdin-driven CLI
so it runs cleanly over SSH on the lab Windows box -- no PyQt5 / GUI
required.

Workflow:
  1. Connect to the Keithley + configure 4-wire vdP source/measure.
  2. For each of F76's 4 geometries: print the lead-routing instructions,
     wait for the user to wire alligator clips and press Enter, then take
     +I and -I readings (current reversal embedded per F76 sec. 11.1).
  3. After 4 geometries (8 voltages), compute rho_A, rho_B, rho_avg, R_s,
     Q_A/Q_B, f_A/f_B, asymmetry %, homogeneity flag and print them.

Setup required:
  - Any uniform, hole-free conductive sample with 4 corner contacts
    (copper plate + 4 alligator clips works; same plate as the RSEN
    bench test).
  - Mark / remember the contact numbers 1-4 counter-clockwise around the
    periphery (matters because F76's voltage labels are tied to them).
  - Banana-to-alligator cables from Force HI / Force LO / Sense HI /
    Sense LO on the Keithley to 4 clips. Don't re-shuffle which clip
    goes to which terminal between geometries -- only the corner each
    clip attaches to changes.

Usage:
  python tests/hardware/vdp_bench.py
  python tests/hardware/vdp_bench.py --current 10e-3 --thickness 0.005

This script is not auto-discovered by pytest; it's a one-shot bench
verification kept alongside rsen_diagnostic.py.
"""
from __future__ import annotations

import argparse
import math
import sys
import time

import pyvisa

# Import F76 math + protocol from the package so the bench script and the
# in-app worker stay in lock-step.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
from resistamet_gui.calculations_vdp import (  # noqa: E402
    F76_HOMOGENEITY_TOLERANCE_PCT,
    calculate_van_der_pauw,
    f76_geometries,
)


_STAT_BIT_COMPLIANCE = 1 << 3


def configure_vdp(k, source_current_a: float, v_compliance_v: float, nplc: float):
    """Identical setup to VdpMeasurementWorker._connect_and_configure."""
    k.write("*RST"); time.sleep(0.5)
    k.write("*CLS")
    k.write(":SYST:AZER:STAT ON")
    k.write(":SENS:FUNC:CONC OFF")
    k.write(":OUTP:SMOD HIMP")
    # F76 sec. 7.3.2: voltmeter must use sense terminals (high-Z) so it
    # doesn't perturb the source loop.
    k.write(":SYST:RSEN ON")
    k.write(":SENS:FUNC 'VOLT:DC'")
    k.write(":SOUR:FUNC CURR")
    k.write(f":SOUR:CURR:RANG {abs(source_current_a)}")
    k.write(f":SOUR:CURR {source_current_a}")
    k.write(f":SENS:VOLT:PROT {v_compliance_v}")
    k.write(":SENS:VOLT:RANG:AUTO ON")
    k.write(f":SENS:VOLT:NPLC {nplc}")
    k.write(":FORM:ELEM VOLT,CURR,STAT")
    k.write(":TRIG:DEL 0")
    k.write(":SOUR:DEL:AUTO ON")


def read_v(k, n: int = 1):
    """Average N :READ? voltages; return (V_mean, OR of STAT bits)."""
    v_sum = 0.0
    stat_or = 0
    for _ in range(n):
        raw = k.query(":READ?").strip()
        parts = [p.strip() for p in raw.split(',')]
        v_sum += float(parts[0])
        stat_or |= int(float(parts[-1]))
    return v_sum / n, stat_or


def measure_one_geometry(k, geom, i_mag: float, settling_s: float,
                          n_avg: int) -> tuple:
    """Source +I, settle, read; source -I, settle, read. Return (v_pos, v_neg, stat)."""
    k.write(":OUTP ON")
    k.write(f":SOUR:CURR {i_mag}")
    time.sleep(settling_s)
    v_pos, stat_pos = read_v(k, n_avg)

    k.write(f":SOUR:CURR {-i_mag}")
    time.sleep(settling_s)
    v_neg, stat_neg = read_v(k, n_avg)

    # Restore +I and disable output so leads can be safely reconnected.
    k.write(f":SOUR:CURR {i_mag}")
    k.write(":OUTP OFF")
    return v_pos, v_neg, (stat_pos | stat_neg)


def prompt(msg: str) -> None:
    """Wait for the user to press Enter. Skip on non-interactive runs."""
    if not sys.stdin.isatty():
        print(f"(non-tty: skipping prompt) {msg}")
        return
    input(msg)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Bench-test the vdP measurement mode against a real instrument."
    )
    p.add_argument("--gpib", default="GPIB0::24::INSTR",
                   help="VISA resource string for the instrument.")
    p.add_argument("--current", type=float, default=1.0e-3,
                   help="Source current magnitude in A (default: 1 mA).")
    p.add_argument("--compliance", type=float, default=5.0,
                   help="Voltage compliance in V (default: 5 V).")
    p.add_argument("--thickness", type=float, default=1.0e-4,
                   help="Sample thickness in cm (default: 1e-4 cm = 1 um).")
    p.add_argument("--nplc", type=float, default=5.0,
                   help="Integration time in power-line cycles (default: 5).")
    p.add_argument("--settling", type=float, default=0.2,
                   help="Settling time after each polarity flip in s (default: 0.2).")
    p.add_argument("--avg", type=int, default=3,
                   help="Readings to average per polarity (default: 3).")
    p.add_argument("--auto", action="store_true",
                   help="Skip all 'press Enter' prompts (for non-interactive tests).")
    args = p.parse_args()

    i_mag = abs(args.current)
    print(f"=== van der Pauw bench test (ASTM F76 Method A) ===")
    print(f"Instrument:  {args.gpib}")
    print(f"Source I:    {i_mag*1e3:.3g} mA")
    print(f"V comp:      {args.compliance} V")
    print(f"Thickness:   {args.thickness} cm")
    print(f"NPLC:        {args.nplc}")
    print(f"Settling:    {args.settling} s")
    print(f"Avg / polarity: {args.avg}")
    print()
    print("Sample requirements (F76 sec. 9):")
    print("  - uniform, hole-free, homogeneous, isotropic specimen")
    print("  - 4 contacts at the periphery, numbered 1-4 counter-clockwise")
    print("  - contacts small relative to the periphery length")
    print()

    rm = pyvisa.ResourceManager()
    k = rm.open_resource(args.gpib)
    k.timeout = 10000
    print(f"Connected: {k.query('*IDN?').strip()}")
    print()

    try:
        configure_vdp(k, source_current_a=i_mag, v_compliance_v=args.compliance,
                       nplc=args.nplc)
        err = k.query(":SYST:ERR?").strip()
        print(f"post-config :SYST:ERR? -> {err}")
        print()

        voltages: dict = {}
        compliance_seen = False

        for idx, geom in enumerate(f76_geometries()):
            print(f"--- {geom.name}  (group {geom.group}) ---")
            print(f"  Force HI -> Contact {geom.source_high}")
            print(f"  Force LO -> Contact {geom.source_low}")
            print(f"  Sense HI -> Contact {geom.sense_high}")
            print(f"  Sense LO -> Contact {geom.sense_low}")
            print(f"  Will produce:  {geom.label_pos} (at +I), "
                  f"{geom.label_neg} (at -I)")
            if not args.auto:
                prompt("  Wire the leads and press Enter to measure... ")
            v_pos, v_neg, stat = measure_one_geometry(
                k, geom, i_mag, args.settling, args.avg
            )
            if stat & _STAT_BIT_COMPLIANCE:
                compliance_seen = True
                print("  *** COMPLIANCE BIT SET on at least one reading ***")
            voltages[geom.label_pos] = v_pos
            voltages[geom.label_neg] = v_neg
            print(f"  {geom.label_pos:>9s} = {v_pos*1e3:+.6f} mV    "
                  f"{geom.label_neg:>9s} = {v_neg*1e3:+.6f} mV    "
                  f"delta = {(v_pos - v_neg)*1e3:+.6f} mV")
            print()

        print("=== Result ===")
        result = calculate_van_der_pauw(voltages, i_mag, args.thickness)
        print(f"  rho_A      = {result.rho_a:.6g} Ohm.cm")
        print(f"  rho_B      = {result.rho_b:.6g} Ohm.cm")
        print(f"  rho_avg    = {result.rho_avg:.6g} Ohm.cm")
        print(f"  R_s        = {result.sheet_resistance:.6g} Ohm/sq")
        print(f"  Q_A / Q_B  = {result.q_a:.4f}  /  {result.q_b:.4f}")
        print(f"  f_A / f_B  = {result.f_a:.4f}  /  {result.f_b:.4f}")
        print(f"  asymmetry  = {result.asymmetry_pct:.3f} %")
        print()
        if result.homogeneous:
            print(f"  HOMOGENEOUS  (<= {F76_HOMOGENEITY_TOLERANCE_PCT}% per F76 sec. 11.1)")
        else:
            print(f"  NON-HOMOGENEOUS  (> {F76_HOMOGENEITY_TOLERANCE_PCT}% per F76 sec. 11.1)")
            print(f"  Possible causes: sample inhomogeneity, contact resistance,")
            print(f"  poor contact placement (F76 sec. 9.4 wants contacts <0.05*L_p),")
            print(f"  or noise dominating one of the geometries (try higher I, more avg).")
        if compliance_seen:
            print()
            print("  WARNING: voltage compliance was hit on at least one reading.")
            print("  Raise --compliance or lower --current.")
        return 0
    finally:
        try:
            k.write(":OUTP OFF")
        except Exception:
            pass
        k.close()


if __name__ == "__main__":
    sys.exit(main())

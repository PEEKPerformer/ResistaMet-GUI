"""Per-mode instrument configuration, and the state each mode decides.

Split out of ``workers.py`` unchanged. Each function drives the SCPI setup for
one mode and returns its frozen state plus the metadata, CSV headers and
filename fragment the run needs. They take the instrument and an outputs facade
explicitly, so nothing here needs a worker.

The SCPI order in these functions is bench-verified; see ``instrument.py`` for
the 2400-series quirks it works around.
"""
from dataclasses import dataclass
from typing import Optional

import pyvisa

from ..formatting import format_power


@dataclass(frozen=True)
class ResistanceState:
    """What the resistance configure step decided, read back by the loop.

    ``voltage_compliance_v`` is the limit the instrument reports after
    configuration, not the one requested. In manual range that is the limit
    in force for the whole run, and the loop detects compliance against it,
    because the ohms function never sets the compliance bit in the status
    word (Keithley 2400 and 2420, bench 2026-09-18).

    With ``auto_range`` on it is only the limit at configure time. Auto-ohms
    sets its own, and moves it with the ohms range: 2.1 V was read back on
    the reset range, while the top ranges measure up to 20 V (user's manual,
    Table 4-1). The loop must not compare samples against it.
    """

    cable_null: float = 0.0
    voltage_compliance_v: float = float('inf')
    auto_range: bool = False


#: The highest source voltage in the 2400 family (the 2410), for bounding a
#: read-back when the model is not known.
_FAMILY_MAX_SOURCE_V = 1100.0


def _read_back_voltage_limit(keithley, events, max_source_v: Optional[float]) -> Optional[float]:
    """Ask the instrument what its voltage limit became. None if it will not say.

    None means nothing is recorded as reported: the caller must not put the
    request in its place, because the request is exactly the number auto-ohms
    is known to override. Only the instrument not answering, or answering
    something that is not a number, is handled here; anything else is a bug
    and is left to fail the configure step.

    A reply outside ``(0, max_source_v * 1.05]`` is not a limit either. The
    overflow value 9.91e37 is finite and positive, and as a limit it would
    mean no sample could ever be flagged.
    """
    try:
        value = float(keithley.query(":SENS:VOLT:PROT?").strip())
    except (pyvisa.errors.VisaIOError, ValueError) as exc:
        events.warn('limit_readback_failed',
                    f"Warning: Could not read back the voltage limit ({exc}). "
                    f"The file will not record an effective limit.")
        return None
    ceiling = (max_source_v or _FAMILY_MAX_SOURCE_V) * 1.05
    if not 0.0 < value <= ceiling:
        events.warn('limit_readback_rejected',
                    f"Warning: The instrument reported a voltage limit of {value:g} V, "
                    f"which it cannot have. The file will not record an effective limit.")
        return None
    return value


@dataclass(frozen=True)
class FourPointState:
    """What the 4PP configure step decided: delta mode and the power envelope."""

    source_current: float
    delta_mode: bool
    delta_settling: float
    power_warn_w: float
    power_stop_w: float
    stop_on_overpower: bool


@dataclass(frozen=True)
class SweepState:
    """What the sweep configure step handed to the instrument's sweep engine."""

    points: int
    source: str
    direction: str
    up_down: bool


def configure_resistance(keithley, events, measurement_settings, nplc, max_source_v=None):
    """Source I, measure R. 2-wire or 4-wire, optional offset compensation.

    ``max_source_v`` is the connected model's, when known; it bounds what is
    accepted as a read-back of the voltage limit.

    Returns (state, metadata, csv_headers, source_value_str).
    """
    test_current = measurement_settings['res_test_current']
    voltage_compliance = measurement_settings['res_voltage_compliance']
    measurement_type = measurement_settings['res_measurement_type']
    auto_range = measurement_settings['res_auto_range']

    keithley.write(":SYST:RSEN ON" if measurement_type == "4-wire" else ":SYST:RSEN OFF")
    keithley.write(":SENS:FUNC 'RES'")
    # Disable auto-ohms before configuring source/compliance
    # (auto-ohms is ON by default after selecting RES function
    # and rejects :SOUR:CURR:RANG, :SOUR:CURR, :SENS:VOLT:PROT)
    keithley.write(":SENS:RES:MODE MAN")
    keithley.write(":SOUR:FUNC CURR")
    keithley.write(f":SOUR:CURR:RANG {abs(test_current)}")
    keithley.write(f":SOUR:CURR {test_current}")
    keithley.write(f":SENS:VOLT:PROT {voltage_compliance}")
    keithley.write(f":SENS:RES:NPLC {nplc}")
    if auto_range:
        keithley.write(":SENS:RES:MODE AUTO")
    else:
        max_r = voltage_compliance / abs(test_current) if abs(test_current) > 0 else 210e6
        keithley.write(f":SENS:RES:RANG {max_r}")
    # Offset-compensated ohms: cancels thermoelectric EMF
    if measurement_settings.get('res_offset_comp', False):
        keithley.write(":SENS:RES:OCOM ON")
    # The limit the instrument has now. Auto-ohms overrides the requested
    # compliance (2.1 V seen on a 2420 that was asked for 0.5 V), and the
    # status word does not report compliance in the ohms function, so in
    # manual range the loop compares readings against this number. In auto
    # range it is recorded and not compared: see ResistanceState.
    effective_compliance = _read_back_voltage_limit(keithley, events, max_source_v)
    # Cable null: software subtraction (2400 series lacks :SENS:RES:REL)
    state = ResistanceState(
        cable_null=float(measurement_settings.get('res_cable_null', 0.0)),
        # No read-back leaves the state's default, which is not finite: the
        # header then omits the key, and in manual range the parser falls
        # back to the requested limit for flagging.
        voltage_compliance_v=(float('inf') if effective_compliance is None
                              else effective_compliance),
        auto_range=bool(auto_range))
    # Pull raw V and I alongside R so accuracy.py can propagate
    # the per-range V and I uncertainties into σ_R. The 2400's
    # ohms function senses V and I internally regardless of
    # FORM:ELEM, so listing VOLT,CURR,RES,STAT just enables
    # them to be returned. Fixed-order output: VOLT, CURR,
    # RES, TIME, STAT (regardless of argument order).
    keithley.write(":FORM:ELEM VOLT,CURR,RES,STAT")

    metadata = {
        'Mode': 'Resistance Measurement',
        'Test Current (A)': test_current,
        'Voltage Compliance (V)': voltage_compliance,
        # What the instrument will actually enforce. With auto range on this
        # differs from the request, and auto-ohms also chooses the test
        # current per range, so the Current column is the record of that.
        'Effective Voltage Compliance (V)': effective_compliance,
        'Measurement Type': measurement_type,
        'Resistance Auto Range': 'ON' if auto_range else 'OFF',
    }
    csv_headers = ['Timestamp (Unix)', 'Elapsed Time (s)', 'Voltage (V)', 'Current (A)', 'Resistance (Ohms)', 'Compliance Status', 'Event']
    source_value_str = f"{test_current*1000:.2f}mA"

    return state, metadata, csv_headers, source_value_str

def configure_source_v(keithley, events, measurement_settings, nplc):
    """Source V, measure I.

    Returns (state, metadata, csv_headers, source_value_str); this mode
    keeps no per-run state beyond the settings dict.
    """
    state = None
    source_voltage = measurement_settings['vsource_voltage']
    current_compliance = measurement_settings['vsource_current_compliance']
    auto_range_curr = measurement_settings['vsource_current_range_auto']

    keithley.write(":SYST:RSEN OFF")
    keithley.write(":SENS:FUNC 'CURR:DC'")
    keithley.write(":SOUR:FUNC VOLT")
    keithley.write(f":SOUR:VOLT:RANG {abs(source_voltage)}")
    keithley.write(f":SOUR:VOLT {source_voltage}")
    keithley.write(f":SENS:CURR:PROT {current_compliance}")
    keithley.write(":SENS:CURR:RANG:AUTO ON" if auto_range_curr else ":SENS:CURR:RANG:AUTO OFF")
    if not auto_range_curr:
        keithley.write(f":SENS:CURR:RANG {current_compliance}")
    keithley.write(f":SENS:CURR:NPLC {nplc}")
    # Keithley 2400 series always returns elements in fixed order:
    # VOLT, CURR, RES, TIME, STAT — regardless of FORM:ELEM argument order
    # Include STAT for hardware compliance detection (bit 3)
    keithley.write(":FORM:ELEM VOLT,CURR,STAT")

    metadata = {
        'Mode': 'Voltage Source',
        'Source Voltage (V)': source_voltage,
        'Current Compliance (A)': current_compliance,
        'Current Auto Range': 'ON' if auto_range_curr else 'OFF',
    }
    csv_headers = ['Timestamp (Unix)', 'Elapsed Time (s)', 'Voltage (V)', 'Current (A)', 'Resistance (Ohms)', 'Compliance Status', 'Event']
    source_value_str = f"{source_voltage:.3f}V"

    return state, metadata, csv_headers, source_value_str

def configure_source_i(keithley, events, measurement_settings, nplc):
    """Source I, measure V.

    Returns (state, metadata, csv_headers, source_value_str); this mode
    keeps no per-run state beyond the settings dict.
    """
    state = None
    source_current = measurement_settings['isource_current']
    voltage_compliance = measurement_settings['isource_voltage_compliance']
    auto_range_volt = measurement_settings['isource_voltage_range_auto']

    keithley.write(":SYST:RSEN OFF")
    keithley.write(":SENS:FUNC 'VOLT:DC'")
    keithley.write(":SOUR:FUNC CURR")
    keithley.write(f":SOUR:CURR:RANG {abs(source_current)}")
    keithley.write(f":SOUR:CURR {source_current}")
    keithley.write(f":SENS:VOLT:PROT {voltage_compliance}")
    keithley.write(":SENS:VOLT:RANG:AUTO ON" if auto_range_volt else ":SENS:VOLT:RANG:AUTO OFF")
    if not auto_range_volt:
        keithley.write(f":SENS:VOLT:RANG {voltage_compliance}")
    keithley.write(f":SENS:VOLT:NPLC {nplc}")
    keithley.write(":FORM:ELEM VOLT,CURR,STAT")

    metadata = {
        'Mode': 'Current Source',
        'Source Current (A)': source_current,
        'Voltage Compliance (V)': voltage_compliance,
        'Voltage Auto Range': 'ON' if auto_range_volt else 'OFF',
    }
    csv_headers = ['Timestamp (Unix)', 'Elapsed Time (s)', 'Voltage (V)', 'Current (A)', 'Resistance (Ohms)', 'Compliance Status', 'Event']
    source_value_str = f"{source_current*1000:.2f}mA"

    return state, metadata, csv_headers, source_value_str

def configure_four_point(keithley, events, measurement_settings, nplc):
    """Four-point probe. Returns None when the pre-flight refuses the power envelope.

    Returns (state, metadata, csv_headers, source_value_str).
    """
    # Use I-source and measure V (like source_i), but compute derived quantities for 4-pt probe
    source_current = measurement_settings.get('fpp_current')
    voltage_compliance = measurement_settings.get('fpp_voltage_compliance')
    auto_range_volt = measurement_settings.get('fpp_voltage_range_auto')

    # 4-wire (remote sense) is REQUIRED for a real 4-point probe measurement.
    # The probe head wires outer tips to Force HI/LO and inner tips to Sense
    # HI/LO (Signatone S-302 manual, page 6). With RSEN OFF the voltmeter
    # routes back to the Force terminals, measuring across the current-
    # carrying outer pair — i.e. a 2-wire measurement that includes contact
    # and spreading resistance.
    keithley.write(":SYST:RSEN ON")
    keithley.write(":SENS:FUNC 'VOLT:DC'")
    keithley.write(":SOUR:FUNC CURR")
    keithley.write(f":SOUR:CURR:RANG {abs(source_current)}")
    keithley.write(f":SOUR:CURR {source_current}")
    keithley.write(f":SENS:VOLT:PROT {voltage_compliance}")
    keithley.write(":SENS:VOLT:RANG:AUTO ON" if auto_range_volt else ":SENS:VOLT:RANG:AUTO OFF")
    if not auto_range_volt:
        keithley.write(f":SENS:VOLT:RANG {voltage_compliance}")
    keithley.write(f":SENS:VOLT:NPLC {nplc}")
    keithley.write(":FORM:ELEM VOLT,CURR,STAT")

    # Delta mode and the probe-safety thresholds. The pre-flight check
    # below uses the configured worst-case I*V_compliance; the runtime
    # monitor uses measured V*I per sample.
    state = FourPointState(
        source_current=source_current,
        delta_mode=bool(measurement_settings.get('fpp_delta_mode', False)),
        delta_settling=float(measurement_settings.get('fpp_delta_settling', 0.1)),
        power_warn_w=float(measurement_settings.get('fpp_power_warn_w', 1.0e-2)),
        power_stop_w=float(measurement_settings.get('fpp_power_stop_w', 1.0e-1)),
        stop_on_overpower=bool(measurement_settings.get('fpp_stop_on_overpower', True)),
    )

    # Pre-flight power envelope check: worst case is the user
    # asking for the full source current at the full compliance
    # voltage, i.e. probe sees I_source * V_compliance.
    worst_case_power = abs(source_current) * abs(voltage_compliance)
    if worst_case_power > state.power_stop_w:
        events.error('power_envelope', 'run',
            f"Configured 4PP power ({format_power(worst_case_power)} = "
            f"{abs(source_current)*1e3:.3g} mA × {abs(voltage_compliance):.3g} V) "
            f"exceeds the probe-safety hard stop "
            f"({format_power(state.power_stop_w)}). Lower the source "
            f"current or the voltage compliance, or raise fpp_power_stop_w "
            f"in settings if you've reviewed the probe spec."
        )
        return
    if worst_case_power > state.power_warn_w:
        events.warn('power_envelope',
            f"Warning: 4PP power envelope: up to {format_power(worst_case_power)} "
            f"(I × V_comp). Above warning threshold "
            f"{format_power(state.power_warn_w)} — proceed with care."
        )

    metadata = {
        'Mode': 'Four-Point Probe',
        'Source Current (A)': source_current,
        'Voltage Compliance (V)': voltage_compliance,
        'Spacing s (cm)': measurement_settings.get('fpp_spacing_cm'),
        'Thickness t (µm)': measurement_settings.get('fpp_thickness_um'),
        'Alpha': measurement_settings.get('fpp_alpha'),
        'K Factor': measurement_settings.get('fpp_k_factor'),
        'Model': measurement_settings.get('fpp_model'),
        'Delta Mode': state.delta_mode,
    }
    csv_headers = ['Timestamp (Unix)', 'Elapsed Time (s)', 'Voltage (V)', 'Current (A)', 'V/I (Ohms)', 'Sheet Rs (Ohms/sq)', 'Resistivity (Ohm*cm)', 'Conductivity (S/cm)', 'Compliance Status', 'Event']
    source_value_str = f"{source_current*1000:.2f}mA"
    if state.delta_mode:
        source_value_str += "_delta"

    return state, metadata, csv_headers, source_value_str

def configure_sweep(keithley, events, measurement_settings, nplc):
    """Bulk linear sweep, set up on the instrument's own sweep engine.

    Returns (state, metadata, csv_headers, source_value_str).
    """
    sweep_source = measurement_settings.get('sweep_source', 'voltage')
    sweep_start = float(measurement_settings.get('sweep_start', 0.0))
    sweep_stop = float(measurement_settings.get('sweep_stop', 1.0))
    sweep_step = float(measurement_settings.get('sweep_step', 0.05))
    sweep_compliance = float(measurement_settings.get('sweep_compliance', 0.1))
    sweep_delay = float(measurement_settings.get('sweep_delay', 0.01))
    sweep_direction = measurement_settings.get('sweep_direction', 'up')

    src_func = 'VOLT' if sweep_source == 'voltage' else 'CURR'
    # For down direction, swap start/stop
    if sweep_direction == 'down':
        sweep_start, sweep_stop = sweep_stop, sweep_start

    points = keithley.setup_sweep(
        src_func, sweep_start, sweep_stop, sweep_step,
        sweep_compliance, nplc, sweep_delay
    )
    # For up_down: double the points (forward + reverse)
    if sweep_direction == 'up_down':
        keithley.write(":SOUR:SWE:DIR UP")
        # We'll do two separate sweeps
        up_down = True
    else:
        up_down = False
    state = SweepState(points=points, source=src_func,
                        direction=sweep_direction, up_down=up_down)

    metadata = {
        'Mode': 'I-V Sweep',
        'Source Function': sweep_source,
        'Start': sweep_start,
        'Stop': sweep_stop,
        'Step': sweep_step,
        'Compliance': sweep_compliance,
        'Delay (s)': sweep_delay,
        'Direction': sweep_direction,
        'Points': state.points,
    }
    csv_headers = ['Point', 'Voltage (V)', 'Current (A)', 'Compliance Status']
    source_value_str = f"sweep_{sweep_start}to{sweep_stop}"

    return state, metadata, csv_headers, source_value_str

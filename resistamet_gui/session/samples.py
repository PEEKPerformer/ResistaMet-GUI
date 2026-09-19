"""Turning one instrument reading into a data dict and a CSV row.

Split out of ``workers.py`` unchanged. Called once per sample by the
acquisition loop: parse the reading for this mode, then build the row in the
column order ``data_export.get_column_config`` declares.
"""
from typing import Optional

import numpy as np

from ..accuracy import (
    current_source_uncertainty, current_uncertainty,
    resistance_uncertainty, voltage_source_uncertainty, voltage_uncertainty,
)
from ..constants import KEITHLEY_COMPLIANCE_MAGIC_NUMBER
from ..data_export import splice_before_tail

# Keithley 2400 series STATUS word bit masks (24-bit)
# Bit 3: Compliance — source is in real compliance
_STAT_BIT_COMPLIANCE = 1 << 3


def _is_overflow(reading) -> bool:
    """True for the 2400's overflow value (+9.9E37; 9.91E37 on some paths)."""
    return bool(np.isfinite(reading) and abs(reading) >= KEITHLEY_COMPLIANCE_MAGIC_NUMBER)


def parse_resistance(parts, stat_word, hw_compliance, measurement_settings, nplc,
                  model_name, mode_state, events, reading_str=''):
    """Parse a resistance reading: V, I, R, plus sigma_R and the cable null.

    Returns (data_dict, compliance_status, compliance_type).
    """
    compliance_status = 'OK'
    compliance_type = None
    data_dict = {}
    # Fixed-order output: VOLT, CURR, RES, [TIME], STAT.
    # We requested VOLT,CURR,RES,STAT so parts is V, I, R, STAT.
    try:
        voltage = float(parts[0])
        current = float(parts[1]) if len(parts) > 1 else float('nan')
        value = float(parts[2]) if len(parts) > 2 else float('nan')
    except Exception:
        voltage = float('nan'); current = float('nan'); value = float('nan')
    compliance_type = 'Voltage'
    # The ohms function does not set the compliance bit (2400 and 2420,
    # bench 2026-09-18), and in manual range it reports the programmed
    # current, so V/I under compliance is a wrong number that looks fine.
    # Detect it the way the source modes do: the measured voltage sitting at
    # the limit the instrument actually has.
    #
    # That holds in manual range only, where the limit read back at configure
    # time is the limit for the whole run. Auto-ohms moves its own limit with
    # the ohms range (2.1 V on the reset range, 20 V full scale on the top
    # ones), so a healthy 1 MOhm DUT sits at 10 V and the frozen 2.1 V would
    # flag every row of it. In auto range the only signs trusted are the
    # status bit and the overflow value the instrument returns once the top
    # range is exceeded.
    if getattr(mode_state, 'auto_range', False):
        at_limit = _is_overflow(voltage) or _is_overflow(value)
    else:
        comp_limit_v = getattr(mode_state, 'voltage_compliance_v', float('inf'))
        if not np.isfinite(comp_limit_v) or comp_limit_v <= 0:
            comp_limit_v = float(measurement_settings.get('res_voltage_compliance', float('inf')) or float('inf'))
        at_limit = np.isfinite(voltage) and abs(voltage) >= comp_limit_v * 0.99
    if hw_compliance or at_limit:
        compliance_status = 'V_COMP'
    if not np.isfinite(value):
        value = float('nan')
        events.warn('invalid_reading', f"Invalid value detected ({reading_str})")
    # Apply software cable null if set (R only — V and I
    # are reported as-measured by the instrument).
    cable_null = mode_state.cable_null
    if cable_null != 0.0 and np.isfinite(value):
        value -= cable_null
    sigma_r = resistance_uncertainty(
        voltage, current, model=model_name, nplc=nplc,
        enhanced=bool(measurement_settings.get('res_offset_comp', False)),
    )
    data_dict = {
        'voltage': voltage, 'current': current, 'resistance': value,
        'resistance_unc': sigma_r,
    }

    return data_dict, compliance_status, compliance_type

def parse_source_v(parts, stat_word, hw_compliance, measurement_settings, nplc,
                  model_name, mode_state, out):
    """Parse a source-V reading: sourced V, measured I.

    Returns (data_dict, compliance_status, compliance_type).
    """
    compliance_status = 'OK'
    compliance_type = None
    data_dict = {}
    # Keithley 2400 series returns elements in fixed order:
    # VOLT, CURR, STAT. In source_v mode the VOLT
    # element echoes the source setpoint (V_set), so
    # it's the right input for the source-accuracy spec.
    try:
        voltage = float(parts[0])
        current = float(parts[1]) if len(parts) > 1 else float('nan')
    except Exception:
        voltage = float('nan'); current = float('nan')
    compliance_type = 'Current'
    comp_limit_i = measurement_settings.get('vsource_current_compliance')
    if hw_compliance or (np.isfinite(current) and abs(current) >= comp_limit_i * 0.99):
        compliance_status = 'I_COMP'
    sigma_v_src = voltage_source_uncertainty(voltage, model=model_name)
    sigma_i_meas = current_uncertainty(current, model=model_name, nplc=nplc)
    # σ on R = V_set / I_meas: RSS of relative uncertainties.
    # NaN-safe — current_unc inherits NaN when current is 0
    # and we already gate that below.
    if np.isfinite(voltage) and np.isfinite(current) and current != 0 and voltage != 0:
        r_calc = voltage / current
        rel = (sigma_v_src / voltage) ** 2 + (sigma_i_meas / current) ** 2
        sigma_r = abs(r_calc) * np.sqrt(rel)
    else:
        sigma_r = float('nan')
    data_dict = {
        'current': current, 'voltage': voltage,
        'voltage_unc': sigma_v_src,
        'current_unc': sigma_i_meas,
        'resistance_unc': sigma_r,
    }

    return data_dict, compliance_status, compliance_type

def parse_source_i(parts, stat_word, hw_compliance, measurement_settings, nplc,
                  model_name, mode_state, out):
    """Parse a source-I reading: sourced I, measured V.

    Returns (data_dict, compliance_status, compliance_type).
    """
    compliance_status = 'OK'
    compliance_type = None
    data_dict = {}
    # CURR element echoes I_set in source_i mode; VOLT
    # is the sense reading.
    try:
        voltage = float(parts[0])
        current = float(parts[1]) if len(parts) > 1 else float('nan')
    except Exception:
        voltage = float('nan'); current = float('nan')
    compliance_type = 'Voltage'
    comp_limit_v = measurement_settings.get('isource_voltage_compliance')
    if hw_compliance or (np.isfinite(voltage) and abs(voltage) >= comp_limit_v * 0.99):
        compliance_status = 'V_COMP'
    sigma_v_meas = voltage_uncertainty(voltage, model=model_name, nplc=nplc)
    sigma_i_src = current_source_uncertainty(current, model=model_name)
    if np.isfinite(voltage) and np.isfinite(current) and current != 0 and voltage != 0:
        r_calc = voltage / current
        rel = (sigma_v_meas / voltage) ** 2 + (sigma_i_src / current) ** 2
        sigma_r = abs(r_calc) * np.sqrt(rel)
    else:
        sigma_r = float('nan')
    data_dict = {
        'voltage': voltage, 'current': current,
        'voltage_unc': sigma_v_meas,
        'current_unc': sigma_i_src,
        'resistance_unc': sigma_r,
    }

    return data_dict, compliance_status, compliance_type

def parse_four_point(parts, stat_word, hw_compliance, measurement_settings, nplc,
                  model_name, mode_state, out):
    """Parse a 4PP reading: measured V at the sourced I.

    Returns (data_dict, compliance_status, compliance_type).
    """
    compliance_status = 'OK'
    compliance_type = None
    data_dict = {}
    try:
        voltage = float(parts[0])
        current = float(parts[1]) if len(parts) > 1 else float('nan')
    except Exception:
        voltage = float('nan'); current = float('nan')
    compliance_type = 'Voltage'
    comp_limit_v = measurement_settings.get('fpp_voltage_compliance')
    if hw_compliance or (np.isfinite(voltage) and abs(voltage) >= comp_limit_v * 0.99):
        compliance_status = 'V_COMP'
    data_dict = {'voltage': voltage, 'current': current}

    return data_dict, compliance_status, compliance_type

def build_row(mode, elapsed_time, data_dict, compliance_status, event_marker,
               measurement_settings, nplc, use_delta, model_name, last_delta):
    """The CSV row for one sample, plus the values derived alongside it.

    Returns ``(row_data, derived)``. Column order matches
    get_column_config(); the aux splice happens at the call site, where the
    sensor state lives.

    ``derived`` is the 4PP sheet resistance, resistivity and conductivity the
    row already carries, named rather than positional, so a UI shows the same
    numbers the CSV holds instead of recomputing them from the raw dict and
    drifting. Empty for the other modes, which derive nothing.
    """
    row_data = []
    derived = {}
    # Build row data with raw values (exporter handles formatting)
    if mode == 'resistance':
        v = data_dict.get('voltage', float('nan'))
        i = data_dict.get('current', float('nan'))
        r = data_dict.get('resistance', float('nan'))
        r_unc = data_dict.get('resistance_unc', float('nan'))
        row_data = [elapsed_time, v, i, r, r_unc, compliance_status, event_marker]
    elif mode == 'four_point':
        v = data_dict.get('voltage', float('nan'))
        i = data_dict.get('current', float('nan'))

        # Decide F84-decomposed path vs legacy K*alpha path.
        # F84 is selected if the user supplied any F84-only
        # input (finite D, non-circle geometry, or a T+dopant
        # combo). Defaults keep the legacy path active so
        # existing config.json users see identical numbers.
        diameter_cm = float(measurement_settings.get('fpp_diameter_cm') or 0.0)
        geometry = str(measurement_settings.get('fpp_geometry') or 'circle')
        temp_raw = measurement_settings.get('fpp_temperature_c')
        try:
            temp_c_val: Optional[float] = float(temp_raw)
            if not np.isfinite(temp_c_val):
                temp_c_val = None
        except (TypeError, ValueError):
            temp_c_val = None
        dopant = str(measurement_settings.get('fpp_dopant_type') or 'none').lower()
        use_f84 = (diameter_cm > 0
                   or geometry != 'circle'
                   or (temp_c_val is not None and dopant in ('n', 'p')))

        # Common kwargs for the legacy path.
        fpp_kwargs = dict(
            spacing_cm=float(measurement_settings.get('fpp_spacing_cm') or 0.1016),
            thickness_um=float(measurement_settings.get('fpp_thickness_um') or 0.0),
            k_factor=float(measurement_settings.get('fpp_k_factor') or 4.532),
            alpha=float(measurement_settings.get('fpp_alpha') or 1.0),
            model=str(measurement_settings.get('fpp_model') or 'thin_film'),
        )

        if use_f84:
            # F84 path: F2·w·F(w/S)·F_sp [·F_T].
            from ..calculations import (
                calculate_four_point_probe_f84, calculate_conductivity,
                calculate_ratio, estimate_current_floor,
            )
            spacing_cm = fpp_kwargs['spacing_cm']
            thickness_um = fpp_kwargs['thickness_um']
            thickness_cm = thickness_um * 1e-4
            v_for_calc = v
            i_for_calc = i
            ratio_for_calc = calculate_ratio(v, i)
            if compliance_status != 'OK':
                src_i = float(measurement_settings.get('fpp_current') or 1e-3)
                v_comp = float(measurement_settings.get('fpp_voltage_compliance') or 5.0)
                i_floor = estimate_current_floor(src_i)
                i_eff = max(abs(i), i_floor) if np.isfinite(i) else i_floor
                ratio_for_calc = abs(v_comp) / i_eff if i_eff > 0 else float('nan')
                v_for_calc = abs(v_comp)
                i_for_calc = i_eff
            f84 = calculate_four_point_probe_f84(
                voltage=v_for_calc, current=i_for_calc,
                spacing_cm=spacing_cm, thickness_um=thickness_um,
                diameter_cm=diameter_cm if diameter_cm > 0 else None,
                geometry=geometry,
                temperature_c=temp_c_val,
                dopant_type=dopant if dopant in ('n', 'p') else None,
            )
            rs_val = (
                f84.rho_T / thickness_cm
                if (thickness_cm > 0 and np.isfinite(f84.rho_T))
                else float('nan')
            )
            # Use rho_23 when available; otherwise rho_T.
            rho_report = f84.rho_23 if f84.rho_23 is not None else f84.rho_T
            sigma = calculate_conductivity(rho_report)
            v_sigma = voltage_uncertainty(v, model=model_name, nplc=nplc)
            i_sigma = current_uncertainty(i, model=model_name, nplc=nplc)
            row_data = [
                elapsed_time, v, i,
                ratio_for_calc, rs_val,
                rho_report, sigma,
                v_sigma, i_sigma,
                compliance_status, event_marker
            ]
            derived = {
                'ratio': ratio_for_calc, 'rs': rs_val, 'rho': rho_report,
                'sigma': sigma, 'v_unc': v_sigma, 'i_unc': i_sigma,
                'method': 'f84',
            }
        else:
            # Legacy path: K * alpha * t * (V/I).
            if compliance_status != 'OK':
                from ..calculations import calculate_four_point_probe_bound
                result = calculate_four_point_probe_bound(
                    v_compliance=float(measurement_settings.get('fpp_voltage_compliance') or 5.0),
                    measured_current=i,
                    source_current=float(measurement_settings.get('fpp_current') or 1e-3),
                    **fpp_kwargs,
                )
            else:
                from ..calculations import calculate_four_point_probe
                result = calculate_four_point_probe(
                    voltage=v, current=i, **fpp_kwargs,
                )
            v_sigma = voltage_uncertainty(v, model=model_name, nplc=nplc)
            i_sigma = current_uncertainty(i, model=model_name, nplc=nplc)
            row_data = [
                elapsed_time, v, i,
                result.ratio, result.sheet_resistance,
                result.resistivity, result.conductivity,
                v_sigma, i_sigma,
                compliance_status, event_marker
            ]
            derived = {
                'ratio': result.ratio, 'rs': result.sheet_resistance,
                'rho': result.resistivity, 'sigma': result.conductivity,
                'v_unc': v_sigma, 'i_unc': i_sigma,
                'method': 'legacy',
            }

        # Splice per-polarity columns when delta mode produced
        # the reading. splice_before_tail lands them just
        # before compliance/event, mirroring
        # get_column_config()'s 'compliance' anchor.
        if use_delta and last_delta:
            ld = last_delta
            row_data = splice_before_tail(
                row_data,
                [ld['v_plus'], ld['v_minus'], ld['r_f'], ld['r_r']],
            )
    else:
        # source_v or source_i
        v = data_dict.get('voltage', float('nan'))
        i = data_dict.get('current', float('nan'))
        r = (v / i) if (np.isfinite(v) and np.isfinite(i) and i != 0) else float('nan')
        r_unc = data_dict.get('resistance_unc', float('nan'))
        if mode == 'source_v':
            i_unc = data_dict.get('current_unc', float('nan'))
            row_data = [elapsed_time, v, i, r, i_unc, r_unc, compliance_status, event_marker]
        else:
            v_unc = data_dict.get('voltage_unc', float('nan'))
            row_data = [elapsed_time, v, i, r, v_unc, r_unc, compliance_status, event_marker]

    return row_data, derived

"""A run's data file as an agent should see it: statistics, not rows.

An agent's context must not fill with samples, and the event history sheds
them first on a long run, so summaries are computed from the file, which
holds every row and is the record a paper cites. ``summarise`` gives, per
numeric column, the count, mean, sample SD, min, max and last value; the
rows in compliance; the marks; and the file's header and end blocks.
``read_slice`` gives the header and a bounded slice of rows, never the
whole file.

Pure functions over the file's text; the header lines are read by
``data_export.metadata_from_lines``, as every other reader of the format
reads them. Statistics are over every row, compliance rows included: the
compliance count says how many those are, and a four-point file's own
``spot_stats`` (in ``end``) are the ones that leave them out.

``result`` is the run's headline, per mode, with a unit on every number:
the quantity it measured, from the rows not in compliance or from the
file's end block, a sweep's fitted resistance, and a line saying what the
uncertainties are.
"""
from __future__ import annotations

import csv
import math
import re
from typing import Any, Dict, List, Optional, Sequence

from ..constants import F76_HOMOGENEITY_TOLERANCE_PCT
from ..data_export import metadata_from_lines

#: Columns that hold words, not numbers.
TEXT_COLUMNS = ('compliance', 'event')
#: Compliance values that mean "within compliance".
NOT_IN_COMPLIANCE = ('', 'OK')

DEFAULT_ROWS = 50
MAX_ROWS = 500
MAX_MARKS = 50

#: Line ends as the exporter and ``parse_metadata`` know them; nothing else
#: (no form feed, no U+2028) ends a line of a data file.
_LINE_BREAKS = re.compile('\r\n|\r|\n')


class RunFile:
    """A data file cut into its parts: header, column row, data rows, end block."""

    def __init__(self, text: str):
        self.header: List[str] = []
        self.columns: List[str] = []
        self.rows: List[List[str]] = []
        self.end: List[str] = []
        data: List[str] = []
        for line in _LINE_BREAKS.split(text):
            if not line:
                continue
            if line.startswith('#'):
                (self.end if self.columns else self.header).append(line)
            elif not self.columns:
                self.columns = next(csv.reader([line]))
            elif not self.end:
                data.append(line)
        self.rows = [row for row in csv.reader(data)]
        self.metadata = _json_safe(metadata_from_lines(self.header))
        self.end_metadata = _json_safe(metadata_from_lines(self.end)) if self.end else None
        units = self.metadata.get('units')
        self.units: List[str] = units if isinstance(units, list) else []

    @property
    def finalized(self) -> bool:
        """Closed with its end block. An open run's file has none yet."""
        return bool(self.end)

    def column(self, name: str) -> List[str]:
        index = self.columns.index(name)
        return [row[index] if index < len(row) else '' for row in self.rows]

    def unit(self, name: str) -> Optional[str]:
        index = self.columns.index(name)
        return self.units[index] if index < len(self.units) and self.units[index] else None


def number(cell: str) -> Optional[float]:
    """A cell as a float, or None when it is not a number."""
    try:
        return float(cell)
    except ValueError:
        return None


def column_stats(cells: Sequence[str]) -> Optional[Dict[str, Any]]:
    """count, mean, sd, min, max and last of the finite numbers in a column.

    None when a non-empty cell is not a number (the column holds words).
    ``sd`` is the sample standard deviation (n - 1), None below two values.
    """
    values: List[float] = []
    for cell in cells:
        if cell == '':
            continue
        value = number(cell)
        if value is None:
            return None
        if math.isfinite(value):
            values.append(value)
    count = len(values)
    if count == 0:
        return {'count': 0, 'mean': None, 'sd': None, 'min': None, 'max': None, 'last': None}
    mean = sum(values) / count
    sd = (math.sqrt(sum((value - mean) ** 2 for value in values) / (count - 1))
          if count > 1 else None)
    return {'count': count, 'mean': mean, 'sd': sd, 'min': min(values),
            'max': max(values), 'last': values[-1]}


def summarise(text: str, first_rows: Optional[int] = None) -> Dict[str, Any]:
    """The numbers a run's file holds, without its rows.

    ``first_rows``: only the first N data rows count, for every statistic,
    the compliance rows and the marks alike. A mode with no sample count
    runs until it is stopped and always writes a few rows past the N that
    were wanted; this summarises the N. ``rows`` is then how many were
    summarised and ``rows_total`` how many the file holds.
    """
    run = RunFile(text)
    rows_total = len(run.rows)
    if first_rows is not None:
        run.rows = run.rows[:max(0, int(first_rows))]
    columns: Dict[str, Any] = {}
    for name in run.columns:
        if name in TEXT_COLUMNS:
            continue
        stats = column_stats(run.column(name))
        if stats is not None:
            columns[name] = {'unit': run.unit(name), **stats}
    summary: Dict[str, Any] = {
        'metadata': run.metadata,
        'end': run.end_metadata,
        'finalized': run.finalized,
        'rows': len(run.rows),
        'columns': columns,
    }
    if first_rows is not None:
        summary['rows_total'] = rows_total
        summary['first_rows'] = int(first_rows)
        summary['note'] = (f"statistics, compliance and marks over the first {len(run.rows)} "
                           f"of {rows_total} data rows")
    if 'compliance' in run.columns:
        kinds: Dict[str, int] = {}
        for value in run.column('compliance'):
            if value not in NOT_IN_COMPLIANCE:
                kinds[value] = kinds.get(value, 0) + 1
        summary['compliance'] = {'rows': sum(kinds.values()), 'kinds': kinds}
    if 'event' in run.columns:
        times = run.column('elapsed_s') if 'elapsed_s' in run.columns else None
        marks = [{'row': index + 1,
                  'elapsed_s': number(times[index]) if times else None,
                  'label': label}
                 for index, label in enumerate(run.column('event')) if label]
        summary['marks'] = marks[:MAX_MARKS]
        summary['marks_total'] = len(marks)
    result = result_of(run, whole_file=first_rows is None)
    if result is not None:
        summary['result'] = result
    return summary


# ---------------------------------------------------------------- results
#
# The headline of a run, with a unit on every number and a line saying what
# its uncertainties are. In the third trial an agent dug a van der Pauw
# result out of the events (bare sheet_resistance, rho_avg and
# sheet_resistance_uncertainty, no units) and fitted a sweep itself.
#
# What the uncertainties are follows accuracy.py, which takes the Keithley
# 1-year datasheet accuracy, +/-(% of reading + offset), as a "one-sigma
# equivalent": it is used as a standard uncertainty as printed, with no
# division by sqrt(3) and no coverage factor. The statistical parts are
# standard errors. Nothing reported is an expanded uncertainty.

#: The continuous modes: what their result is, as (name, column, the
#: column of its per-reading instrument uncertainty).
CONTINUOUS_RESULTS = {
    'resistance': (('R', 'R_ohm', 'R_unc_ohm'),),
    'source_v': (('I', 'I_meas', 'I_unc_A'), ('R', 'R_calc', 'R_calc_unc_ohm')),
    'source_i': (('V', 'V_meas', 'V_unc_V'), ('R', 'R_calc', 'R_calc_unc_ohm')),
}

#: Units by column, for a file whose units line lacks one.
UNITS = {'R_ohm': 'Ω', 'R_calc': 'Ω', 'I_meas': 'A', 'V_meas': 'V',
         'Rs_ohm_sq': 'Ω/□', 'rho_ohm_cm': 'Ω·cm', 'sigma_S_cm': 'S/cm'}

UNCERTAINTY_CONTINUOUS = (
    "sd is the sample standard deviation of the readings (n - 1): their spread, not "
    "an uncertainty of the mean. u_inst_per_reading is the mean of the file's "
    "per-reading instrument uncertainty: the Keithley 1-year datasheet accuracy taken "
    "as one standard deviation (k = 1, not expanded).")
UNCERTAINTY_FOUR_POINT = (
    "Standard uncertainties, k = 1, none expanded. u_stat = sd / sqrt(n), over the "
    "samples not in compliance. u_inst: the Keithley 1-year datasheet accuracy of V "
    "and I, each taken as one standard deviation, combined in quadrature into V/I and "
    "applied to the mean; it does not shrink with n. u_total = sqrt(u_stat^2 + "
    "u_inst^2).")
UNCERTAINTY_FOUR_POINT_ROWS = (
    "sd is the sample standard deviation of the samples not in compliance (n - 1). "
    "u_stat, u_inst and u_total are written to the file when the run ends.")
UNCERTAINTY_VDP = (
    "u is a combined standard uncertainty, k = 1, not expanded: the Keithley 1-year "
    "datasheet voltage accuracy, taken as one standard deviation (the sourced current "
    "as exact) and averaged over the four wirings, combined in quadrature with the "
    "standard error of the four wirings' resistances, then carried to R_s and rho with "
    "the F76 factor and the thickness taken as exact.")
UNCERTAINTY_SWEEP = (
    "standard_error is the standard uncertainty (k = 1) of the least-squares "
    "coefficient, from the scatter of the points about the line alone; the "
    "instrument's accuracy is not in it.")

#: ASTM F76 §11.1, as calculations_vdp applies it.
HOMOGENEITY_CRITERION = (
    f"|rho_A - rho_B| / rho_avg <= {F76_HOMOGENEITY_TOLERANCE_PCT:g} % (ASTM F76 "
    f"section 11.1), worked out on R_s,A and R_s,B, in which the thickness cancels")


def result_of(run: RunFile, whole_file: bool = True) -> Optional[Dict[str, Any]]:
    """The run's headline result, or None for a file of no known mode.

    ``whole_file`` false: only some of the rows are being summarised, and a
    result the file's end block holds for all of them (a four-point spot's
    statistics) is worked out from those rows instead.
    """
    mode = run.metadata.get('mode')
    if mode in CONTINUOUS_RESULTS:
        result = _continuous_result(run, CONTINUOUS_RESULTS[mode])
    elif mode == 'four_point':
        result = _four_point_result(run, whole_file)
    elif mode == 'vdp':
        result = _vdp_result(run)
    elif mode == 'sweep':
        result = _sweep_result(run)
    else:
        return None
    return _json_safe({'mode': mode, **result})


def _not_in_compliance(run: RunFile) -> List[bool]:
    """Per row: whether it counts. A reading in compliance is a bound, not a value."""
    if 'compliance' not in run.columns:
        return [True] * len(run.rows)
    return [value in NOT_IN_COMPLIANCE for value in run.column('compliance')]


def _finite(run: RunFile, column: str, keep: Sequence[bool]) -> List[float]:
    if column not in run.columns:
        return []
    values = [number(cell) for cell, kept in zip(run.column(column), keep) if kept]
    return [value for value in values if value is not None and math.isfinite(value)]


def _spread(values: Sequence[float], unit: Optional[str]) -> Dict[str, Any]:
    """unit, mean, sample SD (n - 1; None below two values) and n."""
    n = len(values)
    mean = sum(values) / n if n else None
    sd = (math.sqrt(sum((value - mean) ** 2 for value in values) / (n - 1))
          if n > 1 else None)
    return {'unit': unit, 'mean': mean, 'sd': sd, 'n': n}


def _unit(run: RunFile, column: str) -> Optional[str]:
    return (run.unit(column) if column in run.columns else None) or UNITS.get(column)


def _g(value: Any) -> str:
    return 'n/a' if value is None or not math.isfinite(value) else f'{value:.6g}'


def _continuous_result(run: RunFile, quantities) -> Dict[str, Any]:
    keep = _not_in_compliance(run)
    result: Dict[str, Any] = {}
    said = []
    for name, column, uncertainty in quantities:
        quantity = _spread(_finite(run, column, keep), _unit(run, column))
        inst = _finite(run, uncertainty, keep)
        quantity['u_inst_per_reading'] = sum(inst) / len(inst) if inst else None
        result[name] = quantity
        said.append(f"{name} = {_g(quantity['mean'])} ± {_g(quantity['sd'])} "
                    f"{quantity['unit'] or ''}".rstrip())
    excluded = keep.count(False)
    result['excluded_in_compliance'] = excluded
    first = result[quantities[0][0]]
    result['headline'] = (f"{'; '.join(said)} (mean ± SD of {first['n']} readings"
                          f"{_left_out(excluded, 'row')})")
    result['uncertainty'] = UNCERTAINTY_CONTINUOUS
    return result


def _left_out(count: int, what: str) -> str:
    if not count:
        return ''
    return f"; {count} {what}{'s' if count != 1 else ''} in compliance left out"


#: A four-point quantity: the name in a result, in spot_stats, and its column.
FOUR_POINT_QUANTITIES = (('Rs', 'rs', 'Rs_ohm_sq'), ('rho', 'rho', 'rho_ohm_cm'),
                         ('sigma', 'sigma', 'sigma_S_cm'))


def _four_point_result(run: RunFile, whole_file: bool) -> Dict[str, Any]:
    end = run.end_metadata or {}
    result: Dict[str, Any] = {}
    if whole_file and end.get('spot_stats.rs.mean') is not None:
        for name, key, column in FOUR_POINT_QUANTITIES:
            stats = {field: end.get(f'spot_stats.{key}.{field}')
                     for field in ('mean', 'sd', 'u_stat', 'u_inst', 'u_total', 'n')}
            # No thickness: rho and sigma are NaN in every row, and n is 0.
            if name != 'Rs' and (not stats['n'] or stats['mean'] is None):
                continue
            result[name] = {'unit': _unit(run, column), **stats}
        result['excluded_in_compliance'] = end.get('spot_stats.n_excluded')
        rs = result['Rs']
        result['headline'] = (f"Rs = {_g(rs['mean'])} ± {_g(rs['u_total'])} {rs['unit'] or ''} "
                              f"(mean ± u_total, n = {rs['n']}"
                              f"{_left_out(result['excluded_in_compliance'] or 0, 'sample')})")
        result['uncertainty'] = UNCERTAINTY_FOUR_POINT
        return result
    keep = _not_in_compliance(run)
    for name, _, column in FOUR_POINT_QUANTITIES:
        quantity = _spread(_finite(run, column, keep), _unit(run, column))
        if name == 'Rs' or quantity['n']:
            result[name] = quantity
    excluded = keep.count(False)
    result['excluded_in_compliance'] = excluded
    rs = result['Rs']
    result['headline'] = (f"Rs = {_g(rs['mean'])} ± {_g(rs['sd'])} {rs['unit'] or ''} "
                          f"(mean ± SD of {rs['n']} samples{_left_out(excluded, 'sample')})")
    result['uncertainty'] = UNCERTAINTY_FOUR_POINT_ROWS
    result['note'] = ("from the rows: the run has not finished" if whole_file
                      else "from the rows summarised, not the run's own spot statistics")
    return result


def _vdp_result(run: RunFile) -> Dict[str, Any]:
    end = run.end_metadata or {}
    if end.get('vdp_result.sheet_resistance') is None:
        return {'note': ("no result: a van der Pauw result is written at the end of a run "
                         "that measured all four wirings")}
    homogeneous = end.get('vdp_result.homogeneous')
    asymmetry = end.get('vdp_result.asymmetry_pct')
    result: Dict[str, Any] = {
        'R_s': {'unit': 'Ω/□', 'value': end.get('vdp_result.sheet_resistance'),
                'u': end.get('vdp_result.sheet_resistance_uncertainty')},
    }
    thickness = end.get('vdp_result.thickness_cm')
    if thickness and end.get('vdp_result.rho_avg') is not None:
        result['rho'] = {'unit': 'Ω·cm', 'value': end.get('vdp_result.rho_avg'),
                         'u': end.get('vdp_result.rho_avg_uncertainty'),
                         'thickness_cm': thickness}
    else:
        result['rho'] = None
        result['rho_note'] = "no thickness was given, so no resistivity"
    result['homogeneity'] = {
        'homogeneous': homogeneous,
        'asymmetry_pct': asymmetry,
        'threshold_pct': F76_HOMOGENEITY_TOLERANCE_PCT,
        'criterion': HOMOGENEITY_CRITERION,
    }
    verdict = 'homogeneous' if homogeneous else 'NOT homogeneous'
    relation = '<=' if homogeneous else '>'
    said = f"R_s = {_g(result['R_s']['value'])} ± {_g(result['R_s']['u'])} Ω/□"
    if result['rho'] is not None:
        said += f"; rho = {_g(result['rho']['value'])} ± {_g(result['rho']['u'])} Ω·cm"
    result['headline'] = (f"{said}; {verdict} by ASTM F76 (asymmetry {_g(asymmetry)} % "
                          f"{relation} {F76_HOMOGENEITY_TOLERANCE_PCT:g} %)")
    result['uncertainty'] = UNCERTAINTY_VDP
    return result


#: A sweep file's voltage and current columns: a voltage sweep's, then a
#: current sweep's. Files from before the names followed the source have
#: the first pair whatever was sourced, and params.source_function says.
SWEEP_COLUMNS = (('V_source', 'I_meas'), ('V_meas', 'I_source'))


def _sweep_result(run: RunFile) -> Dict[str, Any]:
    pair = next((pair for pair in SWEEP_COLUMNS
                 if pair[0] in run.columns and pair[1] in run.columns), None)
    if pair is None:
        return {'note': "no voltage and current columns this summary knows"}
    current_sourced = (pair[1] == 'I_source'
                       or run.metadata.get('params.source_function') == 'current')
    compliance = run.column('compliance') if 'compliance' in run.columns else \
        [''] * len(run.rows)
    points = [(number(v), number(i), c) for v, i, c in
              zip(run.column(pair[0]), run.column(pair[1]), compliance)]
    result = fit_sweep(points, 'current' if current_sourced else 'voltage')
    result['uncertainty'] = UNCERTAINTY_SWEEP
    return result


def fit_sweep(points: Sequence[Sequence[Any]], sourced: str) -> Dict[str, Any]:
    """Resistance from sweep points by least squares, with its standard error.

    ``points`` are ``(voltage, current, compliance)``; ``sourced`` is
    ``voltage`` or ``current``. Ordinary least squares puts all the error in
    the quantity fitted, so the measured quantity is regressed on the
    sourced one: I on V for a voltage sweep (R = 1/slope, u(R) =
    u(slope)/slope^2), V on I for a current sweep (R = slope). The other way
    round the slope is low by the factor r^2. The desktop app's sweep panel
    fits the same way.

    A point in compliance is the limit, not the sample, and is left out and
    counted; so is one that is not a number. Every other point of every leg
    goes into the one line. The intercept is the measured quantity where
    the sourced one is zero: a current offset (A) for a voltage sweep, a
    voltage offset (V) for a current sweep.

    Pure Python: the MCP server does not depend on numpy.
    """
    voltage_sourced = sourced != 'current'
    xs: List[float] = []
    ys: List[float] = []
    in_compliance = not_a_number = 0
    for volts, amps, compliance in points:
        if compliance not in NOT_IN_COMPLIANCE:
            in_compliance += 1
            continue
        if volts is None or amps is None or not (math.isfinite(volts) and math.isfinite(amps)):
            not_a_number += 1
            continue
        xs.append(volts if voltage_sourced else amps)
        ys.append(amps if voltage_sourced else volts)
    n = len(xs)
    fit: Dict[str, Any] = {
        'sourced': 'voltage' if voltage_sourced else 'current',
        'fit': 'I on V' if voltage_sourced else 'V on I',
        'R': {'unit': 'Ω', 'value': None, 'standard_error': None},
        'intercept': {'unit': 'A' if voltage_sourced else 'V', 'value': None,
                      'standard_error': None,
                      'meaning': 'I at V = 0' if voltage_sourced else 'V at I = 0'},
        'n': n,
        'r2': None,
        'excluded_in_compliance': in_compliance,
        'excluded_not_a_number': not_a_number,
    }
    mx = sum(xs) / n if n else 0.0
    my = sum(ys) / n if n else 0.0
    sxx = sum((x - mx) ** 2 for x in xs)
    if n < 2 or sxx == 0:
        fit['headline'] = (f"no fit: {n} usable point{'s' if n != 1 else ''}"
                           + (" at one sourced value" if n >= 2 else "")
                           + _left_out(in_compliance, 'point'))
        return fit
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    slope = sxy / sxx
    intercept = my - slope * mx
    fit['r2'] = 1.0 if syy == 0 else sxy * sxy / (sxx * syy)
    fit['intercept']['value'] = intercept
    u_slope = None
    if n > 2:
        # Residual variance on n - 2 degrees of freedom.
        variance = max(0.0, syy - slope * sxy) / (n - 2)
        u_slope = math.sqrt(variance / sxx)
        fit['intercept']['standard_error'] = math.sqrt(variance * (1.0 / n + mx * mx / sxx))
    if not voltage_sourced:
        fit['R']['value'], fit['R']['standard_error'] = slope, u_slope
    elif slope != 0:
        fit['R']['value'] = 1.0 / slope
        fit['R']['standard_error'] = None if u_slope is None else u_slope / (slope * slope)
    r = fit['R']
    fit['headline'] = (
        (f"R = {_g(r['value'])} ± {_g(r['standard_error'])} Ω" if r['value'] is not None
         else "R: none, the current does not change with the voltage")
        + f" from a least-squares fit of {fit['fit']} (n = {n}, r² = {fit['r2']:.6f}"
        + _left_out(in_compliance, 'point') + ")")
    return fit


def read_slice(text: str, offset: int = 0, rows: int = DEFAULT_ROWS) -> Dict[str, Any]:
    """The header, the end block and ``rows`` rows from ``offset`` (negative: from the end)."""
    run = RunFile(text)
    rows = max(1, min(int(rows), MAX_ROWS))
    total = len(run.rows)
    start = max(0, total + offset) if offset < 0 else min(offset, total)
    chosen = run.rows[start:start + rows]
    return {
        'metadata': run.metadata,
        'end': run.end_metadata,
        'finalized': run.finalized,
        'columns': run.columns,
        'units': run.units,
        'total_rows': total,
        'offset': start,
        'rows': [[_cell(cell) for cell in row] for row in chosen],
    }


def find_listed(run_path: str, files: Sequence[Dict[str, Any]]) -> Optional[str]:
    """Which listed result is the file a run wrote.

    A run names its file as the backend opened it (``measurement_data/
    alice/…csv``, relative to the backend's working directory, or
    absolute); ``GET /results`` names it relative to the data directory
    (``alice/…csv``). The listed path that the run's path ends with is
    the one; failing that, the newest with the same file name.
    """
    wanted = run_path.replace('\\', '/')
    name = wanted.rsplit('/', 1)[-1]
    for listed in files:
        path = str(listed.get('path', ''))
        if wanted == path or wanted.endswith('/' + path):
            return path
    for listed in files:
        if listed.get('name') == name:
            return str(listed.get('path'))
    return None


def _cell(cell: str) -> Any:
    """A cell as JSON: an integer, a finite float, null for NaN, or the text."""
    try:
        return int(cell)
    except ValueError:
        pass
    value = number(cell)
    if value is None:
        return cell
    return value if math.isfinite(value) else None


def _json_safe(value: Any) -> Any:
    """Non-finite floats as null, tuples as lists: what JSON can carry."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)

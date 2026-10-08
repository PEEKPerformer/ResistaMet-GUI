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
"""
from __future__ import annotations

import csv
import math
import re
from typing import Any, Dict, List, Optional, Sequence

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
    return summary


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

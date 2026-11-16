"""Shared helpers for the end-to-end (--simulate) GUI tests.

One home for the event-pump utilities and CSV readers that every e2e module
needs, so the harness can't drift between files. The ``sim_window`` fixture
that pairs with these lives in ``conftest.py``.
"""
from __future__ import annotations

import csv
import glob
import os
import time


def wait_until(condition, *, timeout, app):
    """Pump the Qt event loop until ``condition()`` is truthy or ``timeout``
    (seconds) elapses. Returns True on success, False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        app.processEvents()
        time.sleep(0.05)
    return False


def pump_for(seconds, app):
    """Pump the Qt event loop for a fixed wall-clock duration."""
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.05)


def switch_to(window, label, app):
    """Activate the main tab whose text equals ``label``."""
    idx = next(i for i in range(window.main_tabs.count())
               if window.main_tabs.tabText(i) == label)
    window.main_tabs.setCurrentIndex(idx)
    app.processEvents()


def read_csv_data(path):
    """Read a v2.0 CSV: skip #-prefixed metadata header + trailer lines.

    Returns the list of CSV rows (column header first, then data) with all
    `#` comment lines removed. Matches the behavior pandas gets with
    `read_csv(comment='#')` but keeps stdlib-only.
    """
    with open(path) as f:
        return list(csv.reader(line for line in f if not line.startswith('#')))


def newest_csv(pattern="measurement_data/**/*.csv"):
    """Newest CSV (by mtime) matching ``pattern`` under the cwd."""
    csvs = sorted(glob.glob(pattern, recursive=True), key=os.path.getmtime)
    assert csvs, f"no CSV matching {pattern!r} written"
    return csvs[-1]


def csv_header(path):
    """First non-comment row of a v2.0 CSV (the column header)."""
    rows = read_csv_data(path)
    assert rows, f"{path}: no header row"
    return rows[0]

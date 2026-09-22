"""Parity: the resolver must reproduce what the GUI's gather step produced.

``tests/goldens/gather/*.json`` were captured from the pre-refactor GUI by
``tools/capture_gather_goldens.py`` — profile in, widget values in, settings
out. Two checks run against each of them:

* the resolver, given the same profile and widget values, returns the same
  settings dict (no Qt needed);
* the live GUI still returns it too, so the goldens keep describing the
  shipping behaviour rather than a snapshot nobody validates.

When PR-15 makes ``gather_settings_for_mode`` delegate to the resolver, both
checks stay green or the refactor changed behaviour.
"""
import json
import math
from pathlib import Path

import pytest

from resistamet_gui.schema.resolve import resolve_run_settings

GOLDEN_DIR = Path(__file__).parent / "goldens" / "gather"
GOLDENS = sorted(GOLDEN_DIR.glob("*.json"))

assert GOLDENS, "no gather goldens found — run tools/capture_gather_goldens.py"


def _load(path):
    return json.loads(path.read_text())


def _assert_same_settings(actual, expected, label):
    assert set(actual) == set(expected), f"{label}: section mismatch"
    for section in expected:
        assert set(actual[section]) == set(expected[section]), (
            f"{label}: keys differ in '{section}': "
            f"extra={sorted(set(actual[section]) - set(expected[section]))} "
            f"missing={sorted(set(expected[section]) - set(actual[section]))}"
        )
        for key, want in expected[section].items():
            got = actual[section][key]
            if isinstance(want, float) and math.isnan(want):
                assert isinstance(got, float) and math.isnan(got), f"{label}: {section}.{key}"
            elif isinstance(want, float):
                assert got == pytest.approx(want), f"{label}: {section}.{key}"
            else:
                assert got == want, f"{label}: {section}.{key}"


@pytest.mark.parametrize("path", GOLDENS, ids=[p.stem for p in GOLDENS])
def test_resolver_matches_golden(path):
    golden = _load(path)
    resolved = resolve_run_settings(golden['profile'], golden['mode'], golden['overrides'])
    _assert_same_settings(resolved.settings, golden['expected'], path.stem)


@pytest.mark.parametrize("path", GOLDENS, ids=[p.stem for p in GOLDENS])
def test_resolver_reports_no_issues_on_captured_settings(path):
    """Everything the GUI can produce must validate cleanly."""
    golden = _load(path)
    resolved = resolve_run_settings(golden['profile'], golden['mode'], golden['overrides'])
    assert [str(issue) for issue in resolved.issues] == []


@pytest.mark.parametrize("path", GOLDENS, ids=[p.stem for p in GOLDENS])
def test_gui_still_matches_golden(main_window, path):
    """The goldens must keep describing the shipping GUI."""
    pytest.importorskip("PySide6")
    golden = _load(path)
    mode = golden['mode']

    main_window.user_settings = json.loads(json.dumps(golden['profile']))
    widget = main_window.get_widget_for_mode(mode)
    _apply_overrides_to_widgets(widget, golden['overrides'])

    _assert_same_settings(main_window.gather_settings_for_mode(mode),
                          golden['expected'], path.stem)


def _apply_overrides_to_widgets(widget, overrides):
    """Push captured widget values back onto the tab that produced them."""
    # Name mismatches between the settings key and the widget attribute.
    widget_names = {
        'vsource_duration_hours': 'vsource_duration',
        'isource_duration_hours': 'isource_duration',
    }
    for key, value in overrides.items():
        name = widget_names.get(key, key)
        if key == 'nplc' and not hasattr(widget, 'nplc'):
            name = 'sweep_nplc'
        target = getattr(widget, name, None)
        if target is None:
            continue
        if hasattr(target, 'setCurrentText') and isinstance(value, str):
            target.setCurrentText(value)
        elif hasattr(target, 'setChecked') and isinstance(value, bool):
            target.setChecked(value)
        elif hasattr(target, 'setValue'):
            if isinstance(value, float) and math.isnan(value):
                target.setValue(target.minimum())  # the "not measured" sentinel
            else:
                target.setValue(value)

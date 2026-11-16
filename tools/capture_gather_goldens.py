"""Capture what ``gather_settings_for_mode`` produces, as test fixtures.

Run this against the GUI *before* the resolver takes over the non-widget part
of gathering (PR-15 in ``docs/design/tauri_backend_split.md``). The captured
dicts are the parity target: ``tests/test_gather_golden.py`` replays them
through both the GUI and ``schema.resolve``, so the refactor has to reproduce
today's settings exactly, key for key.

Usage::

    QT_QPA_PLATFORM=offscreen python tools/capture_gather_goldens.py

Writes ``tests/goldens/gather/<mode>_<variant>.json``, each holding the
profile, the widget-derived overrides and the expected result. ``--out DIR``
writes them somewhere else, to compare with the committed ones without
touching them.

The window needs a config file to open. It gets one in a temporary directory
that is removed whatever happens, so a capture that fails half-way leaves
nothing behind in the repository.

Widget reads come from the window's own ``_overrides_from_widgets``, so the
captured overrides are exactly what the GUI read.
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

GOLDEN_DIR = REPO_ROOT / "tests" / "goldens" / "gather"

MODES = ('resistance', 'source_v', 'source_i', 'four_point', 'sweep', 'vdp')


def overrides_from_widgets(window, mode):
    """The window's own widget reads, so there is one table, not two.

    Before PR-15 this file carried a copy of ``gather_settings_for_mode``'s
    widget reads; now the window exposes them and a regenerated golden is by
    construction the values the GUI read.
    """
    widget = window.get_widget_for_mode(mode)
    return window._overrides_from_widgets(mode, widget)


def perturb(window, mode):
    """Move the tab off its defaults so the goldens exercise real values."""
    widget = window.get_widget_for_mode(mode)
    if mode == 'resistance':
        widget.res_test_current.setValue(2.5e-4)
        widget.res_measurement_type.setCurrentText('4-wire')
        widget.res_offset_comp.setChecked(False)
        widget.sampling_rate.setValue(4.0)
        widget.auto_zero.setCurrentText('off')
    elif mode == 'source_v':
        widget.vsource_voltage.setValue(42.0)
        widget.vsource_duration.setValue(2.0)
        widget.vsource_run_continuous.setChecked(True)
    elif mode == 'source_i':
        widget.isource_current.setValue(-5e-4)
        widget.isource_duration.setValue(3.0)
        widget.isource_run_continuous.setChecked(False)
    elif mode == 'four_point':
        widget.fpp_current.setValue(5e-5)
        widget.fpp_thickness_um.setValue(1.2)
        widget.fpp_temperature_c.setValue(23.5)
        widget.fpp_dopant_type.setCurrentText('n')
        widget.fpp_delta_mode.setChecked(True)
        widget.nplc.setValue(2.0)
    elif mode == 'sweep':
        widget.sweep_source.setCurrentText('current')
        widget.sweep_start.setValue(-1.0)
        widget.sweep_stop.setValue(1.0)
        widget.sweep_step.setValue(0.1)
        widget.sweep_direction.setCurrentText('up_down')
        widget.sweep_nplc.setValue(0.5)
    elif mode == 'vdp':
        widget.vdp_current.setValue(2e-3)
        widget.vdp_thickness_cm.setValue(0.05)
        widget.vdp_readings_per_polarity.setValue(3)


def capture(window, mode, variant):
    return {
        'mode': mode,
        'variant': variant,
        'profile': json.loads(json.dumps(window.user_settings)),
        'overrides': json.loads(json.dumps(overrides_from_widgets(window, mode))),
        'expected': json.loads(json.dumps(window.gather_settings_for_mode(mode))),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--out', type=Path, default=GOLDEN_DIR,
                        help="directory to write the goldens to")
    golden_dir = parser.parse_args(argv).out

    with tempfile.TemporaryDirectory(prefix="resistamet-goldens-") as scratch:
        written = capture_all(golden_dir, Path(scratch) / "config.json")
    for path in written:
        print(f"wrote {_shown(path)}")


def _shown(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def capture_all(golden_dir: Path, tmp_config: Path):
    """Open the window on ``tmp_config`` and write every golden."""
    from PySide6.QtWidgets import QApplication
    from resistamet_gui import constants

    constants.CONFIG_FILE = str(tmp_config)

    from resistamet_gui.config import ConfigManager
    from resistamet_gui.ui import main_window as main_window_module
    from resistamet_gui.ui.main_window import ResistanceMeterApp

    main_window_module.ConfigManager = (
        lambda *args, **kwargs: ConfigManager(config_file=str(tmp_config))
    )
    ResistanceMeterApp.select_user = lambda self: None

    app = QApplication.instance() or QApplication([sys.argv[0]])  # noqa: F841
    window = ResistanceMeterApp()
    try:
        window.config_manager.add_user("golden_user")
        window.current_user = "golden_user"
        window.user_settings = window.config_manager.get_user_settings("golden_user")
        window.update_ui_from_settings()

        golden_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for mode in MODES:
            for variant in ('defaults', 'perturbed'):
                if variant == 'perturbed':
                    perturb(window, mode)
                golden = capture(window, mode, variant)
                path = golden_dir / f"{mode}_{variant}.json"
                # allow_nan keeps fpp_temperature_c's NaN, which is the value
                # the worker actually receives for "not measured".
                path.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n")
                written.append(path)
        return written
    finally:
        window.close()


if __name__ == "__main__":
    main()

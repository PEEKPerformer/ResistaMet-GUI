"""Console-script entry point for ``resistamet-gui`` (after ``pip install``).

Also the import target used by the in-repo launcher ``resistamet-gui.py`` so
the two paths stay identical.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys

# Heavy imports (PySide6, matplotlib, the main window) are deferred into main()
# so --version and --help return instantly without spinning up Cocoa / X11
# event-loop bookkeeping that would otherwise prevent a clean process exit.
from resistamet_gui.constants import __version__

# Matplotlib's qtagg backend and pyqtgraph both auto-detect the active Qt
# binding by env var. Set before either import so they don't accidentally
# pick up a stray PyQt5 install and end up with two bindings loaded at once.
os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")


def _parse_args(argv):
    # Help text stays plain ASCII — argparse prints to stdout, and the
    # default Windows console codec (cp1252) can't encode Ω / em-dash / etc.,
    # which crashes --help on Windows. The GUI itself is UTF-8 throughout.
    parser = argparse.ArgumentParser(
        prog="resistamet-gui",
        description=(
            "ResistaMet GUI - electrical characterization for Keithley "
            "2400/2450 sourcemeters."
        ),
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="Run against the in-package Keithley simulator instead of real "
             "hardware. No NI-VISA / pyvisa-py / GPIB needed.",
    )
    parser.add_argument(
        "--sim-resistance", type=float, default=100.0, metavar="OHMS",
        help="DUT resistance the simulator presents in ohms (default: 100). "
             "Only meaningful with --simulate.",
    )
    parser.add_argument(
        "--sim-model", default="2420",
        help="Keithley model number the simulator advertises in its IDN "
             "response (default: 2420). Only meaningful with --simulate.",
    )
    parser.add_argument(
        "--sim-noise-rsd", type=float, default=0.0, metavar="RSD",
        help="Gaussian noise RSD applied to the measured side of each "
             "reading (default: 0.0 = perfect Ohm's law). Typical demo "
             "value: 1e-3 (0.1%%). Only meaningful with --simulate.",
    )
    parser.add_argument(
        "--version", action="version", version=f"ResistaMet GUI {__version__}",
    )
    return parser.parse_args(argv)


def main():
    args = _parse_args(sys.argv[1:])

    if args.simulate:
        from resistamet_gui.simulator import enable_simulation
        enable_simulation(
            dut_resistance_ohms=args.sim_resistance,
            model=args.sim_model,
            noise_rsd=args.sim_noise_rsd,
        )

    from PySide6.QtWidgets import QApplication
    from resistamet_gui.ui.main_window import ResistanceMeterApp

    # HighDPI is always-on in Qt6; the AA_* attributes are no-op
    # DeprecationWarnings here, so they're dropped.

    # Pass only argv[0] so QApplication doesn't choke on our --simulate flag.
    app = QApplication([sys.argv[0]])
    app.setStyle("Fusion")
    # App-wide icon so Alt-Tab / dock / taskbar pick it up even before the
    # main window exists. The frozen .exe also carries it embedded via the
    # PyInstaller spec's icon= (that's what Explorer shows); this covers the
    # source-run and title-bar paths.
    from resistamet_gui.resources import app_icon
    app.setWindowIcon(app_icon())
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    window = ResistanceMeterApp()
    if args.simulate:
        window.setWindowTitle(
            f"{window.windowTitle()}  —  SIMULATOR "
            f"({args.sim_resistance:g} Ω DUT, model {args.sim_model})"
        )
        window.statusBar().showMessage(
            f"Simulator active: model {args.sim_model}, "
            f"DUT = {args.sim_resistance:g} Ω. No real instrument connected."
        )
    window.show()

    try:
        sys.exit(app.exec())
    except KeyboardInterrupt:
        print("Ctrl+C detected, exiting.")


if __name__ == "__main__":
    main()

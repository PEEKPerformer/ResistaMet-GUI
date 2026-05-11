"""Console-script entry point for ``resistamet-gui`` (after ``pip install``).

Also the import target used by the in-repo launcher ``resistamet-gui.py`` so
the two paths stay identical.
"""
from __future__ import annotations

import argparse
import signal
import sys

# Heavy imports (PyQt5, matplotlib, the main window) are deferred into main()
# so --version and --help return instantly without spinning up Cocoa / X11
# event-loop bookkeeping that would otherwise prevent a clean process exit.
from resistamet_gui.constants import __version__


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
        )

    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication
    from resistamet_gui.ui.main_window import ResistanceMeterApp

    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    # Pass only argv[0] so QApplication doesn't choke on our --simulate flag.
    app = QApplication([sys.argv[0]])
    app.setStyle("Fusion")
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
        sys.exit(app.exec_())
    except KeyboardInterrupt:
        print("Ctrl+C detected, exiting.")


if __name__ == "__main__":
    main()

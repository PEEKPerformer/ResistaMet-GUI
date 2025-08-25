#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import signal
import sys
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt

from resistamet_gui.ui.main_window import ResistanceMeterApp


def main():
    if hasattr(Qt, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    signal.signal(signal.SIGINT, signal.SIG_DFL)

    window = ResistanceMeterApp()
    window.show()

    try:
        sys.exit(app.exec_())
    except KeyboardInterrupt:
        print("Ctrl+C detected, exiting.")


if __name__ == "__main__":
    main()


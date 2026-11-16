#!/usr/bin/env python3
"""Launcher for the frozen backend.

PyInstaller runs its entry script as a plain script, where the package-relative
imports in ``resistamet_gui/api/__main__.py`` do not resolve. This wrapper
imports the module as part of its package and calls its ``main``; it is the
only thing ``resistamet-api.spec`` freezes directly. Running from source stays
``python -m resistamet_gui.api``.
"""
import sys

from resistamet_gui.api.__main__ import main

if __name__ == "__main__":
    sys.exit(main())

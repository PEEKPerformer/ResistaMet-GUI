"""Packaged application assets (logo / window icon).

Kept tiny and Qt-lazy: :func:`asset_path` has no Qt dependency so non-GUI code
can locate bundled files, and :func:`app_icon` imports QtGui only when actually
called. Resolution is ``__file__``-relative, which works both from a source
checkout and from a PyInstaller one-file bundle — the spec copies
``resistamet_gui/assets`` into the extraction dir next to this module, so the
same relative lookup lands on the files in either case.
"""
from __future__ import annotations

from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def asset_path(name: str) -> Path:
    """Absolute path to a bundled asset, e.g. ``asset_path('logo.svg')``."""
    return ASSETS_DIR / name


def app_icon():
    """The ResistaMet window / taskbar icon as a ``QIcon``.

    Prefers the multi-resolution ``.ico`` (crisp 16→256 px frames the Windows
    taskbar can pick from); falls back to the 256 px PNG, then to a freedesktop
    theme name so a missing asset degrades gracefully instead of crashing.
    """
    from PySide6.QtGui import QIcon

    for name in ("logo.ico", "logo.png"):
        p = ASSETS_DIR / name
        if p.exists():
            return QIcon(str(p))
    return QIcon.fromTheme("accessories-voltmeter")

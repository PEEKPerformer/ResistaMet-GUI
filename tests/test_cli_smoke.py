"""Smoke tests for the CLI entry point.

``resistamet-gui --version`` and ``--help`` should:
    1. exit 0,
    2. print recognizable output,
    3. return *quickly* — argparse handlers must run before the GUI
       imports load PyQt5 + matplotlib, otherwise the process hangs on
       Cocoa/X11 event-loop bookkeeping (we hit this exact issue during
       v1.6.0 polish).

Tests spawn a fresh subprocess so we don't share Python interpreter state
with the rest of the test suite (and so the Qt platform is whatever the
shell uses by default).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "resistamet-gui.py"


def _run(args, timeout: float = 15.0) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # Offscreen so an accidental GUI import doesn't try to open a display.
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    return subprocess.run(
        [sys.executable, str(LAUNCHER), *args],
        capture_output=True, text=True, timeout=timeout, env=env,
        cwd=str(REPO_ROOT),
    )


def test_version_flag_prints_and_exits_cleanly():
    from resistamet_gui.constants import __version__
    result = _run(["--version"])
    assert result.returncode == 0, (
        f"--version exit={result.returncode}\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    # argparse writes to stdout for --version on Python 3.4+
    combined = (result.stdout + result.stderr)
    assert __version__ in combined, (
        f"--version output missing {__version__!r}: {combined!r}"
    )


def test_help_flag_prints_and_exits_cleanly():
    result = _run(["--help"])
    assert result.returncode == 0, (
        f"--help exit={result.returncode}\nstderr: {result.stderr!r}"
    )
    assert "--simulate" in result.stdout, (
        f"--help missing --simulate flag in output:\n{result.stdout}"
    )
    assert "ResistaMet" in result.stdout

"""The golden-capture tool cleans up after itself, even when it fails.

It used to keep the window's config file inside ``tools/`` and delete it on
the last line of a successful run, so a capture that raised left an
untracked, un-ignored JSON file in the repository.

Run in a subprocess: the tool rebinds ``constants.CONFIG_FILE`` and the
window's ``ConfigManager`` for the life of its process, which is no state to
leave in a test session.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "tools" / "capture_gather_goldens.py"

FAIL_HALF_WAY = """
import runpy, sys
tool = runpy.run_path({tool!r}, run_name='capture_tool')
calls = []
real_capture = tool['capture']
def capture(window, mode, variant):
    calls.append(mode)
    if len(calls) == 3:
        raise RuntimeError('capture failed half-way')
    return real_capture(window, mode, variant)
tool['main'].__globals__['capture'] = capture
tool['main'](['--out', {out!r}])
"""


def _run(code, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", TMPDIR=str(scratch),
               TEMP=str(scratch), TMP=str(scratch))
    done = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=120)
    return done, scratch


def _stray_files():
    return sorted(p.name for p in (REPO_ROOT / "tools").glob("_golden_capture*"))


def test_a_failed_capture_leaves_nothing_behind(tmp_path):
    out = tmp_path / "goldens"
    done, scratch = _run(FAIL_HALF_WAY.format(tool=str(TOOL), out=str(out)), tmp_path)

    assert done.returncode != 0
    assert 'capture failed half-way' in done.stderr
    assert _stray_files() == []
    assert list(scratch.iterdir()) == [], "the temporary config directory was removed"


def test_out_keeps_the_committed_goldens_untouched(tmp_path):
    out = tmp_path / "goldens"
    committed = REPO_ROOT / "tests" / "goldens" / "gather"
    before = {p.name: p.read_bytes() for p in committed.glob("*.json")}

    done, scratch = _run(f"import runpy, sys; sys.argv = [{str(TOOL)!r}, '--out', {str(out)!r}]; "
                         f"runpy.run_path({str(TOOL)!r}, run_name='__main__')", tmp_path)

    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in out.glob("*.json")) == sorted(before)
    assert {p.name: p.read_bytes() for p in committed.glob("*.json")} == before
    assert _stray_files() == []
    assert list(scratch.iterdir()) == []

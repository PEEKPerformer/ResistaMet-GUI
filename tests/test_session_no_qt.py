"""The session package must stay importable without Qt.

Same contract as ``test_schema_no_qt``: the run layer is what the API sidecar
imports, and it must not drag a GUI toolkit into a headless process.
"""
import subprocess
import sys

CHECK = """
import sys
import resistamet_gui.session
leaked = sorted(m for m in sys.modules if m.split('.')[0] in ('PySide6', 'shiboken6'))
print(','.join(leaked))
"""


def test_no_gui_import():
    result = subprocess.run([sys.executable, '-c', CHECK], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ''

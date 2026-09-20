"""The session package must stay importable without Qt.

Same contract as ``test_schema_no_qt``: the run layer is what the API sidecar
imports, and it must not drag a GUI toolkit into a headless process.
"""
import subprocess
import sys

CHECK = """
import importlib
import pkgutil
import sys
import resistamet_gui.session
import resistamet_gui.api
# Every submodule by name: what the package's __init__ happens to import is
# not the contract, and a run module that imported Qt would otherwise pass.
for found in pkgutil.walk_packages(resistamet_gui.session.__path__, 'resistamet_gui.session.'):
    importlib.import_module(found.name)
assert 'resistamet_gui.session.continuous_run' in sys.modules
assert 'resistamet_gui.session.vdp_run' in sys.modules
leaked = sorted(m for m in sys.modules if m.split('.')[0] in ('PySide6', 'shiboken6'))
print(','.join(leaked))
"""


def test_no_gui_import():
    result = subprocess.run([sys.executable, '-c', CHECK], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ''


CHECK_CONTROL = """
import threading
from resistamet_gui.session.control import RunControl

control = RunControl()
control.running = True
control.mark_event('A')
control.mark_event('B')
assert control.event_marker == 'A; B'
assert control.get_and_clear_event_marker() == 'A; B'
assert control.event_marker == ''
assert control.running is True
control.proceed_event.set()
assert control.proceed_event.is_set()
print('ok')
"""


def test_run_control_works_without_qt():
    """The session's run state must be usable from a plain thread."""
    result = subprocess.run([sys.executable, '-c', CHECK_CONTROL],
                             capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'


CHECK_PROMPT = """
import threading
from resistamet_gui.session.control import RunControl

control = RunControl()
control.running = True
prompt = control.raise_prompt('vdp_geometry', ['proceed', 'abort'], detail={'index': 0})
assert control.pending_prompt.prompt_id == prompt.prompt_id
assert control.answer_prompt('stale-id', 'proceed') is False
assert control.answer_prompt(prompt.prompt_id, 'proceed') is True
assert control.answer_prompt(prompt.prompt_id, 'abort') is False, 'first answer wins'
assert control.wait_for_prompt() == ('proceed', {})
assert control.pending_prompt is None

# a stop releases the wait with no answer
second = control.raise_prompt('vdp_geometry', ['proceed', 'abort'])
released = []
def waiter():
    released.append(control.wait_for_prompt()[0])
t = threading.Thread(target=waiter)
t.start()
control.running = False
control.proceed_event.set()
t.join(timeout=2)
assert released == [None], released
print('ok')
"""


def test_prompt_handshake_without_qt():
    result = subprocess.run([sys.executable, '-c', CHECK_PROMPT],
                             capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'

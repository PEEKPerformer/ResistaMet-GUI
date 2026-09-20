"""What is on disk if the process dies without warning.

A run's file is the only record of the measurement, and the process writing
it can be killed outright -- a power cut, Task Manager, the OS. These tests
write rows in a child process, kill it with no chance to clean up, and read
what the file holds.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]

_WRITER = """
import sys, time
from resistamet_gui.data_export import CsvExporter
base, compression, rows = sys.argv[1], sys.argv[2], int(sys.argv[3])
exporter = CsvExporter(base, {'user': 'u', 'sample': 's'}, ['elapsed_s', 'V', 'I'],
                       compression=compression)
for i in range(rows):
    exporter.write_row([i * 0.1, 1.0, 1e-3])
print('written', flush=True)
time.sleep(120)
"""


def _rows_after_a_kill(tmp_path, compression, rows):
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(_REPO), env.get('PYTHONPATH')]))
    child = subprocess.Popen(
        [sys.executable, '-c', _WRITER, str(tmp_path / 'run'), compression, str(rows)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(tmp_path), env=env)
    try:
        line = child.stdout.readline()
        assert line.strip() == 'written', child.stderr.read()[-600:]
    finally:
        child.kill()  # SIGKILL, or TerminateProcess on Windows: no cleanup runs
        child.wait(timeout=10)
    text = (tmp_path / 'run.csv').read_text(encoding='utf-8')
    data = [line for line in text.splitlines()
            if line and not line.startswith('#') and not line.startswith('elapsed_s')]
    return data


@pytest.mark.parametrize('compression', ['never', 'always'])
def test_every_row_written_survives_a_kill(tmp_path, compression):
    """Compression happens at finalize; until then the file is plain CSV."""
    data = _rows_after_a_kill(tmp_path, compression, rows=25)

    assert len(data) == 25
    assert data[0].startswith('0,') and data[-1].startswith('2.4,')


def test_a_single_row_survives_a_kill(tmp_path):
    assert len(_rows_after_a_kill(tmp_path, 'never', rows=1)) == 1

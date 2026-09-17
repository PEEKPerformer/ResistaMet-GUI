"""One process at a time per instrument address.

The lock is an OS file lock, so the interesting case is a *second process*:
within one process a second acquire on the same file may or may not block
depending on the platform, and that is not what this protects against.
"""
import subprocess
import sys
import textwrap

import pytest

from resistamet_gui.session.instrument_lock import (
    InstrumentBusy, hold_instrument, lock_path,
)

HOLDER = """
import sys, time
from resistamet_gui.session.instrument_lock import hold_instrument
with hold_instrument({address!r}, {lock_dir!r}):
    print('held', flush=True)
    time.sleep(float(sys.argv[1]))
"""


def test_lock_file_name_is_path_safe(tmp_path):
    path = lock_path('GPIB0::24::INSTR', str(tmp_path))
    assert path.parent == tmp_path
    assert path.name == 'GPIB0__24__INSTR.lock'


def test_lock_is_released_after_the_block(tmp_path):
    with hold_instrument('GPIB0::24::INSTR', str(tmp_path)):
        pass
    with hold_instrument('GPIB0::24::INSTR', str(tmp_path)):
        pass  # would raise if the first hold leaked


def test_empty_address_is_a_no_op(tmp_path):
    with hold_instrument('', str(tmp_path)) as path:
        assert path is None


def test_second_process_is_refused(tmp_path):
    """The case the lock exists for: two ResistaMet processes, one bus."""
    script = HOLDER.format(address='GPIB0::24::INSTR', lock_dir=str(tmp_path))
    holder = subprocess.Popen([sys.executable, '-c', textwrap.dedent(script), '5'],
                               stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'held'
        with pytest.raises(InstrumentBusy):
            with hold_instrument('GPIB0::24::INSTR', str(tmp_path)):
                pass
    finally:
        holder.kill()
        holder.wait(timeout=5)


def test_lock_survives_the_holder_being_killed(tmp_path):
    """No stale-lock logic: the OS releases when the holder dies."""
    script = HOLDER.format(address='GPIB0::24::INSTR', lock_dir=str(tmp_path))
    holder = subprocess.Popen([sys.executable, '-c', textwrap.dedent(script), '30'],
                               stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == 'held'
    holder.kill()
    holder.wait(timeout=5)

    with hold_instrument('GPIB0::24::INSTR', str(tmp_path)):
        pass  # acquired straight after the holder died


def test_different_addresses_do_not_collide(tmp_path):
    with hold_instrument('GPIB0::24::INSTR', str(tmp_path)):
        with hold_instrument('ASRL6::INSTR', str(tmp_path)):
            pass

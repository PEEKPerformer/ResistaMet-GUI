"""One process at a time per instrument address.

The lock is an OS file lock, so the interesting case is a *second process*:
within one process a second acquire on the same file may or may not block
depending on the platform, and that is not what this protects against.
"""
import subprocess
import sys
import textwrap
import time

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


@pytest.mark.parametrize('spelling', [
    'GPIB0::24::INSTR', 'GPIB0::24', 'GPIB::24::INSTR', 'GPIB::24',
    'gpib0::24::instr', 'Gpib::24', ' GPIB0::24::INSTR ', 'GPIB0::024::INSTR',
])
def test_every_spelling_of_one_gpib_instrument_is_one_lock(tmp_path, spelling):
    assert lock_path(spelling, str(tmp_path)).name == 'GPIB0__24__INSTR.lock'


def test_other_boards_addresses_and_secondaries_stay_apart(tmp_path):
    names = {lock_path(a, str(tmp_path)).name for a in (
        'GPIB0::24::INSTR', 'GPIB1::24::INSTR', 'GPIB0::25::INSTR',
        'GPIB0::24::1::INSTR', 'GPIB0::INTFC')}
    assert len(names) == 5
    assert lock_path('GPIB0::24::1', str(tmp_path)).name == 'GPIB0__24__1__INSTR.lock'


def test_other_resource_strings_only_lose_case_and_whitespace(tmp_path):
    assert lock_path(' asrl6::instr ', str(tmp_path)).name == 'ASRL6__INSTR.lock'
    assert lock_path('ASRL6::INSTR', str(tmp_path)).name == 'ASRL6__INSTR.lock'
    # No INSTR is added where the default class is not ours to know.
    assert lock_path('TCPIP0::10.0.0.5::5025::SOCKET', str(tmp_path)).name == (
        'TCPIP0__10.0.0.5__5025__SOCKET.lock')
    assert lock_path('ASRL6', str(tmp_path)).name == 'ASRL6.lock'


def test_two_spellings_exclude_each_other(tmp_path):
    script = HOLDER.format(address='gpib::24', lock_dir=str(tmp_path))
    holder = subprocess.Popen([sys.executable, '-c', textwrap.dedent(script), '5'],
                               stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'held'
        with pytest.raises(InstrumentBusy):
            with hold_instrument('GPIB0::24::INSTR', str(tmp_path), wait_s=0.2):
                pass
    finally:
        holder.kill()
        holder.wait(timeout=5)


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
            with hold_instrument('GPIB0::24::INSTR', str(tmp_path), wait_s=0.2):
                pass
    finally:
        holder.kill()
        holder.wait(timeout=5)


def test_a_holder_on_its_way_out_is_waited_for(tmp_path):
    """Start right after Stop: the previous run may still be in cleanup.

    The GUI clears its running flag before the worker's cleanup releases the
    lock, so the next run can arrive a few hundred milliseconds early. That is
    not another process on the bus and must not be refused as one.
    """
    script = HOLDER.format(address='GPIB0::24::INSTR', lock_dir=str(tmp_path))
    holder = subprocess.Popen([sys.executable, '-c', textwrap.dedent(script), '0.5'],
                               stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'held'
        began = time.monotonic()
        with hold_instrument('GPIB0::24::INSTR', str(tmp_path), wait_s=3.0):
            waited = time.monotonic() - began
        assert 0.2 < waited < 2.5, f"waited {waited:.2f}s"
    finally:
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


class TestWhereTheLockLives:
    """Two processes exclude each other only if they look in the same place."""

    @pytest.mark.shared_lock_dir
    def test_default_dir_is_per_user_not_per_working_directory(self, tmp_path, monkeypatch):
        from resistamet_gui.session import instrument_lock

        monkeypatch.setattr(instrument_lock.Path, 'home', staticmethod(lambda: tmp_path))
        (tmp_path / 'a').mkdir()
        (tmp_path / 'b').mkdir()
        monkeypatch.chdir(tmp_path / 'a')
        first = lock_path('GPIB0::24::INSTR')
        monkeypatch.chdir(tmp_path / 'b')
        second = lock_path('GPIB0::24::INSTR')
        assert first == second == tmp_path / '.resistamet' / 'locks' / 'GPIB0__24__INSTR.lock'


class TestHeldInstrument:
    def test_held_then_released_frees_the_address(self, tmp_path):
        from resistamet_gui.session.instrument_lock import HeldInstrument

        held = HeldInstrument('GPIB0::24::INSTR', str(tmp_path))
        assert held.path == lock_path('GPIB0::24::INSTR', str(tmp_path))
        held.release()
        held.release()  # idempotent
        with hold_instrument('GPIB0::24::INSTR', str(tmp_path)):
            pass

    def test_refused_while_another_process_holds_it(self, tmp_path):
        from resistamet_gui.session.instrument_lock import HeldInstrument

        script = HOLDER.format(address='GPIB0::24::INSTR', lock_dir=str(tmp_path))
        holder = subprocess.Popen([sys.executable, '-c', textwrap.dedent(script), '5'],
                                   stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == 'held'
            with pytest.raises(InstrumentBusy):
                HeldInstrument('GPIB0::24::INSTR', str(tmp_path), wait_s=0.2)
        finally:
            holder.kill()
            holder.wait(timeout=5)

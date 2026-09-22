"""One process at a time per instrument address.

The single-run rule is per process: nothing stopped the PySide6 app and a
sidecar — or two copies of the app — from opening GPIB0::24 at once and
interleaving SCPI on the same bus. That does not fail cleanly; it produces
readings that look plausible and are not.

This takes an OS file lock per address, held for the length of a run. The OS
releases it when the holder dies, so there is no stale-lock logic to get wrong
and no pid file to go out of date after a crash.

Lock files live beside the config, one per sanitized address, and are never
deleted — an empty lock file is the cheap part; deleting one while another
process holds it is how these break.
"""
import logging
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from ..constants import CONFIG_FILE

logger = logging.getLogger(__name__)


class InstrumentBusy(RuntimeError):
    """Another process holds this instrument."""


def _lock_dir(lock_dir: Optional[str] = None) -> Path:
    directory = Path(lock_dir) if lock_dir else Path(CONFIG_FILE).resolve().parent / 'locks'
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def lock_path(address: str, lock_dir: Optional[str] = None) -> Path:
    """Where the lock file for ``address`` lives."""
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', address) or 'unnamed'
    return _lock_dir(lock_dir) / f"{safe}.lock"


def _acquire(handle) -> None:
    """Take an exclusive, non-blocking lock. Raises OSError when held."""
    try:
        import fcntl
    except ImportError:  # Windows
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(handle) -> None:
    try:
        import fcntl
    except ImportError:  # Windows
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


#: How long to wait for a holder that is on its way out. A run releases its
#: lock at the end of cleanup — after :OUTP OFF, closing the VISA session and
#: the sleep inhibitor — and the GUI clears its own 'running' flag before that
#: cleanup starts, so a Start pressed right after a Stop can legitimately find
#: the previous run still holding the file for a moment.
ACQUIRE_GRACE_S = 3.0
_POLL_S = 0.05


@contextmanager
def hold_instrument(address: str, lock_dir: Optional[str] = None,
                     wait_s: float = ACQUIRE_GRACE_S):
    """Hold ``address`` for the block, or raise :class:`InstrumentBusy`.

    A holder that releases within ``wait_s`` is waited for; one that does not
    is another process using the instrument, and the caller is refused.
    """
    if not address:
        yield None
        return

    path = lock_path(address, lock_dir)
    handle = open(path, 'a+')
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        try:
            _acquire(handle)
            break
        except OSError as exc:
            if time.monotonic() >= deadline:
                handle.close()
                raise InstrumentBusy(
                    f"{address} is in use by another ResistaMet process"
                ) from exc
            time.sleep(_POLL_S)
    try:
        yield path
    finally:
        try:
            _release(handle)
        finally:
            handle.close()

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


@contextmanager
def hold_instrument(address: str, lock_dir: Optional[str] = None):
    """Hold ``address`` for the block, or raise :class:`InstrumentBusy`."""
    if not address:
        yield None
        return

    path = lock_path(address, lock_dir)
    handle = open(path, 'a+')
    try:
        _acquire(handle)
    except OSError as exc:
        handle.close()
        raise InstrumentBusy(
            f"{address} is in use by another ResistaMet process"
        ) from exc
    try:
        yield path
    finally:
        try:
            _release(handle)
        finally:
            handle.close()

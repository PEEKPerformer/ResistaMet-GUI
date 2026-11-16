"""Letting AI agents in, and keeping them out (``docs/design/mcp_layer.md`` M2).

While agent access is on, the backend holds a second token, role ``agent``,
and writes it to a connection file that only this user can read. An MCP
server finds the backend through that file; the token never goes near
stdout, which the desktop reads the ``ui`` token from.

Turning access off withdraws the token at once -- an agent holding it gets
401 on its next request -- and deletes the file. Turning it on again mints a
new token, so one that leaked while access was on is worth nothing later.

The file names the URL, which is only known once the socket is bound. So
access can be switched on before that, and the file follows as soon as the
URL is set.

Two backends on one account would fight over one file. The first one keeps
it: a file whose process is still alive and is not this one is left alone,
and this backend runs without agent access. A file whose process is gone is
stale and is replaced.
"""
import json
import logging
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def default_connection_file() -> str:
    """Where an MCP server looks for a backend that lets agents in."""
    return str(Path.home() / '.resistamet' / 'api' / 'connection.json')


def pid_is_alive(pid: int) -> bool:
    """Whether a process with this id exists. In doubt, yes.

    The caller only replaces a file whose process is gone, so the safe
    mistake is to think a process alive: agent access stays off and the log
    says which file to delete. A recycled pid reads as alive for the same
    reason.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        # 0 and negative ids name process groups to os.kill, not a process.
        return False
    if sys.platform == 'win32':
        return _windows_pid_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists; it belongs to someone else
    except OSError:
        return True
    return True


def _windows_pid_is_alive(pid: int) -> bool:
    # os.kill on Windows does not probe: any signal but CTRL_C/CTRL_BREAK
    # terminates the process. Ask the kernel instead.
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Access denied: there is such a process, and it is not ours to open.
        # Anything else, ERROR_INVALID_PARAMETER above all: there is none.
        return ctypes.get_last_error() == error_access_denied
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        # A process that exited with 259 reads as running: the safe mistake.
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _read_pid(path: str) -> Optional[int]:
    """The pid a connection file names, or None if it names none."""
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            content = json.load(handle)
    except (OSError, ValueError):
        return None
    pid = content.get('pid') if isinstance(content, dict) else None
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None


def _write_private(path: str, content: dict) -> None:
    """Write ``content`` so that only this user can ever read it.

    The file is created 0600, not chmod'ed afterwards, so there is no moment
    at which the token is readable by others; then it is renamed over the
    old one, so a reader sees a whole file or none. On Windows the mode bits
    do nothing: what keeps other accounts out there is that the file is in
    this user's profile directory.
    """
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory, mode=0o700, exist_ok=True)
    temporary = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_BINARY', 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(json.dumps(content).encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class AgentAccess:
    """The agent token and its connection file, for one backend process.

    ``grant`` and ``revoke`` add a token to the API's token map with the
    ``agent`` role and take it out again; this class decides when.
    """

    def __init__(self, grant: Callable[[str], None], revoke: Callable[[str], None],
                 path: Optional[str] = None):
        self._grant = grant
        self._revoke = revoke
        self.path = path or default_connection_file()
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._url: Optional[str] = None
        self._published = False

    @property
    def enabled(self) -> bool:
        return self._token is not None

    def set_url(self, url: str) -> None:
        """Where the backend listens, once the socket is bound."""
        with self._lock:
            self._url = url
            if self._token is not None and not self._published:
                self._publish()

    def enable(self) -> bool:
        """Let agents in. Returns whether access is on afterwards.

        Already on: nothing changes, so an agent that is connected stays
        connected. Off for this process when another live backend holds the
        connection file, or the file cannot be written.
        """
        with self._lock:
            if self._token is not None:
                return True
            self._token = secrets.token_urlsafe(32)
            self._grant(self._token)
            if self._url is not None:
                self._publish()
            return self._token is not None

    def disable(self) -> None:
        """Turn agents out: the token stops working now and the file goes."""
        with self._lock:
            self._withdraw()
            if self._published:
                self._published = False
                # Only our own file: never one another backend has written
                # since.
                if _read_pid(self.path) == os.getpid():
                    try:
                        os.remove(self.path)
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        logger.error("could not remove the agent connection file %s: %s",
                                     self.path, exc)

    def _withdraw(self) -> None:
        if self._token is not None:
            self._revoke(self._token)
            self._token = None

    def _publish(self) -> None:
        """Write the connection file, or turn access back off if we may not."""
        holder = _read_pid(self.path)
        if holder is not None and holder != os.getpid() and pid_is_alive(holder):
            logger.error("agent access stays off: another backend (pid %d) is "
                         "serving agents through %s. Stop it, or delete the file "
                         "if no such backend is running.", holder, self.path)
            self._withdraw()
            return
        try:
            _write_private(self.path, {'url': self._url, 'agent_token': self._token,
                                       'pid': os.getpid(), 'started': time.time()})
        except OSError as exc:
            logger.error("agent access stays off: could not write %s: %s", self.path, exc)
            self._withdraw()
            return
        self._published = True
        logger.info("agent access on; connection file %s", self.path)

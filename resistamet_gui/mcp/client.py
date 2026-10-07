"""Finding the backend, and talking HTTP to it with the agent token.

A backend that lets agents in writes ``~/.resistamet/api/connection.json``
(``{url, agent_token, pid, started}``, readable by this user only) and
removes it when it stops or access is turned off
(``resistamet_gui/api/agent_access.py``). That file is the only way in:
there is no flag for a URL or a token, so an agent cannot be pointed at a
backend the user did not let it into.

The file is read when it is first needed and read again whenever the
backend does not answer or answers 401. Either can mean the backend
restarted (new port, new token) or access was turned off and on (new
token), and in both cases the file says what is true now. A request that
was refused or never connected has done nothing, so it is safe to send
once more; nothing else is retried.

The token goes into the ``Authorization`` header and nowhere else: not into
a log line, an error message, ``repr`` or the audit log.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import httpx2

from . import audit

logger = logging.getLogger(__name__)

#: What an agent is told whenever there is no backend it may use. It names
#: both ways to let agents in, so the agent can pass it on to the user as is.
NOT_RUNNING = ("ResistaMet is not running with AI agents allowed: open the desktop "
               "app → Settings → AI agents, or start "
               "`python -m resistamet_gui.api --allow-agents`")

#: Long enough for an identify on a slow bus; a run is never waited on in a
#: single request (``wait_for`` polls).
REQUEST_TIMEOUT_S = 30.0


def default_connection_file() -> str:
    """Where a backend that lets agents in says so.

    The same path as ``agent_access.default_connection_file``, which this
    package may not import.
    """
    return str(Path.home() / '.resistamet' / 'api' / 'connection.json')


class BackendUnavailable(Exception):
    """No backend will take this agent's requests. The message says why and what to do."""

    def __init__(self, reason: str):
        super().__init__(f"{NOT_RUNNING}. ({reason})")
        self.reason = reason


class BackendError(Exception):
    """The backend answered and refused. ``detail`` is its own explanation, unchanged."""

    def __init__(self, status: int, detail: Any):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Connection:
    """One backend, as its connection file describes it."""

    url: str
    token: str = field(repr=False)
    pid: Optional[int] = None


def process_exists(pid: int) -> bool:
    """Whether a process with this id exists. In doubt, yes.

    Only a fast way to a clear message: a backend whose process is gone left
    its file behind. Saying "alive" wrongly costs nothing, because the
    request that follows fails to connect and says so. On Windows there is
    no cheap probe (``os.kill`` there terminates), so the request decides.
    """
    if sys.platform == 'win32':
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # it exists; it is not ours to signal
    return True


def read_connection_file(path: str) -> Connection:
    """The backend a connection file names. Raises ``BackendUnavailable``."""
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            content = json.load(handle)
    except FileNotFoundError:
        raise BackendUnavailable(f"no connection file at {path}") from None
    except (OSError, ValueError) as exc:
        raise BackendUnavailable(f"the connection file {path} cannot be read: "
                                 f"{type(exc).__name__}") from None
    url = content.get('url') if isinstance(content, dict) else None
    token = content.get('agent_token') if isinstance(content, dict) else None
    if not isinstance(url, str) or not url or not isinstance(token, str) or not token:
        raise BackendUnavailable(f"the connection file {path} names no backend")
    pid = content.get('pid')
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        pid = None
    if pid is not None and not process_exists(pid):
        raise BackendUnavailable(f"the backend that wrote {path} (pid {pid}) is no "
                                 "longer running")
    return Connection(url=url.rstrip('/'), token=token, pid=pid)


class Backend:
    """HTTP to whichever backend the connection file names right now.

    ``transport`` is for tests (an ``httpx2.MockTransport``); the server
    uses the default network transport, on loopback.
    """

    def __init__(self, connection_file: Optional[str] = None, *,
                 transport: Optional[httpx2.AsyncBaseTransport] = None,
                 timeout: float = REQUEST_TIMEOUT_S):
        self.connection_file = connection_file or default_connection_file()
        self._connection: Optional[Connection] = None
        # trust_env=False: a proxy from the environment must never see a
        # request to the backend, which carries the token.
        self._http = httpx2.AsyncClient(transport=transport, timeout=timeout,
                                        trust_env=False)

    async def aclose(self) -> None:
        await self._http.aclose()

    def redact(self, text: str) -> str:
        """``text`` without the token in force, for anything written down."""
        connection = self._connection
        if connection is not None and connection.token in text:
            return text.replace(connection.token, '***')
        return text

    async def call(self, method: str, path: str, *,
                   params: Optional[Mapping[str, Any]] = None,
                   json_body: Any = None) -> Any:
        """One request; the reply's JSON. Raises ``BackendError`` on an HTTP error."""
        response = await self._send(method, path, params=params, json_body=json_body)
        if response.status_code >= 400:
            raise _error_of(response)
        return response.json()

    async def get_text(self, path: str, *, params: Optional[Mapping[str, Any]] = None,
                       max_bytes: int) -> str:
        """A text reply, refused past ``max_bytes`` rather than held in memory."""
        response = await self._send('GET', path, params=params, stream=True)
        try:
            if response.status_code >= 400:
                await response.aread()
                raise _error_of(response)
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise BackendError(413, f"the reply is larger than {max_bytes} bytes")
                chunks.append(chunk)
            return b''.join(chunks).decode('utf-8', errors='replace')
        finally:
            await response.aclose()

    async def _send(self, method: str, path: str, *, params=None, json_body=None,
                    stream: bool = False) -> httpx2.Response:
        """Send once; on no answer or a 401, re-read the file and send once more."""
        for attempt in (1, 2):
            if self._connection is None or attempt == 2:
                self._connection = None
                connection = read_connection_file(self.connection_file)
            else:
                connection = self._connection
            request = self._http.build_request(
                method, connection.url + path, params=params, json=json_body,
                headers={'Authorization': f'Bearer {connection.token}'})
            try:
                response = await self._http.send(request, stream=stream)
            except httpx2.ConnectError:
                if attempt == 1:
                    continue
                raise BackendUnavailable(f"nothing answers at {connection.url}") from None
            audit.note_http_status(response.status_code)
            if response.status_code == 401:
                await response.aclose()
                if attempt == 1:
                    continue
                raise BackendUnavailable(f"the backend at {connection.url} refuses the agent "
                                         "token: agent access was turned off")
            if self._connection is None:
                logger.info("talking to the backend at %s (pid %s)", connection.url,
                            connection.pid)
            self._connection = connection
            return response
        raise AssertionError("unreachable")  # pragma: no cover


def _error_of(response: httpx2.Response) -> BackendError:
    """The backend's own ``detail``, kept whole: it tells the agent what to change."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and 'detail' in body:
        detail = body['detail']
    else:
        detail = response.text[:2000]
    return BackendError(response.status_code, detail)

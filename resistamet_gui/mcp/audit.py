"""One JSONL line per tool call (``docs/design/mcp_layer.md`` M7).

The data file records the run; this records what the agent asked for and
what it was told, so a run an agent started can be traced back to the
requests behind it. One file per UTC day,
``~/.resistamet/logs/mcp/<YYYY-MM-DD>.jsonl``. Each line holds the time, the
MCP client's name and version (from its ``initialize``), the tool, its
arguments, its result, the outcome, the last HTTP status the backend
answered, and the run it concerned, if any.

Large results are cut and marked, so a long event digest cannot grow the
log without bound; the data file is the record of the measurement, not
this. The agent's reasoning is not here either: it lives in the MCP
client's own transcript (``docs/mcp.md`` says how to keep it).

The token is never written. Nothing a tool returns contains it, and every
line is passed through the backend client's ``redact`` as well, in case.

Nothing here imports the MCP SDK: the middleware reads the request context
by attribute, so this module is tested, and runs, on its own.
"""
from __future__ import annotations

import contextvars
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

#: Above this many characters of JSON, a result or an argument set is cut.
CAP_CHARS = 4000


def default_audit_dir() -> str:
    return str(Path.home() / '.resistamet' / 'logs' / 'mcp')


class CallNote:
    """What a tool call learns on its way that its result may not show."""

    def __init__(self):
        self.http_status: Optional[int] = None
        self.run_id: Optional[str] = None


#: The note of the tool call in progress. The middleware sets a fresh one
#: before the call and reads it after; the call only changes its fields, so
#: they are seen even where the SDK runs the call in a copied context.
_current: contextvars.ContextVar[Optional[CallNote]] = contextvars.ContextVar(
    'resistamet_mcp_call', default=None)


def note_http_status(status: int) -> None:
    """Record the status of the backend's last reply. A no-op outside a tool call."""
    note = _current.get()
    if note is not None:
        note.http_status = status


def note_run_id(run_id: Optional[str]) -> None:
    """Record which run a tool call concerned. A no-op outside a tool call."""
    note = _current.get()
    if note is not None and run_id:
        note.run_id = run_id


def capped(value: Any, cap: int = CAP_CHARS) -> Any:
    """``value`` itself, or its JSON cut to ``cap`` characters and marked as cut."""
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)
    if len(text) <= cap:
        return value
    return {'truncated': True, 'chars': len(text), 'head': text[:cap]}


class AuditLog:
    """Appends audit lines to one file per UTC day under ``directory``.

    ``redact`` is applied to every line before it is written. A line that
    cannot be written is logged and dropped: the audit log must never be
    the reason a stop did not reach the instrument.
    """

    def __init__(self, directory: Optional[str] = None,
                 clock: Callable[[], float] = time.time,
                 redact: Optional[Callable[[str], str]] = None):
        self.directory = Path(directory or default_audit_dir())
        self._clock = clock
        self._redact = redact

    def path_for(self, when: float) -> Path:
        day = datetime.fromtimestamp(when, tz=timezone.utc).strftime('%Y-%m-%d')
        return self.directory / f"{day}.jsonl"

    def record(self, *, tool: str, arguments: Any, result: Any, outcome: str,
               http_status: Optional[int] = None, run_id: Optional[str] = None,
               client: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Write one line; return what was written (before redaction)."""
        when = self._clock()
        line = {
            'time': datetime.fromtimestamp(when, tz=timezone.utc).isoformat(),
            'client': client,
            'tool': tool,
            'arguments': capped(arguments),
            'outcome': outcome,
            'http_status': http_status,
            'run_id': run_id,
            'result': capped(result),
        }
        text = json.dumps(line, ensure_ascii=False, separators=(',', ':'), default=str)
        if self._redact is not None:
            text = self._redact(text)
        path = self.path_for(when)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write(text + '\n')
        except OSError as exc:
            logger.warning("could not write the MCP audit log %s: %s", path, exc)
        return line


class AuditMiddleware:
    """Server middleware: an audit line for every ``tools/call``.

    At this level the call is seen whole, including arguments the tool's
    schema refused before any tool code ran. The result arrives in its wire
    form, a dict with ``content``, ``structuredContent`` and ``isError``.
    """

    def __init__(self, log: AuditLog):
        self.log = log

    async def __call__(self, ctx, call_next):
        if getattr(ctx, 'method', None) != 'tools/call':
            return await call_next(ctx)
        params = getattr(ctx, 'params', None) or {}
        tool = str(params.get('name', ''))
        arguments = params.get('arguments') or {}
        note = CallNote()
        token = _current.set(note)
        try:
            try:
                result = await call_next(ctx)
            except Exception as exc:
                self._write(ctx, tool, arguments, note, 'error', f"{type(exc).__name__}: {exc}")
                raise
        finally:
            _current.reset(token)
        wire = _wire(result)
        failed = bool(wire.get('isError'))
        self._write(ctx, tool, arguments, note, 'error' if failed else 'ok',
                    _payload(wire), wire.get('structuredContent'))
        return result

    def _write(self, ctx, tool, arguments, note, outcome, result, structured=None):
        run_id = note.run_id or _run_id_in(arguments) or _run_id_in(structured)
        self.log.record(tool=tool, arguments=arguments, result=result, outcome=outcome,
                        http_status=note.http_status, run_id=run_id, client=client_of(ctx))


def client_of(ctx) -> Optional[Dict[str, Any]]:
    """The MCP client's name and version from its ``initialize``, if it sent them."""
    try:
        info = ctx.session.client_params.client_info
        return {'name': info.name, 'version': info.version}
    except AttributeError:
        return None


def _wire(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return result
    dump = getattr(result, 'model_dump', None)
    if dump is not None:
        return dump(by_alias=True, mode='json', exclude_none=True)
    return {}


def _payload(wire: Dict[str, Any]) -> Any:
    """What the agent was told: the structured result, else the text."""
    if wire.get('structuredContent') is not None:
        return wire['structuredContent']
    texts = [block.get('text', '') for block in wire.get('content') or []
             if isinstance(block, dict) and block.get('type') == 'text']
    return '\n'.join(texts)


def _run_id_in(value: Any) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    for candidate in (value.get('run_id'), (value.get('status') or {}).get('run_id')
                      if isinstance(value.get('status'), dict) else None):
        if isinstance(candidate, str) and candidate:
            return candidate
    return None

"""The MCP server is an HTTP client of the backend and nothing more.

``resistamet_gui.mcp`` may import itself, the standard library, the MCP SDK
and what the SDK itself brings (``mcp``, ``httpx2``, ``anyio``,
``pydantic``), and from the application only ``constants`` (the version)
and ``data_export`` (the file format it reads). Never ``session`` or
``api``: what an agent may do is what the backend allows its token, and an
import of either would be a way round that. Nor Qt or pyvisa, which would
give it a window or a bus of its own (``docs/design/mcp_layer.md`` §8).

Parsed, not imported, as for ``gpib_usb``: the scan is the same, and it
runs where the SDK is not installed.
"""
import pathlib
from typing import List, Optional, Tuple

import pytest

from .test_gpib_usb_self_contained import _is_stdlib, imports

PACKAGE = 'resistamet_gui.mcp'
PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent / 'resistamet_gui' / 'mcp'
THIRD_PARTY = ('mcp', 'httpx2', 'anyio', 'pydantic')
APPLICATION = ('resistamet_gui.constants', 'resistamet_gui.data_export')


def violation(name: str) -> Optional[str]:
    """Why an import breaks the rule, or None."""
    top = name.split('.')[0]
    if name == PACKAGE or name.startswith(PACKAGE + '.'):
        return None
    if top == 'resistamet_gui':
        if name in APPLICATION:
            return None
        return 'imports the application beyond constants and data_export'
    if top in THIRD_PARTY or _is_stdlib(top):
        return None
    return 'imports a package outside the standard library and the MCP SDK'


def _module_name(path: pathlib.Path) -> str:
    parts = path.relative_to(PACKAGE_DIR).with_suffix('').parts
    if parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join((PACKAGE,) + parts)


def _all_imports() -> List[Tuple[str, int, str]]:
    found = []
    for path in sorted(PACKAGE_DIR.rglob('*.py')):
        source = path.read_text(encoding='utf-8')
        for line, name, _ in imports(source, _module_name(path), path.name == '__init__.py'):
            found.append((path.name, line, name))
    return found


def test_the_package_imports_neither_the_session_nor_the_api():
    problems = ['%s:%d %s: %s' % (path, line, name, why)
                for path, line, name in _all_imports()
                for why in [violation(name)] if why]
    assert problems == []


def test_the_scan_sees_the_package_s_own_imports():
    # A parse that found nothing would pass the test above for the wrong reason.
    names = {name for _, _, name in _all_imports()}
    assert {'httpx2', 'mcp.server.mcpserver', 'resistamet_gui.mcp.client',
            'resistamet_gui.constants', 'json'} <= names


@pytest.mark.parametrize('source, module, why', [
    ('from ..session.manager import MeasurementSession', PACKAGE + '.tools', 'the application'),
    ('from ..api import agent_access', PACKAGE + '.client', 'the application'),
    ('import resistamet_gui.api.app', PACKAGE + '.client', 'the application'),
    ('from .. import config', PACKAGE + '.client', 'the application'),
    ('def f():\n    from resistamet_gui.session import events', PACKAGE + '.tools', 'the application'),
    ('import pyvisa', PACKAGE + '.tools', 'outside the standard library'),
    ('from PySide6 import QtCore', PACKAGE + '.tools', 'outside the standard library'),
    ('import fastapi', PACKAGE + '.tools', 'outside the standard library'),
])
def test_the_check_catches(source, module, why):
    reasons = [violation(name) for _, name, _ in imports(source, module, False)]
    assert any(reason and why in reason for reason in reasons), reasons


@pytest.mark.parametrize('source, module', [
    ('from .client import Backend', PACKAGE + '.tools'),
    ('from ..constants import __version__', PACKAGE + '.server'),
    ('from ..data_export import metadata_from_lines', PACKAGE + '.summary'),
    ('from mcp.server.mcpserver import MCPServer', PACKAGE + '.server'),
    ('import httpx2\nimport anyio\nfrom pydantic import Field', PACKAGE + '.client'),
])
def test_the_check_allows(source, module):
    reasons = [violation(name) for _, name, _ in imports(source, module, False)]
    assert reasons and all(reason is None for reason in reasons), reasons

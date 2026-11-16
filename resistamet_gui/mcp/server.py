"""The MCP server: the tools, the audit middleware and the instructions.

Built from the SDK's ``MCPServer``. Every ``tools/call`` passes through
``AuditMiddleware`` before anything else of ours runs, so a call the SDK
refused for bad arguments is logged as well as one that reached the
backend.
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..constants import __version__
from . import tools
from .audit import AuditLog, AuditMiddleware
from .client import Backend

#: What an MCP client may show the model before any tool is called.
INSTRUCTIONS = """\
ResistaMet drives a Keithley 2400-family sourcemeter through a backend that a \
person runs (the desktop app, or python -m resistamet_gui.api). You are a client \
of it with the agent role.

- Start with get_status. Call check_settings before every start_run.
- Values are SI: volts, amperes, ohms, seconds, hertz.
- A run you start is held to the profile's agent limits (by default 30 V; current \
and power left to the instrument). Only a person can change them.
- Prompts with requires_human (touch safety, van der Pauw rewiring) are answered \
by a person at the ResistaMet window, never by you. Tell the user what is asked.
- Follow a run with wait_for, not repeated get_status. Read results with \
get_run_summary; samples are summarised, never streamed into the conversation.
- Stopping is always allowed: stop_run ends any run, whoever started it.
"""


def build_server(backend: Backend, audit_log: AuditLog) -> MCPServer:
    """The server an MCP client talks to, over whichever transport runs it."""
    server = MCPServer(
        name='resistamet',
        title='ResistaMet',
        version=__version__,
        instructions=INSTRUCTIONS,
        middleware=[AuditMiddleware(audit_log)],
    )
    tools.register(server, backend)
    return server

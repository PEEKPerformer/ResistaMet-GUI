"""An MCP server that lets an AI agent drive ResistaMet through its API.

``python -m resistamet_gui.mcp`` is started by an MCP client (Claude Code,
Claude Desktop, ...) over stdio. It is an HTTP client of a backend that is
already running with agent access on -- the desktop app's sidecar, or
``python -m resistamet_gui.api --allow-agents`` -- and holds no instrument,
no run lock and no event ring of its own (``docs/design/mcp_layer.md`` M1).

So the package imports neither ``resistamet_gui.session`` nor
``resistamet_gui.api``, nor Qt or pyvisa: whatever the agent may do is what
the backend lets the ``agent`` token do, and nothing here can reach past it.
``tests/test_mcp_self_contained.py`` holds that line.

Modules: ``client`` (finding the backend, HTTP), ``tools`` (the tools, one
per route), ``audit`` (one JSONL line per tool call), ``server`` (the MCP
server that ties them together).
"""

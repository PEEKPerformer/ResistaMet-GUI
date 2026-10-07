"""``python -m resistamet_gui.mcp``: the MCP server, on stdio.

An MCP client starts this and talks JSON-RPC over its stdin and stdout, so
stdout belongs to the protocol: everything else, logs included, goes to
stderr. The server does not start a backend (``docs/design/mcp_layer.md``
M1); it finds the one that is running through the connection file.
"""
from __future__ import annotations

import argparse
import logging
import sys


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="python -m resistamet_gui.mcp",
        description="MCP server (stdio) that lets an AI agent drive a running ResistaMet "
                    "backend with agent access on.")
    parser.add_argument("--connection-file", default=None, metavar="PATH",
                        help="The backend's agent connection file (default: "
                             "~/.resistamet/api/connection.json).")
    parser.add_argument("--audit-dir", default=None, metavar="PATH",
                        help="Where the audit log of tool calls goes (default: "
                             "~/.resistamet/logs/mcp).")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # One line per request at INFO; the audit log already has every call.
    logging.getLogger('httpx2').setLevel(logging.WARNING)
    try:
        import anyio

        from .audit import AuditLog
        from .client import Backend
        from .server import build_server
    except ImportError as exc:
        print(f"The MCP server needs the 'mcp' extra (Python 3.10+): "
              f"pip install \"resistamet-gui[mcp]\" ({exc})", file=sys.stderr)
        return 2

    backend = Backend(args.connection_file)
    server = build_server(backend, AuditLog(args.audit_dir, redact=backend.redact))

    async def serve():
        try:
            await server.run_stdio_async()
        finally:
            await backend.aclose()

    anyio.run(serve)
    return 0


if __name__ == "__main__":
    sys.exit(main())

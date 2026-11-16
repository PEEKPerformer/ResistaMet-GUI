"""Localhost HTTP/WebSocket API over a MeasurementSession.

Imports FastAPI, so it is optional: install with ``pip install -e ".[api]"``.
Qt is never imported here — ``test_session_no_qt`` covers this package too.
"""
from .app import create_app

__all__ = ['create_app']

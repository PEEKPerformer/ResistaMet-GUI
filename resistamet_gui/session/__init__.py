"""Headless run layer: the events a run reports and, later, the run itself.

Qt-free by construction — ``test_session_no_qt`` enforces it — so the same
procedures drive the PySide6 adapters in ``workers.py`` and the API sidecar.
See ``docs/design/tauri_backend_split.md`` section 3.
"""
from .emitter import EventEmitter, ListSink
from .events import EVENT_SCHEMA_VERSION, PAYLOAD_MODELS, ErrorPayload, Event, LogPayload

__all__ = [
    'EVENT_SCHEMA_VERSION',
    'PAYLOAD_MODELS',
    'ErrorPayload',
    'Event',
    'EventEmitter',
    'ListSink',
    'LogPayload',
]

"""Typed settings contract for a measurement run.

Public surface of the package; see ``settings_common`` for the shared groups
and ``docs/design/tauri_backend_split.md`` section 4 for the design. Importing
this package must not pull in Qt, pyvisa or matplotlib — ``test_schema_no_qt``
enforces that, because the headless session and the API depend on it.
"""
from .resolve import Issue, ResolvedRun, allowed_override_keys, resolve_run_settings
from .settings_modes import (
    MODE_MODELS,
    ClientInfo,
    CurrentSourceSettings,
    FourPointSettings,
    ResistanceSettings,
    SweepSettings,
    RunRequest,
    VdpSettings,
    VoltageSourceSettings,
)
from .spots import SampleGeometry, SpotRequest
from .settings_common import (
    AuxSensorSettings,
    DisplaySettings,
    FileSettings,
    InstrumentSettings,
    OutputSettings,
    SafetySettings,
    SettingsModel,
)

__all__ = [
    'MODE_MODELS',
    'Issue',
    'ResolvedRun',
    'allowed_override_keys',
    'resolve_run_settings',
    'AuxSensorSettings',
    'ClientInfo',
    'CurrentSourceSettings',
    'DisplaySettings',
    'FileSettings',
    'FourPointSettings',
    'InstrumentSettings',
    'OutputSettings',
    'ResistanceSettings',
    'RunRequest',
    'SafetySettings',
    'SampleGeometry',
    'SettingsModel',
    'SpotRequest',
    'SweepSettings',
    'VdpSettings',
    'VoltageSourceSettings',
]

"""The schema package must stay importable without Qt.

The headless session, the API sidecar and any scripted client import the
settings contract; a stray ``from PySide6 import ...`` in it would drag a GUI
toolkit into a process that has no display. This test imports the package in a
subprocess with a clean interpreter and fails if Qt (or pyvisa) came along.
"""
import subprocess
import sys

PACKAGES = ['resistamet_gui.schema', 'resistamet_gui.schema.map_session']

CHECK = """
import sys
import {package}
leaked = sorted(m for m in sys.modules if m.split('.')[0] in ('PySide6', 'shiboken6', 'pyvisa', 'matplotlib'))
print(','.join(leaked))
"""


def _import_in_subprocess(package):
    result = subprocess.run(
        [sys.executable, '-c', CHECK.format(package=package)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return [name for name in result.stdout.strip().split(',') if name]


def test_no_gui_or_visa_import():
    for package in PACKAGES:
        leaked = _import_in_subprocess(package)
        assert leaked == [], f"{package} pulled in {leaked}"


def test_defaults_come_from_default_settings():
    """Every default is DEFAULT_SETTINGS by reference, not a re-typed literal.

    A copied literal would drift the day a default changes, and the drift
    would be invisible. Comparing the model's default against the dict is the
    cheap standing check that the single source held.
    """
    from resistamet_gui.constants import DEFAULT_SETTINGS
    from resistamet_gui.schema import (
        AuxSensorSettings, DisplaySettings, FileSettings, InstrumentSettings,
        OutputSettings, SafetySettings,
    )

    sections = {
        'measurement': [InstrumentSettings, AuxSensorSettings, SafetySettings],
        'file': [FileSettings],
        'output': [OutputSettings],
        'display': [DisplaySettings],
    }
    for section, models in sections.items():
        for model in models:
            instance = model()
            for name in model.model_fields:
                assert name in DEFAULT_SETTINGS[section], (
                    f"{model.__name__}.{name} has no DEFAULT_SETTINGS['{section}'] entry"
                )
                expected = DEFAULT_SETTINGS[section][name]
                actual = getattr(instance, name)
                if isinstance(expected, list):
                    assert list(actual) == list(expected)
                else:
                    assert actual == expected, f"{model.__name__}.{name}"

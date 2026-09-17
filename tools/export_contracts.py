"""Write the JSON Schema for the settings and event contracts.

The pydantic models are the single source of truth; these files are their
machine-readable form. Step 2 generates the Tauri UI's TypeScript types from
them, so a contract change shows up as a JSON diff next to the Python diff
instead of as a surprise in the frontend.

Usage::

    python tools/export_contracts.py

Writes ``contracts/settings.schema.json`` and ``contracts/events.schema.json``.
``tests/test_contracts_up_to_date.py`` fails when the committed files are
stale.
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

CONTRACT_DIR = REPO_ROOT / "contracts"


def settings_schema():
    from resistamet_gui.schema.settings_common import (
        AuxSensorSettings, DisplaySettings, FileSettings, InstrumentSettings,
        OutputSettings, SafetySettings,
    )
    from resistamet_gui.schema.settings_modes import MODE_MODELS, RunRequest

    models = {
        'InstrumentSettings': InstrumentSettings,
        'AuxSensorSettings': AuxSensorSettings,
        'SafetySettings': SafetySettings,
        'FileSettings': FileSettings,
        'OutputSettings': OutputSettings,
        'DisplaySettings': DisplaySettings,
        'RunRequest': RunRequest,
    }
    models.update({model.__name__: model for model in MODE_MODELS.values()})
    return {
        'modes': {mode: model.__name__ for mode, model in MODE_MODELS.items()},
        'definitions': {name: model.model_json_schema() for name, model in models.items()},
    }


def events_schema():
    from resistamet_gui.session.events import EVENT_SCHEMA_VERSION, Event, PAYLOAD_MODELS

    return {
        'version': EVENT_SCHEMA_VERSION,
        'envelope': Event.model_json_schema(),
        'payloads': {
            event_type: model.model_json_schema()
            for event_type, model in PAYLOAD_MODELS.items()
        },
    }


def render(schema):
    """Stable text: sorted keys, fixed indent, trailing newline."""
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main():
    CONTRACT_DIR.mkdir(exist_ok=True)
    for name, schema in (('settings', settings_schema()), ('events', events_schema())):
        path = CONTRACT_DIR / f"{name}.schema.json"
        path.write_text(render(schema))
        print(f"wrote {path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()

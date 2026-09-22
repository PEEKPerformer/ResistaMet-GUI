"""The committed contracts must match the models that generate them.

Stale contract files would ship the Tauri UI types for a schema the backend no
longer has. Regenerate with ``python tools/export_contracts.py``.
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import export_contracts  # noqa: E402

CONTRACTS = [
    ('settings', export_contracts.settings_schema),
    ('events', export_contracts.events_schema),
    ('maps', export_contracts.maps_schema),
    ('session', export_contracts.session_schema),
]


@pytest.mark.parametrize("name,builder", CONTRACTS, ids=[n for n, _ in CONTRACTS])
def test_committed_contract_matches_models(name, builder):
    path = REPO_ROOT / "contracts" / f"{name}.schema.json"
    assert path.exists(), f"{path} missing — run python tools/export_contracts.py"
    assert path.read_text() == export_contracts.render(builder()), (
        f"{path.name} is stale — run python tools/export_contracts.py"
    )


@pytest.mark.parametrize("name,builder", CONTRACTS, ids=[n for n, _ in CONTRACTS])
def test_export_is_deterministic(name, builder):
    assert export_contracts.render(builder()) == export_contracts.render(builder())


def test_every_mode_has_a_definition():
    schema = export_contracts.settings_schema()
    for mode, model_name in schema['modes'].items():
        assert model_name in schema['definitions'], mode


def test_event_payloads_are_exported():
    """Every modelled event type reaches the exported contract."""
    from resistamet_gui.session.events import PAYLOAD_MODELS

    schema = json.loads((REPO_ROOT / "contracts" / "events.schema.json").read_text())
    assert set(schema['payloads']) == set(PAYLOAD_MODELS)

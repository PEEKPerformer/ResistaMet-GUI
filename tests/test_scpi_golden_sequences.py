"""The configure sequence of every mode, pinned in order.

Every other ``command_log`` assertion in the suite is a membership or prefix
check (``":SYST:RSEN ON" in cmds``), so a reordering of the configure step
would pass them all, and on a 2400 the order is the behaviour: auto-ohms
rejects source commands until ``:SENS:RES:MODE MAN``, ``FORM:ELEM`` and the
range commands have their own quirks (see ``instrument.py``).

``tests/goldens/scpi/*.json`` each hold one mode and option combination: the
measurement settings, and the exact ordered list of writes from ``*RST`` to
the first ``:OUTP ON``. These sequences were checked identical to the
pre-refactor ``workers.py`` (413d8cf) by a differential run of 28 scenarios
against the fake instrument, from which these fourteen are the ones that
differ in how they configure.

A deliberate change to a sequence is bench work first. Then regenerate with
``REGENERATE_SCPI_GOLDENS=1 pytest tests/test_scpi_golden_sequences.py`` and
review the JSON diff line by line.
"""
import json
import os
from pathlib import Path

import pytest

from resistamet_gui.session.continuous_run import ContinuousRun
from resistamet_gui.session.control import RunControl
from resistamet_gui.session.emitter import EventEmitter
from resistamet_gui.session.vdp_run import VdpRun

GOLDEN_DIR = Path(__file__).parent / "goldens" / "scpi"
GOLDENS = sorted(GOLDEN_DIR.glob("*.json"))
REGENERATE = bool(os.environ.get("REGENERATE_SCPI_GOLDENS"))

assert GOLDENS, "no SCPI goldens found"

COMMON = {
    "sampling_rate": 50.0, "nplc": 1.0, "settling_time": 0.0,
    "gpib_address": "GPIB0::24::INSTR", "stop_on_compliance": False, "auto_zero": "on",
    "filter_enabled": False, "filter_type": "repeat", "filter_count": 10,
}


@pytest.fixture(autouse=True)
def _no_sleep_inhibitor(monkeypatch):
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)


def _writes_until_output_on(golden, fake_rm, tmp_path):
    settings = {
        "measurement": {**COMMON, **golden["measurement"]},
        "display": {"enable_plot": False, "plot_update_interval": 100, "buffer_size": 100},
        "file": {"auto_save_interval": 60, "data_directory": str(tmp_path / "data")},
        "output": {"format": "csv", "compression": "never", "compression_threshold_mb": 5},
    }
    control = RunControl()

    def sink(event):
        # The run is driven from its own event stream, on its own thread:
        # answer the first geometry prompt, stop at the first result.
        if event.type == 'prompt':
            control.answer_prompt(event.payload['prompt_id'], 'proceed')
        elif event.type in ('sample', 'sweep_segment', 'vdp_geometry_complete'):
            control.finish('user_stop')

    if golden["mode"] == 'vdp':
        run = VdpRun("wafer", "alice", settings, control, EventEmitter(sink))
    else:
        run = ContinuousRun(golden["mode"], "wafer", "alice", settings, control,
                             EventEmitter(sink))
    run.execute()

    writes = [cmd for fake in fake_rm.opened for op, cmd in fake.command_log if op == 'write']
    assert '*RST' in writes and ':OUTP ON' in writes, writes
    return writes[writes.index('*RST'):writes.index(':OUTP ON') + 1]


@pytest.mark.parametrize("path", GOLDENS, ids=[p.stem for p in GOLDENS])
def test_the_configure_sequence_is_exactly_this(path, fake_rm, tmp_path):
    golden = json.loads(path.read_text())
    writes = _writes_until_output_on(golden, fake_rm, tmp_path)
    if REGENERATE:
        golden["writes"] = writes
        path.write_text(json.dumps(golden, indent=2) + "\n")
    assert writes == golden["writes"]


def test_every_mode_has_a_golden():
    modes = {json.loads(path.read_text())["mode"] for path in GOLDENS}
    assert modes == {'resistance', 'source_v', 'source_i', 'four_point', 'sweep', 'vdp'}

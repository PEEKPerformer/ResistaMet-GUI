"""Events name the file that exists, not the one compression replaced."""
import os

import pytest

from resistamet_gui.session.continuous_run import ContinuousRun
from resistamet_gui.session.control import RunControl
from resistamet_gui.session.emitter import EventEmitter
from resistamet_gui.session.vdp_run import VdpRun


def _settings(tmp_path):
    return {
        "measurement": {
            "sampling_rate": 100.0, "nplc": 0.1, "settling_time": 0.0,
            "gpib_address": "GPIB0::24::INSTR", "stop_on_compliance": False,
            "auto_zero": "on", "filter_enabled": False,
            "fpp_current": 1e-4, "fpp_voltage_compliance": 5.0, "fpp_voltage_range_auto": True,
            "fpp_spacing_cm": 0.1, "fpp_thickness_um": 0.0, "fpp_alpha": 1.0,
            "fpp_k_factor": 4.532, "fpp_samples": 2, "fpp_model": "thin_film",
            "fpp_delta_mode": False, "fpp_power_warn_w": 1.0, "fpp_power_stop_w": 2.0,
            "fpp_stop_on_overpower": True,
            "vdp_current": 1e-3, "vdp_voltage_compliance": 5.0, "vdp_thickness_cm": 0.05,
            "vdp_settling_s": 0.0,
        },
        "display": {"enable_plot": False, "plot_update_interval": 100, "buffer_size": 100},
        "file": {"auto_save_interval": 60, "data_directory": str(tmp_path / "data")},
        "output": {"format": "csv", "compression": "always", "compression_threshold_mb": 5},
    }


@pytest.fixture(autouse=True)
def _no_sleep_inhibitor(monkeypatch):
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)


@pytest.mark.parametrize('kind', ['four_point', 'vdp'])
def test_a_compressed_run_reports_the_gz(kind, fake_rm, tmp_path):
    control = RunControl()
    events = []

    def sink(event):
        events.append(event)
        if event.type == 'prompt':
            control.answer_prompt(event.payload['prompt_id'], 'proceed')

    if kind == 'vdp':
        run = VdpRun('wafer1', 'alice', _settings(tmp_path), control, EventEmitter(sink))
    else:
        run = ContinuousRun(kind, 'wafer1', 'alice', _settings(tmp_path), control,
                             EventEmitter(sink))
    run.execute()

    def paths(event_type):
        return [e.payload['path'] for e in events if e.type == event_type]

    ended = paths('run_ended')[0]
    assert ended.endswith('.gz') and os.path.exists(ended)
    assert paths('file_finalized') == [ended]
    assert run.filename == ended
    if kind == 'four_point':
        assert paths('spot_complete') == [ended]
    # file_opened named the file as it was while the run wrote to it.
    assert paths('file_opened')[0] + '.gz' == ended

"""A fault inside the shutdown itself: once only, output off, run_ended last."""
import pytest

from resistamet_gui.data_export import CsvExporter
from resistamet_gui.session.continuous_run import ContinuousRun
from resistamet_gui.session.control import RunControl
from resistamet_gui.session.emitter import EventEmitter, ListSink


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
        },
        "display": {"enable_plot": False, "plot_update_interval": 100, "buffer_size": 100},
        "file": {"auto_save_interval": 60, "data_directory": str(tmp_path / "data")},
        "output": {"format": "csv", "compression": "never", "compression_threshold_mb": 5},
    }


@pytest.fixture(autouse=True)
def _no_sleep_inhibitor(monkeypatch):
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)


def _writes(fake_rm):
    return [cmd.upper() for op, cmd in fake_rm.opened[-1].command_log if op == 'write']


def test_a_finalize_that_raises(fake_rm, tmp_path, monkeypatch):
    finalize = CsvExporter.finalize
    calls = []

    def fail_with_a_footer(self, end_metadata=None):
        calls.append(end_metadata is not None)
        if end_metadata is not None:
            raise OSError("No space left on device")
        return finalize(self, end_metadata)
    monkeypatch.setattr(CsvExporter, 'finalize', fail_with_a_footer)

    sink = ListSink()
    ContinuousRun('four_point', 'wafer1', 'alice', _settings(tmp_path), RunControl(),
                   EventEmitter(sink)).execute()

    assert calls == [True, False], "the footer once, then cleanup's bare backstop"
    assert 'finalize_failed' in [e.payload['code'] for e in sink.of_type('log')]
    assert sink.of_type('file_finalized') == []
    assert sink.types().count('acquisition_finished') == 1
    assert sink.types()[-1] == 'run_ended'
    assert _writes(fake_rm)[-1] == ':OUTP OFF'


def test_a_sink_that_raises_during_the_shutdown(fake_rm, tmp_path):
    events = []

    def sink(event):
        events.append(event)
        if event.type in ('file_finalized', 'acquisition_finished'):
            raise RuntimeError("client went away")

    ContinuousRun('four_point', 'wafer1', 'alice', _settings(tmp_path), RunControl(),
                   EventEmitter(sink)).execute()

    types = [event.type for event in events]
    assert types.count('file_finalized') == 1
    assert types.count('acquisition_finished') == 1
    assert types[-1] == 'run_ended'
    assert events[-1].payload['reason'] == 'target_samples'
    assert _writes(fake_rm)[-1] == ':OUTP OFF'

"""Every way a run can end must report itself, exactly once, last.

These drive the procedures directly with a list sink — no Qt, no QThread — so
the event stream is what is under test rather than the adapter.
"""
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from resistamet_gui.session.continuous_run import ContinuousRun
from resistamet_gui.session.control import RunControl
from resistamet_gui.session.emitter import EventEmitter, ListSink


class _Outputs:
    """The remaining call-shaped facade; nothing routes through it any more."""

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _settings(tmp_path, **measurement):
    base = {
        "measurement": {
            "sampling_rate": 50.0, "nplc": 1.0, "settling_time": 0.0,
            "gpib_address": "GPIB0::24::INSTR", "stop_on_compliance": False,
            "auto_zero": "on", "filter_enabled": False, "filter_type": "repeat",
            "filter_count": 10,
            "res_test_current": 1e-3, "res_voltage_compliance": 5.0,
            "res_measurement_type": "4-wire", "res_auto_range": True,
            "res_offset_comp": False, "res_cable_null": 0.0,
        },
        "display": {"enable_plot": False, "plot_update_interval": 100, "buffer_size": 100},
        "file": {"auto_save_interval": 60, "data_directory": str(tmp_path / "data")},
        "output": {"format": "csv", "compression": "never", "compression_threshold_mb": 5},
    }
    base["measurement"].update(measurement)
    return base


def _run(tmp_path, mode='resistance', **measurement):
    control = RunControl()
    sink = ListSink()
    run = ContinuousRun(mode, "wafer1", "alice", _settings(tmp_path, **measurement),
                         control, _Outputs(), EventEmitter(sink))
    return run, control, sink


def _reasons(sink):
    return [e.payload['reason'] for e in sink.of_type('run_ended')]


@pytest.fixture(autouse=True)
def _no_sleep_inhibitor(monkeypatch):
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)


class TestRunEnded:
    def test_connect_failure_still_reports_the_end(self, tmp_path, monkeypatch):
        """No instrument, no file — the run still says why it stopped.

        The failure is forced rather than left to the environment: another
        test module may have installed the simulator process-wide, and a
        resistance run that connects has no duration to stop it.
        """
        from resistamet_gui.session import continuous_run as module

        def refuse(address):
            raise OSError(f"no instrument at {address}")

        monkeypatch.setattr(module, 'Keithley2400', refuse)
        run, control, sink = _run(tmp_path)
        run.execute()

        assert sink.types()[0] == 'run_started'
        assert sink.types()[-1] == 'run_ended'
        assert _reasons(sink) == ['connect_failed']
        assert len(sink.of_type('run_ended')) == 1
        assert sink.of_type('run_ended')[0].payload['ok'] is False

    def test_user_stop_wins_over_later_reasons(self, tmp_path):
        run, control, sink = _run(tmp_path)
        control.finish('user_stop')
        control.finish('duration')
        assert control.finish_reason == 'user_stop'

    def test_finish_stops_the_run(self, tmp_path):
        run, control, sink = _run(tmp_path)
        control.running = True
        control.finish('compliance_stop')
        assert control.running is False
        assert control.finish_reason == 'compliance_stop'

    def test_finish_wakes_a_waiting_prompt(self, tmp_path):
        """A stop must release a run parked on an operator decision."""
        _, control, _ = _run(tmp_path)
        control.raise_prompt('vdp_geometry', ['proceed', 'abort'])
        control.finish('user_stop')
        assert control.proceed_event.is_set()
        assert control.wait_for_prompt() is None


class TestRunEndedWithInstrument:
    """With the fake instrument, the ordinary completions report too."""

    def test_target_samples(self, fake_rm, tmp_path):
        run, control, sink = _run(tmp_path, mode='four_point', fpp_current=1e-4,
                                   fpp_voltage_compliance=5.0, fpp_voltage_range_auto=True,
                                   fpp_spacing_cm=0.1, fpp_thickness_um=0.0, fpp_alpha=1.0,
                                   fpp_k_factor=4.532, fpp_samples=2, fpp_model='thin_film',
                                   fpp_delta_mode=False, fpp_power_warn_w=1.0,
                                   fpp_power_stop_w=2.0, fpp_stop_on_overpower=True)
        run.execute()

        assert _reasons(sink) == ['target_samples']
        ended = sink.of_type('run_ended')[0].payload
        assert ended['ok'] is True
        assert ended['samples'] == 2
        assert ended['path'].endswith('.csv')

    def test_run_ended_is_last_and_follows_cleanup(self, fake_rm, tmp_path):
        run, control, sink = _run(tmp_path, mode='four_point', fpp_current=1e-4,
                                   fpp_voltage_compliance=5.0, fpp_voltage_range_auto=True,
                                   fpp_spacing_cm=0.1, fpp_thickness_um=0.0, fpp_alpha=1.0,
                                   fpp_k_factor=4.532, fpp_samples=1, fpp_model='thin_film',
                                   fpp_delta_mode=False, fpp_power_warn_w=1.0,
                                   fpp_power_stop_w=2.0, fpp_stop_on_overpower=True)
        run.execute()

        types = sink.types()
        assert types[-1] == 'run_ended'
        assert types.index('acquisition_finished') < types.index('run_ended')
        assert 'file_finalized' in types

    def test_write_failure_reports_write_error(self, fake_rm, tmp_path, monkeypatch):
        run, control, sink = _run(tmp_path, mode='four_point', fpp_current=1e-4,
                                   fpp_voltage_compliance=5.0, fpp_voltage_range_auto=True,
                                   fpp_spacing_cm=0.1, fpp_thickness_um=0.0, fpp_alpha=1.0,
                                   fpp_k_factor=4.532, fpp_samples=10, fpp_model='thin_film',
                                   fpp_delta_mode=False, fpp_power_warn_w=1.0,
                                   fpp_power_stop_w=2.0, fpp_stop_on_overpower=True)

        original_open = run._open_output_file

        def failing_open(*args, **kwargs):
            ok = original_open(*args, **kwargs)
            if ok:
                def boom(row):
                    raise OSError("disk full")
                run.exporter.write_row = boom
            return ok

        monkeypatch.setattr(run, '_open_output_file', failing_open)
        run.execute()

        assert _reasons(sink) == ['write_error']
        assert sink.of_type('run_ended')[0].payload['ok'] is False


class TestEarlyExitReasons:
    """An abandoned setup names itself rather than reporting 'completed'."""

    @pytest.mark.parametrize("reason,patch", [
        ('connect_failed', 'Keithley2400'),
    ])
    def test_setup_failures_are_named(self, tmp_path, monkeypatch, reason, patch):
        from resistamet_gui.session import continuous_run as module

        def refuse(*args, **kwargs):
            raise OSError("nope")

        monkeypatch.setattr(module, patch, refuse)
        run, control, sink = _run(tmp_path)
        run.execute()
        assert _reasons(sink) == [reason]

    def test_four_point_preflight_refusal(self, fake_rm, tmp_path):
        """Worst-case power above the hard stop must not read as completion."""
        run, control, sink = _run(tmp_path, mode='four_point', fpp_current=1e-3,
                                   fpp_voltage_compliance=100.0, fpp_voltage_range_auto=True,
                                   fpp_spacing_cm=0.1, fpp_thickness_um=0.0, fpp_alpha=1.0,
                                   fpp_k_factor=4.532, fpp_samples=1, fpp_model='thin_film',
                                   fpp_delta_mode=False, fpp_power_warn_w=0.01,
                                   fpp_power_stop_w=0.05, fpp_stop_on_overpower=True)
        run.execute()

        assert _reasons(sink) == ['power_envelope']
        assert sink.of_type('run_ended')[0].payload['ok'] is False


class TestDerivedValues:
    """A sample must carry the same 4PP numbers its CSV row does."""

    def _four_point(self, tmp_path, **overrides):
        settings = dict(fpp_current=1e-4, fpp_voltage_compliance=5.0,
                         fpp_voltage_range_auto=True, fpp_spacing_cm=0.1,
                         fpp_thickness_um=1.0, fpp_alpha=1.0, fpp_k_factor=4.532,
                         fpp_samples=2, fpp_model='thin_film', fpp_delta_mode=False,
                         fpp_power_warn_w=1.0, fpp_power_stop_w=2.0,
                         fpp_stop_on_overpower=True, fpp_diameter_cm=0.0,
                         fpp_geometry='circle', fpp_dopant_type='none')
        settings.update(overrides)
        return _run(tmp_path, mode='four_point', **settings)

    def _csv_rows(self, path):
        import csv
        with open(path) as handle:
            rows = [r for r in csv.reader(handle) if r and not r[0].startswith('#')]
        return rows[0], rows[1:]

    def test_derived_matches_the_csv_row(self, fake_rm, tmp_path):
        run, control, sink = self._four_point(tmp_path)
        run.execute()

        samples = sink.of_type('sample')
        assert samples, "no samples"
        header, rows = self._csv_rows(sink.of_type('run_ended')[0].payload['path'])
        for sample, row in zip(samples, rows):
            derived = sample.payload['derived']
            assert float(row[header.index('Rs_ohm_sq')]) == pytest.approx(derived['rs'])
            assert float(row[header.index('rho_ohm_cm')]) == pytest.approx(derived['rho'])
            assert float(row[header.index('sigma_S_cm')]) == pytest.approx(derived['sigma'])

    def test_method_names_the_correction_path(self, fake_rm, tmp_path):
        run, control, sink = self._four_point(tmp_path)
        run.execute()
        assert {s.payload['derived']['method'] for s in sink.of_type('sample')} == {'legacy'}

    def test_f84_path_is_labelled(self, fake_rm, tmp_path):
        run, control, sink = self._four_point(tmp_path, fpp_diameter_cm=5.0)
        run.execute()
        assert {s.payload['derived']['method'] for s in sink.of_type('sample')} == {'f84'}

    def test_other_modes_derive_nothing(self, fake_rm, tmp_path, monkeypatch):
        run, control, sink = _run(tmp_path, mode='source_v', vsource_voltage=0.1,
                                   vsource_current_compliance=0.1,
                                   vsource_current_range_auto=True,
                                   vsource_duration_hours=0.0)
        import threading
        stopper = threading.Timer(0.6, lambda: control.finish('user_stop'))
        stopper.start()
        run.execute()
        stopper.cancel()

        samples = sink.of_type('sample')
        assert samples, "no samples"
        assert all(s.payload.get('derived') is None for s in samples)

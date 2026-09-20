"""A mark is a record the run cannot reconstruct, so it must reach the file.

Two ways it did not: marks still queued when the run ended were dropped, and
marks were taken off the queue before the row write, so a row that failed to
write took its marks with it.
"""
import csv

import pytest

from resistamet_gui.data_export import CsvExporter, parse_metadata
from resistamet_gui.session.continuous_run import ContinuousRun
from resistamet_gui.session.control import RunControl
from resistamet_gui.session.emitter import EventEmitter, ListSink


def _settings(tmp_path, samples):
    return {
        "measurement": {
            "sampling_rate": 100.0, "nplc": 0.1, "settling_time": 0.0,
            "gpib_address": "GPIB0::24::INSTR", "stop_on_compliance": False,
            "auto_zero": "on", "filter_enabled": False,
            "fpp_current": 1e-4, "fpp_voltage_compliance": 5.0, "fpp_voltage_range_auto": True,
            "fpp_spacing_cm": 0.1, "fpp_thickness_um": 0.0, "fpp_alpha": 1.0,
            "fpp_k_factor": 4.532, "fpp_samples": samples, "fpp_model": "thin_film",
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


@pytest.fixture
def on_row(monkeypatch):
    """Run a callback around each row write: on_row[n] = (before, fails)."""
    write_row = CsvExporter.write_row
    plan = {}
    count = {'n': 0}

    def wrapped(self, row):
        count['n'] += 1
        before, fails, after = plan.get(count['n'], (None, False, None))
        if before:
            before()
        if fails:
            raise OSError("No space left on device")
        write_row(self, row)
        if after:
            after()
    monkeypatch.setattr(CsvExporter, 'write_row', wrapped)
    return plan


def _run(tmp_path, samples):
    control, sink = RunControl(), ListSink()
    run = ContinuousRun('four_point', 'wafer1', 'alice', _settings(tmp_path, samples), control,
                         EventEmitter(sink))
    return run, control, sink


def _event_column(path):
    with open(path, newline='') as handle:
        rows = list(csv.reader(line for line in handle if not line.startswith('#')))
    return [row[-1] for row in rows[1:]]


def _path(sink):
    return sink.of_type('run_ended')[0].payload['path']


class TestMarksLeftWhenTheRunEnds:
    def test_they_go_in_the_footer(self, fake_rm, tmp_path, on_row):
        run, control, sink = _run(tmp_path, samples=2)
        on_row[2] = (None, False, lambda: (control.mark_event('LIGHTS OFF'),
                                           control.mark_event('DOOR')))
        run.execute()

        assert _event_column(_path(sink)) == ['', '']
        assert parse_metadata(_path(sink))['marks_unwritten'] == 'LIGHTS OFF; DOOR'
        finalized = sink.of_type('file_finalized')[0].payload['end_metadata']
        assert finalized['marks_unwritten'] == 'LIGHTS OFF; DOOR'

    def test_a_run_with_none_left_has_no_such_key(self, fake_rm, tmp_path, on_row):
        run, control, sink = _run(tmp_path, samples=2)
        on_row[1] = (lambda: control.mark_event('A'), False, None)
        run.execute()
        assert 'marks_unwritten' not in parse_metadata(_path(sink))


class TestARowThatFailsToWrite:
    def test_its_marks_ride_on_the_next_row(self, fake_rm, tmp_path, on_row):
        run, control, sink = _run(tmp_path, samples=2)
        control.mark_event('PROBE MOVED')
        on_row[1] = (None, True, None)
        run.execute()

        assert _event_column(_path(sink))[0] == 'PROBE MOVED'
        assert 'marks_unwritten' not in parse_metadata(_path(sink))

    def test_a_mark_made_during_the_write_is_not_swept_away(self, fake_rm, tmp_path, on_row):
        """Only the marks the row carried are taken off the queue."""
        run, control, sink = _run(tmp_path, samples=3)
        control.mark_event('FIRST')
        on_row[1] = (lambda: control.mark_event('SECOND'), False, None)
        run.execute()
        assert _event_column(_path(sink))[:2] == ['FIRST', 'SECOND']

    def test_a_written_mark_is_written_once(self, fake_rm, tmp_path, on_row):
        run, control, sink = _run(tmp_path, samples=3)
        control.mark_event('ONCE')
        run.execute()
        assert _event_column(_path(sink)) == ['ONCE', '', '']

"""A sweep that fails says so, and a row that cannot be written is not silent."""
import pytest
import pyvisa

from resistamet_gui.session.continuous_run import ContinuousRun
from resistamet_gui.session.control import RunControl
from resistamet_gui.session.emitter import EventEmitter, ListSink


def _settings(tmp_path, **measurement):
    base = {
        "measurement": {
            "sampling_rate": 100.0, "nplc": 0.1, "settling_time": 0.0,
            "gpib_address": "GPIB0::24::INSTR", "stop_on_compliance": False,
            "auto_zero": "on", "filter_enabled": False,
            "sweep_source": "voltage", "sweep_start": 0.0, "sweep_stop": 1.0,
            "sweep_step": 0.2, "sweep_compliance": 0.1, "sweep_direction": "up",
            "sweep_delay": 0.0,
        },
        "display": {"enable_plot": False, "plot_update_interval": 100, "buffer_size": 100},
        "file": {"auto_save_interval": 60, "data_directory": str(tmp_path / "data")},
        "output": {"format": "csv", "compression": "never", "compression_threshold_mb": 5},
    }
    base["measurement"].update(measurement)
    return base


@pytest.fixture(autouse=True)
def _no_sleep_inhibitor(monkeypatch):
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)


def _sweep(tmp_path, **measurement):
    sink = ListSink()
    ContinuousRun('sweep', 'diode', 'alice', _settings(tmp_path, **measurement), RunControl(),
                   EventEmitter(sink)).execute()
    return sink


def _ended(sink):
    ended = sink.of_type('run_ended')[0].payload
    return ended['reason'], ended['ok']


@pytest.fixture
def failing_rows(monkeypatch):
    """Make the chosen row indexes fail to write; returns the set to fill."""
    from resistamet_gui.data_export import CsvExporter
    write_row = CsvExporter.write_row
    failing = set()

    def maybe_fail(self, row):
        if row[0] in failing:
            raise OSError("No space left on device")
        return write_row(self, row)
    monkeypatch.setattr(CsvExporter, 'write_row', maybe_fail)
    return failing


class TestASweepWhoseReadFails:
    def test_it_is_not_reported_as_completed(self, fake_rm, tmp_path, monkeypatch):
        from resistamet_gui._simulator import FakeKeithley
        query = FakeKeithley.query

        def time_out_the_sweep(self, cmd):
            if cmd.strip().upper() == ':READ?':
                raise pyvisa.errors.VisaIOError(-1073807339)     # VI_ERROR_TMO
            return query(self, cmd)
        monkeypatch.setattr(FakeKeithley, 'query', time_out_the_sweep)

        sink = _sweep(tmp_path)
        assert _ended(sink) == ('sweep_error', False)
        assert [e.payload['code'] for e in sink.of_type('error')] == ['sweep_error']
        writes = [cmd for op, cmd in fake_rm.opened[-1].command_log if op == 'write']
        assert writes[-1].upper() == ':OUTP OFF'

    def test_a_sweep_that_works_still_completes(self, fake_rm, tmp_path):
        assert _ended(_sweep(tmp_path)) == ('completed', True)


class TestASweepRowThatCannotBeWritten:
    def test_one_lost_row_is_named_and_the_run_goes_on(self, fake_rm, tmp_path, failing_rows):
        failing_rows.add(2)
        sink = _sweep(tmp_path)

        warnings = [e.payload['message'] for e in sink.of_type('log')
                    if e.payload['code'] == 'write_failed']
        assert len(warnings) == 1 and 'row 2' in warnings[0]
        assert _ended(sink) == ('completed', True)
        assert sink.of_type('run_ended')[0].payload['samples'] == 5
        assert len(sink.of_type('sweep_segment')[0].payload['voltages']) == 6

    def test_three_in_a_row_is_an_error_as_in_the_sampling_loop(self, fake_rm, tmp_path,
                                                                 failing_rows):
        failing_rows.update({1, 2, 3, 4, 5})
        sink = _sweep(tmp_path)

        assert _ended(sink) == ('write_error', False)
        errors = [e.payload for e in sink.of_type('error')]
        assert [e['code'] for e in errors] == ['write_failed']
        assert 'row 3' in errors[0]['message']
        # The points were measured: a client still gets them.
        assert len(sink.of_type('sweep_segment')[0].payload['voltages']) == 6

    def test_the_reverse_leg_counts_its_rows_on(self, fake_rm, tmp_path, failing_rows):
        failing_rows.add(8)
        sink = _sweep(tmp_path, sweep_direction='up_down')
        warnings = [e.payload['message'] for e in sink.of_type('log')
                    if e.payload['code'] == 'write_failed']
        assert len(warnings) == 1 and 'row 8' in warnings[0]

"""A van der Pauw file is closed properly however the run ends.

The continuous run got this first: output off, a footer that says why the run
ended, one file_finalized, run_ended last. A stopped or failed vdP run fell
through to the silent backstop in cleanup and left a file with no footer,
which reads afterwards like a run that completed.
"""
import threading
import time

import pytest

from resistamet_gui.data_export import parse_metadata
from resistamet_gui.session.control import RunControl
from resistamet_gui.session.emitter import EventEmitter, ListSink
from resistamet_gui.session.vdp_run import VdpRun


def _settings(tmp_path):
    return {
        "measurement": {
            "nplc": 1.0, "gpib_address": "GPIB0::24::INSTR", "auto_zero": "on",
            "filter_enabled": False,
            "vdp_current": 1e-3, "vdp_voltage_compliance": 5.0,
            "vdp_voltage_range_auto": True, "vdp_settling_s": 0.0,
            "vdp_readings_per_polarity": 1, "vdp_thickness_cm": 0.05,
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


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class _Driven:
    """A vdP run on a thread, with the operator's Measure button."""

    def __init__(self, tmp_path, prompt_timeout_s=None):
        self.control, self.sink = RunControl(), ListSink()
        self.run = VdpRun('wafer1', 'alice', _settings(tmp_path), self.control,
                           EventEmitter(self.sink), prompt_timeout_s=prompt_timeout_s)
        self.thread = threading.Thread(target=self.run.execute, daemon=True)
        self.thread.start()

    def measure(self, geometries):
        for done in range(geometries):
            assert _wait_for(lambda: self.control.pending_prompt is not None)
            self.run.proceed()
            assert _wait_for(
                lambda: len(self.sink.of_type('vdp_geometry_complete')) == done + 1)

    def wait_for_prompt(self):
        assert _wait_for(lambda: self.control.pending_prompt is not None)

    def join(self):
        self.thread.join(10.0)
        assert not self.thread.is_alive()
        return self.sink


def _check_closed_properly(sink, fake_rm, reason, rows):
    ended = sink.of_type('run_ended')[0].payload
    assert (ended['reason'], ended['samples']) == (reason, rows)
    footer = parse_metadata(ended['path'])
    assert footer['end_reason'] == reason
    assert footer['total_samples'] == rows
    assert 'ended_at' in footer and 'duration_s' in footer
    types = sink.types()
    assert types.count('file_finalized') == 1
    assert types.index('file_finalized') < types.index('run_ended')
    assert types[-1] == 'run_ended'
    assert sink.of_type('file_finalized')[0].payload['path'] == ended['path']
    writes = [cmd.upper() for op, cmd in fake_rm.opened[-1].command_log if op == 'write']
    assert writes[-1] == ':OUTP OFF'
    return footer


class TestEveryExitAfterTheFileIsOpen:
    def test_a_stop_after_the_first_geometry(self, fake_rm, tmp_path):
        driven = _Driven(tmp_path)
        driven.measure(1)
        driven.wait_for_prompt()
        driven.run.stop_measurement()
        footer = _check_closed_properly(driven.join(), fake_rm, 'user_stop', rows=1)
        assert not any(key.startswith('vdp_result') for key in footer)

    def test_no_answer_at_a_geometry(self, fake_rm, tmp_path):
        driven = _Driven(tmp_path, prompt_timeout_s=0.05)
        _check_closed_properly(driven.join(), fake_rm, 'prompt_timeout', rows=0)

    def test_a_read_that_fails(self, fake_rm, tmp_path, monkeypatch):
        from resistamet_gui._simulator import FakeKeithley
        query = FakeKeithley.query
        reads = {'n': 0}

        def fail_the_third_read(self, cmd):
            if cmd.strip().upper() == ':READ?':
                reads['n'] += 1
                if reads['n'] == 3:
                    raise RuntimeError("something nobody planned for")
            return query(self, cmd)
        monkeypatch.setattr(FakeKeithley, 'query', fail_the_third_read)

        driven = _Driven(tmp_path)
        driven.measure(1)
        driven.wait_for_prompt()
        driven.run.proceed()
        sink = driven.join()
        _check_closed_properly(sink, fake_rm, 'worker_error', rows=1)
        assert sink.of_type('run_ended')[0].payload['ok'] is False

    def test_a_run_that_completes(self, fake_rm, tmp_path):
        driven = _Driven(tmp_path)
        driven.measure(4)
        sink = driven.join()
        footer = _check_closed_properly(sink, fake_rm, 'completed', rows=4)
        assert footer['vdp_result.sheet_resistance'] == pytest.approx(
            sink.of_type('vdp_result')[0].payload['sheet_resistance'])
        # The result is in the file before anyone is told about it.
        types = sink.types()
        assert types.index('file_finalized') < types.index('vdp_result')

    def test_a_stop_before_the_file_is_open_finalizes_nothing(self, fake_rm, tmp_path,
                                                               monkeypatch):
        from resistamet_gui.session import vdp_run

        def refuse(address, visa_library='', gpib_interface=''):
            raise OSError(f"no instrument at {address}")
        monkeypatch.setattr(vdp_run, 'Keithley2400', refuse)
        sink = _Driven(tmp_path).join()
        assert sink.of_type('file_finalized') == []
        assert sink.types()[-1] == 'run_ended'


class TestTheFileIsAnnounced:
    def test_file_opened_is_sent_like_every_other_mode(self, fake_rm, tmp_path):
        driven = _Driven(tmp_path)
        driven.wait_for_prompt()
        driven.run.stop_measurement()
        sink = driven.join()

        opened = [e.payload for e in sink.of_type('file_opened')]
        assert len(opened) == 1
        assert opened[0]['path'] == sink.of_type('run_ended')[0].payload['path']
        assert len(opened[0]['columns']) == len(opened[0]['units']) > 0
        types = sink.types()
        assert types.index('file_opened') < types.index('prompt')

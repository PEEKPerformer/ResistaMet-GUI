"""A stop that arrives before the output is on keeps it off.

execute() used to re-arm ``running`` on entry, and nothing looked at the stop
between there and :OUTP ON, so a stop pressed during the seconds a real GPIB
connect and configure take still energised the leads, created a data file
for a run nobody wanted, and let a sweep run to its last point.
"""
import glob
import os

import pytest

from resistamet_gui.session import continuous_run, vdp_run
from resistamet_gui.session.continuous_run import ContinuousRun
from resistamet_gui.session.control import RunControl
from resistamet_gui.session.emitter import EventEmitter, ListSink
from resistamet_gui.session.vdp_run import VdpRun


def _settings(tmp_path, **measurement):
    base = {
        "measurement": {
            "sampling_rate": 100.0, "nplc": 0.1, "settling_time": 0.0,
            "gpib_address": "GPIB0::24::INSTR", "stop_on_compliance": False,
            "auto_zero": "on", "filter_enabled": False,
            "fpp_current": 1e-4, "fpp_voltage_compliance": 5.0, "fpp_voltage_range_auto": True,
            "fpp_spacing_cm": 0.1, "fpp_thickness_um": 0.0, "fpp_alpha": 1.0,
            "fpp_k_factor": 4.532, "fpp_samples": 3, "fpp_model": "thin_film",
            "fpp_delta_mode": False, "fpp_power_warn_w": 1.0, "fpp_power_stop_w": 2.0,
            "fpp_stop_on_overpower": True,
            "sweep_source": "voltage", "sweep_start": 0.0, "sweep_stop": 1.0,
            "sweep_step": 0.25, "sweep_compliance": 0.1, "sweep_direction": "up_down",
            "sweep_delay": 0.0,
            "vdp_current": 1e-3, "vdp_voltage_compliance": 5.0, "vdp_thickness_cm": 0.05,
            "vdp_settling_s": 0.0,
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


def _build(kind, tmp_path):
    control, sink = RunControl(), ListSink()
    if kind == 'vdp':
        run = VdpRun('wafer1', 'alice', _settings(tmp_path), control, EventEmitter(sink))
    else:
        run = ContinuousRun(kind, 'wafer1', 'alice', _settings(tmp_path), control,
                             EventEmitter(sink))
    return run, control, sink


def _writes(fake_rm):
    return [cmd.upper() for fake in fake_rm.opened
            for op, cmd in fake.command_log if op == 'write']


def _files(tmp_path):
    return [p for p in glob.glob(str(tmp_path / 'data' / '**' / '*'), recursive=True)
            if os.path.isfile(p)]


def _ended(sink):
    return [(e.payload['reason'], e.payload['ok']) for e in sink.of_type('run_ended')]


KINDS = ['four_point', 'sweep', 'vdp']


@pytest.mark.parametrize('kind', KINDS)
class TestAStopBeforeTheRunBegins:
    def test_nothing_is_touched(self, fake_rm, tmp_path, kind):
        run, control, sink = _build(kind, tmp_path)
        control.finish('user_stop')
        run.execute()

        assert sink.types() == ['run_started', 'run_ended']
        assert _ended(sink) == [('user_stop', False)]
        assert fake_rm.opened == []
        assert _files(tmp_path) == []
        assert control.running is False

    def test_an_abort_reads_as_one(self, fake_rm, tmp_path, kind):
        run, control, sink = _build(kind, tmp_path)
        control.finish('aborted')
        run.execute()
        assert _ended(sink) == [('aborted', False)]
        assert fake_rm.opened == []


@pytest.mark.parametrize('kind', KINDS)
class TestAStopWhileTheInstrumentIsBeingOpened:
    def test_during_the_wait_for_the_lock(self, fake_rm, tmp_path, kind, monkeypatch):
        run, control, sink = _build(kind, tmp_path)
        module = vdp_run if kind == 'vdp' else continuous_run
        held = module.HeldInstrument

        def stop_while_waiting(address):
            control.finish('user_stop')
            return held(address)
        monkeypatch.setattr(module, 'HeldInstrument', stop_while_waiting)
        run.execute()

        assert _ended(sink) == [('user_stop', False)]
        assert fake_rm.opened == [], "no connect after a stop"
        assert _files(tmp_path) == []

    def test_during_the_connect(self, fake_rm, tmp_path, kind, monkeypatch):
        run, control, sink = _build(kind, tmp_path)
        module = vdp_run if kind == 'vdp' else continuous_run
        connect = module.Keithley2400.connect

        def stop_while_connecting(self):
            connected = connect(self)
            control.finish('user_stop')
            return connected
        monkeypatch.setattr(module.Keithley2400, 'connect', stop_while_connecting)
        run.execute()

        assert [reason for reason, _ in _ended(sink)] == ['user_stop']
        writes = _writes(fake_rm)
        assert not any(cmd.startswith(':OUTP ON') for cmd in writes), writes
        assert _files(tmp_path) == [], "nothing was measured, so there is no file"
        assert sink.of_type('sample') == [] and sink.of_type('sweep_segment') == []
        assert sink.types()[-1] == 'run_ended'


class TestAStopBetweenTheTwoLegsOfASweep:
    def test_the_reverse_leg_is_not_run(self, fake_rm, tmp_path, monkeypatch):
        from resistamet_gui._simulator import FakeKeithley
        run, control, sink = _build('sweep', tmp_path)
        query = FakeKeithley.query

        def stop_after_the_first_read(self, cmd):
            reply = query(self, cmd)
            if cmd.strip().upper() == ':READ?':
                control.finish('user_stop')
            return reply
        monkeypatch.setattr(FakeKeithley, 'query', stop_after_the_first_read)
        run.execute()

        reads = [cmd for fake in fake_rm.opened for op, cmd in fake.command_log
                 if op == 'query' and cmd.strip().upper() == ':READ?']
        assert len(reads) == 1
        writes = _writes(fake_rm)
        assert writes.count(':OUTP ON') == 1
        assert writes[-1] == ':OUTP OFF'
        assert _ended(sink) == [('user_stop', True)]
        # The forward leg was measured and is kept, with its footer.
        path = sink.of_type('run_ended')[0].payload['path']
        with open(path) as handle:
            text = handle.read()
        assert 'total_samples' in text
        assert len(sink.of_type('file_finalized')) == 1

"""run_ended is the last event of a run: nothing is stamped after it.

stop_measurement() emits 'stopping' and a log line from the caller's thread,
whatever state the run is in, so a stop that arrived as the run was ending
put two events after run_ended. A client that closes on run_ended, or
resumes from the last seq it saw, was told the wrong thing.
"""
import pytest
from pydantic import ValidationError

from resistamet_gui.session.continuous_run import ContinuousRun
from resistamet_gui.session.control import RunControl
from resistamet_gui.session.emitter import EventEmitter, ListSink
from resistamet_gui.session.vdp_run import VdpRun

RUN_ENDED = {'reason': 'completed', 'ok': True, 'samples': 0, 'duration_s': 0.0, 'path': None}


class TestTheEmitter:
    def test_nothing_is_delivered_or_numbered_after_run_ended(self):
        sink = ListSink()
        emitter = EventEmitter(sink)
        emitter.log('connecting', 'x')
        ended = emitter.emit('run_ended', RUN_ENDED)
        assert emitter.emit('stopping', {'reason': 'user_stop'}) is None
        assert emitter.log('stopping', 'too late') is None
        assert emitter.error('worker_error', 'run', 'too late') is None
        assert sink.types() == ['log', 'run_ended']
        assert ended.seq == 2

    def test_a_run_ended_that_fails_validation_does_not_close_it(self):
        sink = ListSink()
        emitter = EventEmitter(sink)
        with pytest.raises(ValidationError):
            emitter.emit('run_ended', {'reason': None})
        emitter.emit('run_ended', RUN_ENDED)
        assert sink.types() == ['run_ended']

    def test_another_run_s_emitter_is_not_affected(self):
        first, second = ListSink(), ListSink()
        EventEmitter(first).emit('run_ended', RUN_ENDED)
        EventEmitter(second).log('connecting', 'x')
        assert second.types() == ['log']


def _refuse(address, visa_library='', gpib_interface=''):
    raise OSError(f"no instrument at {address}")


@pytest.mark.parametrize('kind', ['continuous', 'vdp'])
def test_a_stop_after_the_run_has_ended_adds_nothing(kind, tmp_path, monkeypatch):
    from resistamet_gui.session import continuous_run, vdp_run
    monkeypatch.setattr(continuous_run, 'Keithley2400', _refuse)
    monkeypatch.setattr(vdp_run, 'Keithley2400', _refuse)
    settings = {'measurement': {'gpib_address': '', 'vdp_current': 1e-3,
                                 'vdp_voltage_compliance': 5.0},
                'file': {'data_directory': str(tmp_path)}}
    sink = ListSink()
    if kind == 'vdp':
        run = VdpRun('wafer1', 'alice', settings, RunControl(), EventEmitter(sink))
    else:
        run = ContinuousRun('resistance', 'wafer1', 'alice', settings, RunControl(),
                             EventEmitter(sink))
    run.execute()
    assert sink.types()[-1] == 'run_ended'

    run.stop_measurement()
    assert sink.types()[-1] == 'run_ended'
    assert sink.types().count('stopping') == 0

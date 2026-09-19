"""The instrument lock always comes back, and comes back last.

The lock is per OS file handle, so a handle this process leaks refuses this
process's own next run with "in use by another ResistaMet process" until the
program is restarted. Every way a run can die has to give it back.
"""
import copy
import threading
import time

import pytest

from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.session.continuous_run import ContinuousRun
from resistamet_gui.session.control import RunControl
from resistamet_gui.session.emitter import EventEmitter, ListSink
from resistamet_gui.session.instrument_lock import InstrumentBusy, hold_instrument
from resistamet_gui.session.manager import MeasurementSession
from resistamet_gui.session.vdp_run import VdpRun

ADDRESS = 'GPIB0::24::INSTR'


@pytest.fixture
def profile(tmp_path):
    settings = copy.deepcopy({key: DEFAULT_SETTINGS[key]
                              for key in ('measurement', 'display', 'file', 'output')})
    settings['file']['data_directory'] = str(tmp_path / 'data')
    settings['measurement'].update({
        'gpib_address': ADDRESS, 'sampling_rate': 50.0, 'settling_time': 0.0,
        'fpp_current': 1e-4, 'fpp_voltage_compliance': 5.0, 'fpp_samples': 2,
        'fpp_power_warn_w': 1.0, 'fpp_power_stop_w': 2.0,
        'vdp_thickness_cm': 0.05, 'vdp_settling_s': 0.0,
    })
    return settings


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


def _lock_is_free():
    try:
        with hold_instrument(ADDRESS, wait_s=0):
            return True
    except InstrumentBusy:
        return False


def _boom(*args, **kwargs):
    raise RuntimeError("boom")


def _ended(sink):
    return [(e.payload['reason'], e.payload['ok']) for e in sink.of_type('run_ended')]


class TestARunThatDiesBeforeItsInstrument:
    """The steps before the connect used to run outside execute()'s try."""

    def _continuous(self, profile):
        sink = ListSink()
        return ContinuousRun('four_point', 'wafer1', 'alice', profile, RunControl(),
                              EventEmitter(sink)), sink

    def _vdp(self, profile):
        sink = ListSink()
        return VdpRun('wafer1', 'alice', profile, RunControl(), EventEmitter(sink)), sink

    @pytest.mark.parametrize('step', ['_safety_prompt_declined', '_spot_refused'])
    def test_a_continuous_run(self, fake_rm, profile, monkeypatch, step):
        run, sink = self._continuous(profile)
        monkeypatch.setattr(run, step, _boom)
        run.execute()
        assert _ended(sink) == [('worker_error', False)]
        assert sink.types()[-1] == 'run_ended'
        assert _lock_is_free()
        assert fake_rm.opened == [], "nothing was connected"

    def test_a_van_der_pauw_run(self, fake_rm, profile, monkeypatch):
        run, sink = self._vdp(profile)
        monkeypatch.setattr(run, '_safety_prompt_declined', _boom)
        run.execute()
        assert _ended(sink) == [('worker_error', False)]
        assert sink.types()[-1] == 'run_ended'
        assert _lock_is_free()

    @pytest.mark.parametrize('build', ['_continuous', '_vdp'])
    def test_a_run_started_that_cannot_be_sent(self, fake_rm, profile, build):
        run, sink = getattr(self, build)(profile)
        run.sample_name = None   # the run_started payload requires a string
        run.execute()
        assert _ended(sink) == [('worker_error', False)]
        assert _lock_is_free()

    @pytest.mark.parametrize('build', ['_continuous', '_vdp'])
    def test_a_refusal_still_reads_as_one(self, fake_rm, profile, build):
        """Moving the steps inside the try must not change what a refusal says."""
        profile['measurement'].update({'fpp_voltage_compliance': 60.0,
                                        'vdp_voltage_compliance': 60.0})
        run, sink = getattr(self, build)(profile)
        run._safety_ack = 'prompt'
        run._prompt_timeout_s = 0.05
        run.execute()
        assert _ended(sink) == [('prompt_timeout', False)]
        ended = sink.of_type('run_ended')[0].payload
        assert (ended['samples'], ended['duration_s'], ended['path']) == (0, 0.0, None)
        assert sink.types()[0] == 'run_started' and sink.types()[-1] == 'run_ended'
        assert _lock_is_free()
        assert fake_rm.opened == []

    def test_an_instrument_held_elsewhere(self, fake_rm, profile):
        run, sink = self._continuous(profile)
        from resistamet_gui.session import continuous_run
        with hold_instrument(ADDRESS, wait_s=0):
            original = continuous_run.HeldInstrument
            continuous_run.HeldInstrument = lambda address: original(address, wait_s=0)
            try:
                run.execute()
            finally:
                continuous_run.HeldInstrument = original
        assert _ended(sink) == [('instrument_busy', False)]
        assert [e.payload['code'] for e in sink.of_type('error')] == ['instrument_busy']
        assert fake_rm.opened == []


class TestTheSessionIsTheLastResort:
    """Whatever the run object does, the session ends idle with the bus free."""

    @pytest.fixture
    def sink(self):
        return ListSink()

    @pytest.fixture
    def session(self, sink):
        made = MeasurementSession(sink)
        yield made
        made.close(timeout=5.0)

    def test_a_run_that_raises_outright(self, session, sink, fake_rm, profile, monkeypatch):
        monkeypatch.setattr(ContinuousRun, 'execute', _boom)
        run_id = session.start(profile, 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')
        session._thread.join(5.0)

        assert _ended(sink) == [('worker_error', False)]
        assert sink.events[-1].type == 'run_ended'
        assert sink.events[-1].run_id == run_id
        assert [e.payload['code'] for e in sink.of_type('error')] == ['worker_error']
        assert _lock_is_free()

    def test_the_next_start_is_not_refused(self, session, sink, fake_rm, profile, monkeypatch):
        with monkeypatch.context() as patch:
            patch.setattr(ContinuousRun, 'execute', _boom)
            session.start(profile, 'four_point', 'wafer1', 'alice')
            assert _wait_for(lambda: session.state == 'idle')

        session.start(profile, 'four_point', 'wafer2', 'alice')
        assert _wait_for(lambda: len(sink.of_type('run_ended')) == 2)
        assert _ended(sink)[-1] == ('target_samples', True)

    def test_a_run_that_ended_itself_is_not_ended_twice(self, session, sink, fake_rm, profile):
        session.start(profile, 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')
        session._thread.join(5.0)
        assert _ended(sink) == [('target_samples', True)]

    def test_a_thread_that_will_not_start(self, session, sink, fake_rm, profile, monkeypatch):
        with monkeypatch.context() as patch:
            patch.setattr(threading.Thread, 'start', _boom)
            with pytest.raises(RuntimeError):
                session.start(profile, 'four_point', 'wafer1', 'alice')
        assert session.state == 'idle'
        assert session.status()['run_id'] is None
        assert _lock_is_free()

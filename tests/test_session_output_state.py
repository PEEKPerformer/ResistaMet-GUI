"""Whether the instrument output is off when a run ends is said, not assumed.

A run whose link to the instrument died mid-run cannot send ``:OUTP OFF``.
The cleanup used to note that as a generic warning and the source stayed on,
driving the sample, until the next session happened to address it. Now the
cleanup reads the output state back, warns with its own code when it cannot
confirm it off, and ``run_ended`` carries ``output_verified``.
"""
import copy
import time

import pyvisa
import pytest

from resistamet_gui._simulator import FakeKeithley
from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.session.continuous_run import ContinuousRun
from resistamet_gui.session.control import RunControl
from resistamet_gui.session.emitter import EventEmitter, ListSink
from resistamet_gui.session.manager import MeasurementSession

ADDRESS = 'GPIB0::24::INSTR'
UNVERIFIED = "Instrument output may still be ON — check the front panel."


@pytest.fixture
def profile(tmp_path):
    settings = copy.deepcopy({
        'measurement': DEFAULT_SETTINGS['measurement'],
        'display': DEFAULT_SETTINGS['display'],
        'file': DEFAULT_SETTINGS['file'],
        'output': DEFAULT_SETTINGS['output'],
    })
    settings['file']['data_directory'] = str(tmp_path / 'data')
    settings['measurement'].update({
        'gpib_address': ADDRESS, 'sampling_rate': 50.0, 'settling_time': 0.0,
        'fpp_current': 1e-4, 'fpp_voltage_compliance': 5.0, 'fpp_samples': 2,
        'fpp_power_warn_w': 1.0, 'fpp_power_stop_w': 2.0,
        'vdp_settling_s': 0.0, 'vdp_readings_per_polarity': 1, 'vdp_thickness_cm': 0.05,
    })
    return settings


@pytest.fixture
def sink():
    return ListSink()


@pytest.fixture
def session(sink):
    made = MeasurementSession(sink)
    yield made
    made.close(timeout=5.0)


@pytest.fixture(autouse=True)
def _no_sleep_inhibitor(monkeypatch):
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)


def _wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _link_lost():
    return pyvisa.errors.VisaIOError(pyvisa.constants.VI_ERROR_IO)


def pull_the_cable_after_the_first_reading(monkeypatch):
    """Every fake instrument's link dies once it has answered one ``:READ?``.

    The instrument is still on and still sourcing; only the adapter is gone,
    so every write and query after that raises the I/O error the bus gives.
    Returns the state, whose ``pulled`` flag says whether it has happened.
    """
    state = {'pulled': False}
    write, query = FakeKeithley.write, FakeKeithley.query

    def patched_write(fake, cmd):
        if state['pulled']:
            raise _link_lost()
        return write(fake, cmd)

    def patched_query(fake, cmd):
        if state['pulled']:
            raise _link_lost()
        reply = query(fake, cmd)
        if cmd.strip().upper() == ':READ?':
            state['pulled'] = True
        return reply

    monkeypatch.setattr(FakeKeithley, 'write', patched_write)
    monkeypatch.setattr(FakeKeithley, 'query', patched_query)
    return state


def _warnings(sink):
    return [e.payload['code'] for e in sink.of_type('log') if e.payload['level'] == 'warning']


def _ended(sink):
    (event,) = sink.of_type('run_ended')
    return event.payload


class TestARunThatLostItsLink:
    def test_a_resistance_run_says_the_output_is_unverified(self, session, sink, fake_rm,
                                                             profile, monkeypatch):
        pull_the_cable_after_the_first_reading(monkeypatch)
        session.start(profile, 'resistance', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle', timeout=30.0)

        ended = _ended(sink)
        assert ended['reason'] == 'read_error'
        assert ended['output_verified'] is False
        assert 'output_unverified' in _warnings(sink)
        (warning,) = [e for e in sink.of_type('log') if e.payload['code'] == 'output_unverified']
        assert warning.payload['message'] == UNVERIFIED
        assert sink.types()[-1] == 'run_ended'

    def test_a_van_der_pauw_run_says_so_too(self, session, sink, fake_rm, profile, monkeypatch):
        pull_the_cable_after_the_first_reading(monkeypatch)
        session.start(profile, 'vdp', 'wafer1', 'alice')
        assert _wait_for(lambda: session.status()['pending_prompt'] is not None)
        prompt = session.status()['pending_prompt']
        assert session.answer_prompt(prompt['prompt_id'], 'proceed')
        assert _wait_for(lambda: session.state == 'idle', timeout=30.0)

        ended = _ended(sink)
        assert ended['output_verified'] is False
        assert 'output_unverified' in _warnings(sink)
        assert sink.types()[-1] == 'run_ended'

    def test_a_run_that_died_is_reported_the_same_way(self, session, sink, fake_rm, profile,
                                                       monkeypatch):
        """The session's last-resort cleanup after a dead run has the same
        link, and its run_ended must not claim what it could not confirm."""
        from resistamet_gui.instrument import Keithley2400
        state = pull_the_cable_after_the_first_reading(monkeypatch)

        def dies_as_the_cable_goes(run):
            run.keithley = Keithley2400(ADDRESS).connect()
            run.keithley.write(":OUTP ON")
            state['pulled'] = True
            raise RuntimeError("boom")
        monkeypatch.setattr(ContinuousRun, 'execute', dies_as_the_cable_goes)

        session.start(profile, 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')
        session._thread.join(5.0)

        ended = _ended(sink)
        assert (ended['reason'], ended['output_verified']) == ('worker_error', False)
        assert 'output_unverified' in _warnings(sink)


class TestAnInstrumentThatKeepsItsOutputOn:
    def test_a_read_back_of_1_is_unverified(self, fake_rm, profile, monkeypatch):
        """The write went through; the instrument did not act on it."""
        dispatch = FakeKeithley._dispatch_write

        def ignores_output_off(fake, cmd):
            if cmd.upper().startswith(':OUTP OFF'):
                return None
            return dispatch(fake, cmd)
        monkeypatch.setattr(FakeKeithley, '_dispatch_write', ignores_output_off)

        sink = ListSink()
        ContinuousRun('four_point', 'wafer1', 'alice', profile, RunControl(),
                       EventEmitter(sink)).execute()

        assert fake_rm.opened[-1].state['outp'] is True
        assert _ended(sink)['output_verified'] is False
        assert 'output_unverified' in _warnings(sink)


class TestARunWithItsLinkIntact:
    def test_a_completed_run_is_verified(self, session, sink, fake_rm, profile):
        session.start(profile, 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')

        ended = _ended(sink)
        assert (ended['reason'], ended['output_verified']) == ('target_samples', True)
        assert 'output_unverified' not in _warnings(sink)
        fake = fake_rm.opened[-1]
        assert fake.state['outp'] is False
        # Read back after the write, before the handle is closed.
        tail = [cmd.upper() for _, cmd in fake.command_log][-2:]
        assert tail == [':OUTP OFF', ':OUTP?']

    def test_a_run_refused_before_the_instrument_is_verified(self, tmp_path, profile,
                                                              monkeypatch):
        """No instrument was opened, so no output was ever on."""
        from resistamet_gui.session import continuous_run as module

        def refuse(address, **kwargs):
            raise OSError(f"no instrument at {address}")
        monkeypatch.setattr(module, 'Keithley2400', refuse)

        sink = ListSink()
        ContinuousRun('resistance', 'wafer1', 'alice', profile, RunControl(),
                       EventEmitter(sink)).execute()

        ended = _ended(sink)
        assert (ended['reason'], ended['output_verified']) == ('connect_failed', True)
        assert 'output_unverified' not in _warnings(sink)

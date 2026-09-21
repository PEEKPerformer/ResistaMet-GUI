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
    Returns the state: ``pulled`` says whether it has happened, and a test
    that plugs the cable back in clears it and disarms ``armed`` so the
    next run keeps its link.
    """
    state = {'pulled': False, 'armed': True}
    write, query = FakeKeithley.write, FakeKeithley.query

    def patched_write(fake, cmd):
        if state['pulled']:
            raise _link_lost()
        return write(fake, cmd)

    def patched_query(fake, cmd):
        if state['pulled']:
            raise _link_lost()
        reply = query(fake, cmd)
        if state['armed'] and cmd.strip().upper() == ':READ?':
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


RECOVERED = "Output turned OFF after the previous run lost its link."


def _lose_the_link_for_one_run(session, sink, profile, monkeypatch, mode='resistance'):
    """A run at ADDRESS ends with its output in doubt; the link is then back."""
    state = pull_the_cable_after_the_first_reading(monkeypatch)
    session.start(profile, mode, 'wafer1', 'alice')
    assert _wait_for(lambda: session.state == 'idle', timeout=30.0)
    assert _ended(sink)['output_verified'] is False
    state['pulled'], state['armed'] = False, False
    sink.events.clear()


def _commands(fake):
    return [(op, cmd.upper()) for op, cmd in fake.command_log]


def _codes(sink):
    return [e.payload['code'] for e in sink.of_type('log')]


class TestTheSessionRemembers:
    def test_the_flag_is_set_after_a_lost_link_run(self, session, sink, fake_rm, profile,
                                                    monkeypatch):
        _lose_the_link_for_one_run(session, sink, profile, monkeypatch)
        assert session.output_unknown_at == ADDRESS

    def test_a_clean_run_does_not_set_it(self, session, sink, fake_rm, profile):
        session.start(profile, 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')
        assert _ended(sink)['output_verified'] is True
        assert session.output_unknown_at is None

    def test_a_run_that_died_sets_it_too(self, session, sink, fake_rm, profile, monkeypatch):
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
        assert session.output_unknown_at == ADDRESS


class TestIdentifyTurnsTheOutputOff:
    def test_output_off_is_the_first_command_after_idn(self, session, sink, fake_rm, profile,
                                                        monkeypatch):
        _lose_the_link_for_one_run(session, sink, profile, monkeypatch)

        found = session.identify(ADDRESS)

        assert found['model']
        fake = fake_rm.opened[-1]
        assert _commands(fake)[:3] == [('query', '*IDN?'), ('write', ':OUTP OFF'),
                                       ('query', ':OUTP?')]
        assert fake.state['outp'] is False
        assert session.output_unknown_at is None
        (said,) = sink.of_type('log')
        assert said.run_id is None
        assert (said.payload['code'], said.payload['message']) == ('output_off_recovered', RECOVERED)
        assert said.payload['level'] == 'info'

    def test_a_clean_session_identifies_without_writing(self, session, sink, fake_rm):
        session.identify(ADDRESS)
        assert [op for op, _ in fake_rm.opened[-1].command_log] == ['query', 'query']
        assert sink.events == []

    def test_a_read_back_of_1_leaves_the_doubt(self, session, sink, fake_rm, profile,
                                                monkeypatch):
        _lose_the_link_for_one_run(session, sink, profile, monkeypatch)
        dispatch = FakeKeithley._dispatch_write

        def ignores_output_off(fake, cmd):
            if cmd.upper().startswith(':OUTP OFF'):
                return None
            return dispatch(fake, cmd)
        monkeypatch.setattr(FakeKeithley, '_dispatch_write', ignores_output_off)
        # The fake starts with its output off; this identify must not be
        # fooled by that, only by the instrument's answer to :OUTP OFF.
        query = FakeKeithley.query
        monkeypatch.setattr(FakeKeithley, 'query',
                            lambda fake, cmd: '1' if cmd.strip().upper() == ':OUTP?'
                            else query(fake, cmd))

        session.identify(ADDRESS)

        assert session.output_unknown_at == ADDRESS
        assert _warnings(sink) == ['output_unverified']
        assert 'output_off_recovered' not in _codes(sink)

    def test_the_second_identify_sends_nothing_more(self, session, sink, fake_rm, profile,
                                                     monkeypatch):
        _lose_the_link_for_one_run(session, sink, profile, monkeypatch)
        session.identify(ADDRESS)
        session.identify(ADDRESS)
        assert [op for op, _ in fake_rm.opened[-1].command_log] == ['query', 'query']


class TestTheNextRunTurnsTheOutputOff:
    """The run's *RST does it; no second command, but the record says so."""

    @pytest.mark.parametrize('mode', ['four_point', 'vdp'])
    def test_rst_is_the_first_write_and_the_run_says_so(self, session, sink, fake_rm,
                                                          profile, monkeypatch, mode):
        _lose_the_link_for_one_run(session, sink, profile, monkeypatch)

        session.start(profile, mode, 'wafer1', 'alice')
        assert _wait_for(lambda: session.output_unknown_at is None)
        if mode == 'vdp':
            # The output is off before the first geometry is asked for.
            assert _wait_for(lambda: session.status()['pending_prompt'] is not None)
            session.stop()
        assert _wait_for(lambda: session.state == 'idle')

        fake = fake_rm.opened[-1]
        writes = [cmd for op, cmd in _commands(fake) if op == 'write']
        assert writes[0] == '*RST', "nothing is configured before the reset"
        assert ':OUTP OFF' not in writes[:writes.index(':OUTP:SMOD HIMP')], \
            "no second output-off before the configuration"
        assert fake.state['outp'] is False
        (said,) = [e for e in sink.of_type('log') if e.payload['code'] == 'output_off_recovered']
        assert said.payload['message'] == RECOVERED
        assert said.run_id == 'run-2'
        # Before the configuration: the file opens once configure is done.
        assert said.seq < sink.of_type('file_opened')[0].seq
        assert _ended(sink)['output_verified'] is True

    def test_a_clean_run_does_not_claim_a_recovery(self, session, sink, fake_rm, profile):
        session.start(profile, 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')
        assert 'output_off_recovered' not in _codes(sink)

    def test_a_connect_that_fails_keeps_the_doubt(self, session, sink, fake_rm, profile,
                                                   monkeypatch):
        _lose_the_link_for_one_run(session, sink, profile, monkeypatch)
        from resistamet_gui.session import continuous_run as module

        def refuse(address, **kwargs):
            raise OSError(f"no instrument at {address}")
        monkeypatch.setattr(module, 'Keithley2400', refuse)

        session.start(profile, 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')
        assert _ended(sink)['reason'] == 'connect_failed'
        assert session.output_unknown_at == ADDRESS

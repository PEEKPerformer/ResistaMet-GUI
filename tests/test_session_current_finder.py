"""The 4PP pre-run current finder, through the headless session.

The fake instrument presents one resistance with no noise, so the measured
scatter is zero and the finder works from its resolution floor (1 uV): four
significant figures want 10 mV across the sense probes.
"""
import copy
import os
import time

import pytest

from resistamet_gui.constants import (
    DEFAULT_SETTINGS, FPP_AUTOSELECT_DISCARD_READINGS, FPP_AUTOSELECT_PROBE_CYCLES,
    FPP_AUTOSELECT_PROBE_DISCARD_CYCLES,
)
from resistamet_gui.data_export import parse_metadata
from resistamet_gui.session.emitter import ListSink
from resistamet_gui.session.manager import MeasurementSession

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PAIRS = FPP_AUTOSELECT_PROBE_DISCARD_CYCLES + FPP_AUTOSELECT_PROBE_CYCLES


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
        'sampling_rate': 50.0, 'settling_time': 0.0,
        'fpp_current': 1e-3, 'fpp_voltage_compliance': 5.0, 'fpp_samples': 2,
        'fpp_delta_settling': 0.01,
        'fpp_autoselect_current': True, 'fpp_autoselect_sigfigs': 4,
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


@pytest.fixture(autouse=True)
def _no_minimum_settle(monkeypatch):
    """The fake settles at once; 18 reversals at 50 ms each are only waiting."""
    from resistamet_gui.session import current_finder
    monkeypatch.setattr(current_finder, 'FPP_AUTOSELECT_MIN_SETTLE_S', 0.0)


def _wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _run(session, profile):
    session.start(profile, 'four_point', 'wafer1', 'alice')
    assert _wait_for(lambda: session.state == 'idle')


def _run_to_prompt(session, profile):
    session.start(profile, 'four_point', 'wafer1', 'alice')
    assert _wait_for(lambda: session.status()['pending_prompt'] is not None)
    return session.status()['pending_prompt']


def _log(fake_rm):
    return [cmd for _op, cmd in fake_rm.opened[-1].command_log]


def _ended(sink):
    return sink.of_type('run_ended')[-1].payload


def _output_is_on(fake_rm) -> bool:
    return bool(fake_rm.opened[-1].state['outp'])


class TestOk:
    """100 ohm: 0.1 mA gives the 10 mV. Nobody is asked."""

    def test_the_run_sources_the_chosen_current(self, session, sink, fake_rm, profile):
        _run(session, profile)

        assert sink.of_type('prompt') == []
        selected = sink.of_type('fpp_current_selected')[0].payload
        assert selected['verdict'] == 'ok'
        assert selected['current_a'] == pytest.approx(1e-4)
        assert selected['requested_current_a'] == 1e-3
        assert selected['resistance_ohm'] == pytest.approx(100.0)
        assert selected['bound'] is False
        samples = [e.payload['values'] for e in sink.of_type('sample')]
        assert len(samples) == 2
        for values in samples:
            assert values['current'] == pytest.approx(1e-4)
            assert values['voltage'] == pytest.approx(1e-2)
        assert _ended(sink)['ok'] is True

    def test_the_file_name_and_header_carry_the_chosen_current(self, session, sink, fake_rm,
                                                             profile):
        _run(session, profile)
        path = _ended(sink)['path']
        header = parse_metadata(path)

        assert '_4PP_0.10mA' in os.path.basename(path)
        assert header['params.source_current_A'] == pytest.approx(1e-4)
        assert header['params.autoselect_current'] is True
        assert header['params.autoselect_requested_current_A'] == 1e-3
        assert header['params.autoselect_target_sig_figs'] == 4

    def test_the_hand_over_from_probe_to_run_is_exactly_this(self, session, sink, fake_rm,
                                                           profile):
        """Output off, the final range and level, output on, discards, row 1.

        Without the discards the first row was read one settle after the
        range change, with the probe's readings the most recent ones.
        """
        _run(session, profile)
        log = _log(fake_rm)

        probe = [':OUTP ON', ':SOUR:CURR:RANG 0.001']
        probe += [':SOUR:CURR 0.001', ':READ?', ':SOUR:CURR -0.001', ':READ?'] * PAIRS
        probe += [':SOUR:CURR 0.001', ':OUTP OFF']
        hand_over = [':SOUR:CURR:RANG 0.0001', ':SOUR:CURR 0.0001', ':OUTP ON']
        hand_over += [':READ?'] * FPP_AUTOSELECT_DISCARD_READINGS
        start = log.index(':OUTP ON')
        end = start + len(probe) + len(hand_over)
        assert log[start:end] == probe + hand_over
        # Then the two rows, and nothing else is read before the output is off.
        assert [cmd for cmd in log[end:log.index(':OUTP OFF', end)] if cmd == ':READ?'] == \
            [':READ?'] * 2

    def test_a_negative_requested_current_stays_negative(self, session, sink, fake_rm, profile):
        profile['measurement']['fpp_current'] = -1e-3
        _run(session, profile)
        assert sink.of_type('fpp_current_selected')[0].payload['current_a'] == \
            pytest.approx(-1e-4)
        assert ':SOUR:CURR -0.0001' in _log(fake_rm)

    def test_a_moving_filter_is_off_for_the_probe_and_re_armed_after(self, session, sink,
                                                                    fake_rm, profile):
        profile['measurement'].update({'filter_enabled': True, 'filter_type': 'moving'})
        _run(session, profile)
        log = _log(fake_rm)

        armed = log.index(':SENS:AVER ON')
        off = log.index(':SENS:AVER OFF')
        probe_on = log.index(':OUTP ON')
        level = log.index(':SOUR:CURR 0.0001')
        re_armed = log.index(':SENS:AVER ON', armed + 1)
        run_on = log.index(':OUTP ON', probe_on + 1)
        assert armed < off < probe_on < level < re_armed < run_on

    def test_a_repeat_filter_is_left_alone(self, session, sink, fake_rm, profile):
        profile['measurement'].update({'filter_enabled': True, 'filter_type': 'repeat'})
        _run(session, profile)
        log = _log(fake_rm)
        assert ':SENS:AVER OFF' not in log
        assert log.count(':SENS:AVER ON') == 1

    def test_a_megohm_is_measured_at_the_smallest_current(self, session, sink, fake_rm,
                                                          profile):
        """1 mA into 1 Mohm is in compliance; 1 uA is 1 V, and measures it.

        R comes from the programmed 1 uA. The release divided by the CURR
        element one read after the range change.
        """
        fake_rm._dut_r = 1e6
        _run(session, profile)

        assert sink.of_type('prompt') == []
        selected = sink.of_type('fpp_current_selected')[0].payload
        assert selected['verdict'] == 'ok'
        assert selected['current_a'] == 1e-6
        assert selected['resistance_ohm'] == pytest.approx(1e6)
        log = _log(fake_rm)
        low = log.index(':SOUR:CURR:RANG 1e-06')
        assert log[low:low + 1 + 4 * PAIRS] == [':SOUR:CURR:RANG 1e-06'] + [
            ':SOUR:CURR 1e-06', ':READ?', ':SOUR:CURR -1e-06', ':READ?'] * PAIRS


class TestTheProbeNeverReadsTheCurrentElement:
    def test_a_lying_current_element_changes_nothing(self, session, sink, fake_rm, profile,
                                                     monkeypatch):
        """Bench: the element read 20 % of the setpoint, once -23.7 uA on 1 uA."""
        from resistamet_gui._simulator import FakeKeithley
        real = FakeKeithley._format_elements

        def lying(self, v, i, r, stat):
            return real(self, v, -23.7 * i, r, stat)

        monkeypatch.setattr(FakeKeithley, '_format_elements', lying)
        fake_rm._dut_r = 1e6
        _run(session, profile)

        selected = sink.of_type('fpp_current_selected')[0].payload
        assert selected['resistance_ohm'] == pytest.approx(1e6)
        assert selected['current_a'] == 1e-6
        assert [e.payload['values']['current'] for e in sink.of_type('sample')] == [1e-6, 1e-6]


class TestMarginal:
    """1 mohm under a 20 mA ceiling (0.1 W / 5 V): 20 uV, SNR 20, not 1e4."""

    @pytest.fixture(autouse=True)
    def _milliohm(self, fake_rm):
        fake_rm._dut_r = 1e-3

    def test_the_run_asks_with_the_output_off(self, session, sink, fake_rm, profile):
        prompt = _run_to_prompt(session, profile)
        on_while_pending = _output_is_on(fake_rm)
        log = _log(fake_rm)
        session.stop()

        assert on_while_pending is False
        assert log[-1] == ':OUTP OFF'
        assert prompt['kind'] == 'fpp_current_decision'
        assert prompt['options'] == ['proceed', 'abort']
        assert prompt['requires_human'] is False
        detail = prompt['detail']
        assert detail['verdict'] == 'marginal'
        assert detail['current_a'] == pytest.approx(0.02)
        assert detail['snr'] == pytest.approx(20.0)
        assert detail['sig_figs'] == pytest.approx(1.30, abs=0.01)
        assert detail['target_sig_figs'] == 4
        assert "1.3 significant figures" in detail['message']
        assert session.state in ('awaiting_prompt', 'stopping', 'idle')
        # Nothing is on disk for a run nobody has agreed to.
        assert sink.of_type('file_opened') == []

    def test_proceed_runs_at_the_suggested_current(self, session, sink, fake_rm, profile):
        prompt = _run_to_prompt(session, profile)
        session.answer_prompt(prompt['prompt_id'], 'proceed')
        assert _wait_for(lambda: session.state == 'idle')

        assert sink.of_type('prompt_resolved')[0].payload['choice'] == 'proceed'
        assert sink.of_type('fpp_current_selected')[0].payload['verdict'] == 'marginal'
        assert [e.payload['values']['current'] for e in sink.of_type('sample')] == \
            [pytest.approx(0.02)] * 2
        header = parse_metadata(_ended(sink)['path'])
        assert header['params.source_current_A'] == pytest.approx(0.02)
        assert header['params.autoselect_requested_current_A'] == 1e-3
        assert _ended(sink)['ok'] is True

    def test_abort_ends_the_run_with_no_file_and_no_sample(self, session, sink, fake_rm,
                                                         profile):
        prompt = _run_to_prompt(session, profile)
        session.answer_prompt(prompt['prompt_id'], 'abort')
        assert _wait_for(lambda: session.state == 'idle')

        assert _ended(sink)['reason'] == 'cancelled'
        assert _ended(sink)['ok'] is False
        assert _ended(sink)['path'] is None
        assert _ended(sink)['output_verified'] is True
        assert sink.of_type('sample') == []
        assert sink.of_type('fpp_current_selected') == []
        log = _log(fake_rm)
        assert log.count(':OUTP ON') == 1      # the probe's; the run never turned it on
        assert not _output_is_on(fake_rm)

    def test_a_stop_during_the_prompt_ends_the_run(self, session, sink, fake_rm, profile):
        _run_to_prompt(session, profile)
        session.stop()
        assert session.status()['pending_prompt'] is None
        assert _wait_for(lambda: session.state == 'idle')

        assert _ended(sink)['reason'] == 'user_stop'
        assert _ended(sink)['ok'] is False
        assert sink.of_type('prompt_resolved')[0].payload['choice'] is None
        assert sink.of_type('sample') == []
        assert sink.of_type('file_opened') == []
        assert _log(fake_rm).count(':OUTP ON') == 1

    def test_no_answer_in_time_abandons_the_run(self, session, sink, fake_rm, profile):
        session.start(profile, 'four_point', 'wafer1', 'alice', prompt_timeout_s=0.2)
        assert _wait_for(lambda: session.state == 'idle')
        assert _ended(sink)['reason'] == 'prompt_timeout'
        assert sink.of_type('sample') == []


class TestTooConductive:
    def test_under_one_figure_at_the_ceiling_asks(self, session, sink, fake_rm, profile):
        fake_rm._dut_r = 1e-5      # 0.2 uV at 20 mA
        prompt = _run_to_prompt(session, profile)
        session.answer_prompt(prompt['prompt_id'], 'abort')
        assert _wait_for(lambda: session.state == 'idle')
        assert prompt['detail']['verdict'] == 'too_conductive'
        assert prompt['detail']['current_a'] == pytest.approx(0.02)


class TestTooResistive:
    """1 Tohm: in compliance at 1 mA and still at 1 uA."""

    @pytest.fixture(autouse=True)
    def _insulator(self, fake_rm):
        fake_rm._dut_r = 1e12

    def test_compliance_at_the_smallest_current_is_a_bound(self, session, sink, fake_rm,
                                                           profile):
        prompt = _run_to_prompt(session, profile)
        on_while_pending = _output_is_on(fake_rm)
        session.answer_prompt(prompt['prompt_id'], 'abort')
        assert _wait_for(lambda: session.state == 'idle')

        assert on_while_pending is False
        detail = prompt['detail']
        assert detail['verdict'] == 'too_resistive'
        assert detail['bound'] is True
        # 5 V over the programmed 1 uA: the most that can have flowed.
        assert detail['resistance_ohm'] == pytest.approx(5e6)
        assert detail['current_a'] == 1e-6
        assert detail['snr'] is None and detail['noise_floor_v'] is None
        assert "V/I > 5e+06 Ω" in detail['message']

    def test_a_thickness_adds_the_conductivity_bound(self, session, sink, fake_rm, profile):
        profile['measurement']['fpp_thickness_um'] = 100.0
        prompt = _run_to_prompt(session, profile)
        session.stop()
        # rho > 4.532 x 0.01 cm x 5e6 ohm = 2.27e5 ohm cm; sigma < 4.4e-4 S/m.
        assert "σ < 0.00044 S/m" in prompt['detail']['message']

    def test_proceed_logs_compliance_rows_at_the_smallest_current(self, session, sink, fake_rm,
                                                                 profile):
        prompt = _run_to_prompt(session, profile)
        session.answer_prompt(prompt['prompt_id'], 'proceed')
        assert _wait_for(lambda: session.state == 'idle')

        samples = [e.payload for e in sink.of_type('sample')]
        assert [s['compliance'] for s in samples] == ['V_COMP', 'V_COMP']
        assert [s['values']['current'] for s in samples] == [1e-6, 1e-6]
        assert parse_metadata(_ended(sink)['path'])['params.source_current_A'] == 1e-6


class TestStopDuringTheProbe:
    def test_the_output_goes_off_and_nothing_is_recorded(self, session, sink, fake_rm, profile):
        profile['measurement']['fpp_delta_settling'] = 5.0
        session.start(profile, 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: fake_rm.opened and ':OUTP ON' in _log(fake_rm))
        assert _output_is_on(fake_rm)
        began = time.time()
        session.stop()
        assert _wait_for(lambda: session.state == 'idle')

        assert time.time() - began < 3.0
        assert _ended(sink)['reason'] == 'user_stop'
        assert _ended(sink)['ok'] is False
        assert _ended(sink)['output_verified'] is True
        assert sink.of_type('file_opened') == []
        assert not _output_is_on(fake_rm)


class TestProbeFailure:
    def test_a_read_error_in_the_probe_ends_the_run_with_the_output_off(
            self, session, sink, fake_rm, profile, monkeypatch):
        import pyvisa
        from resistamet_gui._simulator import FakeKeithley
        real = FakeKeithley.query

        def no_readings(self, cmd):
            if cmd.strip() == ':READ?':
                raise pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)
            return real(self, cmd)

        monkeypatch.setattr(FakeKeithley, 'query', no_readings)
        session.start(profile, 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')

        assert _ended(sink)['reason'] == 'autoselect_failed'
        assert [e.payload['code'] for e in sink.of_type('error')] == ['autoselect_failed']
        assert sink.of_type('file_opened') == []
        assert not _output_is_on(fake_rm)


class TestOff:
    def test_off_probes_nothing_and_says_so_in_the_header(self, session, sink, fake_rm,
                                                          profile):
        profile['measurement']['fpp_autoselect_current'] = False
        _run(session, profile)
        log = _log(fake_rm)

        assert log.count(':OUTP ON') == 1
        assert log.count(':READ?') == 2
        assert sink.of_type('fpp_current_selected') == []
        header = parse_metadata(_ended(sink)['path'])
        assert header['params.source_current_A'] == 1e-3
        assert header['params.autoselect_current'] is False
        assert 'params.autoselect_requested_current_A' not in header
        assert 'params.autoselect_target_sig_figs' not in header


class TestTheFindersChoiceAgainstThePowerStop:
    """The smallest current the finder may choose, at the compliance, can be
    more than the power stop when the probe current is below it: 1 uA at
    200 V against 0.1 mW. The configure step refuses such a request; the
    finder's own choice ran, and tripped on its first row."""

    def test_proceeding_past_the_power_stop_is_refused(self, session, sink, fake_rm, profile):
        from resistamet_gui._simulator import _idn_for_model
        fake_rm._idn = _idn_for_model('2410')
        fake_rm._dut_r = 1e12
        profile['measurement'].update({
            'fpp_current': 4e-7, 'fpp_voltage_compliance': 200.0,
            'fpp_power_warn_w': 1e-4, 'fpp_power_stop_w': 1e-4,
            'safety_voltage_warn_silenced': True})
        prompt = _run_to_prompt(session, profile)
        assert prompt['kind'] == 'fpp_current_decision'
        session.answer_prompt(prompt['prompt_id'], 'proceed')
        assert _wait_for(lambda: session.state == 'idle')

        assert _ended(sink)['reason'] == 'power_envelope'
        assert _ended(sink)['path'] is None
        assert sink.of_type('sample') == [] and sink.of_type('overpower_trip') == []
        assert _output_is_on(fake_rm) is False
        assert any("hard stop" in e.payload['message'] for e in sink.of_type('error'))

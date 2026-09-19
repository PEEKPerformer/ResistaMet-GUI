"""The headless session: state, single-run rule, commands, teardown.

No Qt anywhere — this is the path the API sidecar and the MCP layer will use.
"""
import copy
import os
import re
import time
from pathlib import Path

import pytest

from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.session.emitter import ListSink
from resistamet_gui.session.manager import MeasurementSession, SessionBusy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def profile(tmp_path):
    settings = copy.deepcopy({
        'measurement': DEFAULT_SETTINGS['measurement'],
        'display': DEFAULT_SETTINGS['display'],
        'file': DEFAULT_SETTINGS['file'],
        'output': DEFAULT_SETTINGS['output'],
    })
    settings['file']['data_directory'] = str(tmp_path / 'data')
    settings['measurement']['sampling_rate'] = 50.0
    settings['measurement']['settling_time'] = 0.0
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


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _four_point(profile, samples=2):
    profile['measurement'].update({
        'fpp_current': 1e-4, 'fpp_voltage_compliance': 5.0, 'fpp_samples': samples,
        'fpp_power_warn_w': 1.0, 'fpp_power_stop_w': 2.0,
    })
    return profile


class TestStateMachine:
    def test_starts_idle(self, session):
        assert session.state == 'idle'
        assert session.status()['run_id'] is None

    def test_run_reports_its_id_and_ends_idle(self, session, sink, fake_rm, profile):
        run_id = session.start(_four_point(profile), 'four_point', 'wafer1', 'alice')
        assert run_id == 'run-1'
        assert _wait_for(lambda: session.state == 'idle')
        assert [e.run_id for e in sink.events][0] == 'run-1'

    def test_one_run_at_a_time(self, session, fake_rm, profile):
        session.start(_four_point(profile, samples=50), 'four_point', 'wafer1', 'alice')
        with pytest.raises(SessionBusy):
            session.start(profile, 'four_point', 'wafer2', 'alice')
        session.stop()

    def test_identify_is_refused_during_a_run(self, session, fake_rm, profile):
        session.start(_four_point(profile, samples=50), 'four_point', 'wafer1', 'alice')
        with pytest.raises(SessionBusy):
            session.identify('GPIB0::24::INSTR')
        session.stop()

    def test_identify_returns_the_model(self, session, fake_rm):
        info = session.identify('GPIB0::24::INSTR')
        assert 'KEITHLEY' in info['idn'].upper()
        assert info['model']


class TestCommands:
    def test_stop_ends_the_run(self, session, sink, fake_rm, profile):
        session.start(_four_point(profile, samples=0), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('sample'))
        session.stop()
        assert _wait_for(lambda: session.state == 'idle')
        assert [e.payload['reason'] for e in sink.of_type('run_ended')] == ['user_stop']

    def test_pause_and_resume_show_in_the_state(self, session, sink, fake_rm, profile):
        session.start(_four_point(profile, samples=0), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('sample'))
        session.pause()
        assert _wait_for(lambda: session.state == 'paused')
        session.resume()
        assert _wait_for(lambda: session.state == 'running')
        session.stop()

    def test_mark_event_reaches_a_sample(self, session, sink, fake_rm, profile):
        session.start(_four_point(profile, samples=0), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('sample'))
        session.mark_event('PROBE_MOVED')
        assert _wait_for(lambda: any(e.payload['event_marker'] == 'PROBE_MOVED'
                                      for e in sink.of_type('sample')))
        session.stop()

    def test_commands_without_a_run_are_refused(self, session):
        with pytest.raises(SessionBusy):
            session.mark_event('X')
        with pytest.raises(SessionBusy):
            session.answer_prompt('vdp_geometry-1', 'proceed')

    def test_stop_without_a_run_is_harmless(self, session):
        session.stop()
        assert session.state == 'idle'


class TestFileNames:
    """Two runs of one sample inside the same second ask for the same path."""

    @pytest.fixture
    def same_second(self, monkeypatch):
        from resistamet_gui.session import continuous_run
        real = continuous_run.create_base_path

        def frozen(*args, **kwargs):
            return real(*args, timestamp=1789000000, **kwargs)
        monkeypatch.setattr(continuous_run, 'create_base_path', frozen)

    def _run(self, session, sink, profile, run_number):
        session.start(_four_point(profile), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: len(sink.of_type('file_finalized')) == run_number
                         and session.state == 'idle')
        return Path(sink.of_type('file_finalized')[-1].payload['path'])

    def test_the_second_run_does_not_overwrite_the_first(self, session, sink, fake_rm,
                                                          profile, same_second):
        first = self._run(session, sink, profile, 1)
        before = first.read_bytes()
        second = self._run(session, sink, profile, 2)

        # 0.10mA: the decimal point survives into the name.
        assert first.name == '1789000000_wafer1_4PP_0.10mA.csv'
        assert second.name == '1789000000_wafer1_4PP_0.10mA-2.csv'
        assert first.read_bytes() == before
        assert sink.of_type('file_opened')[-1].payload['path'] == str(second)


class TestLogText:
    """Log messages are read at the bench: they name a mode the way the UIs
    do, carry no emoji, and write units with their symbols. Log codes are
    what clients key on, and stay as they were."""

    #: Pictographs and dingbats, including the warning sign this log once used.
    EMOJI = re.compile('[\u2600-\u27bf\ufe0f\U0001f300-\U0001faff]')

    def _messages(self, sink):
        return {e.payload['code']: e.payload['message'] for e in sink.of_type('log')}

    def test_every_mode_has_a_display_name(self):
        from resistamet_gui.constants import MODE_DISPLAY_NAMES
        from resistamet_gui.schema.settings_modes import MODE_MODELS
        assert sorted(MODE_DISPLAY_NAMES) == sorted(MODE_MODELS)

    def test_a_probe_run_is_named_as_the_ui_names_it(self, session, sink, fake_rm, profile):
        # (Not "four_point" in this test's name: the data path, which the
        # closing message quotes, is built from it.)
        # 0.1 mA x 5 V = 0.5 mW worst case over a 0.1 mW warning: the run
        # carries the power-envelope warning that used to open with an emoji.
        profile = _four_point(profile)
        profile['measurement']['fpp_power_warn_w'] = 1e-4
        session.start(profile, 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')

        messages = self._messages(sink)
        assert messages['configuring'] == "Configuring instrument for Four-point probe mode..."
        assert messages['progress'].startswith("Running Four-point probe: ")
        assert messages['completed'].startswith("Measurement (Four-point probe) completed!")
        assert messages['power_envelope'].startswith("Warning: 4PP power envelope: ")
        assert [m for m in messages.values() if 'four_point' in m] == []
        assert [m for m in messages.values() if self.EMOJI.search(m)] == []

    def test_power_warnings_keep_sub_milliwatt_thresholds(self, session, sink, fake_rm, profile):
        # Bench: a 0.5 mW warning threshold was printed as "0 mW". Here
        # 3 mA into the fake's 100 ohm is 900 µW measured, 15 mW worst case.
        profile = _four_point(profile)
        profile['measurement'].update({
            'fpp_current': 3e-3, 'fpp_power_warn_w': 5e-4, 'fpp_power_stop_w': 5e-2})
        session.start(profile, 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')

        warnings = [e.payload['message'] for e in sink.of_type('log')
                    if e.payload['code'] == 'power_envelope']
        assert warnings[0] == ("Warning: 4PP power envelope: up to 15 mW (I × V_comp). "
                               "Above warning threshold 500 µW — proceed with care.")
        assert re.fullmatch(r"Warning: 4PP power [\d.]+ µW above warn threshold 500 µW",
                            warnings[1]), warnings[1]

    def test_resistance_progress_uses_the_ohm_sign(self, session, sink, fake_rm, profile):
        session.start(profile, 'resistance', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('sample'))
        session.stop()
        assert _wait_for(lambda: session.state == 'idle')

        messages = self._messages(sink)
        assert re.fullmatch(r"Running Resistance: \d\d:\d\d:\d\d \| R: [\d.]+ \u03a9",
                            messages['progress']), messages['progress']
        assert messages['stopping'] == "Stopping measurement (Resistance)..."


class TestOpenSenseLeads:
    """Four-point with the sense leads open or swapped: V/I comes out
    negative. The run says so once and changes nothing."""

    @pytest.fixture
    def floating_sense(self, monkeypatch):
        """The fake with the voltmeter floating at -0.95 V, as on the bench."""
        import pyvisa
        from tests.fakes.fake_keithley import FakeResourceManager
        rm = FakeResourceManager(dut_voltage_offset=-0.95)
        monkeypatch.setattr(pyvisa, "ResourceManager", lambda *args, **kwargs: rm)
        return rm

    def _warnings(self, sink):
        return [e.payload for e in sink.of_type('log')
                if e.payload['code'] == 'fpp_negative_ratio']

    def test_one_warning_for_the_whole_run(self, session, sink, floating_sense, profile):
        session.start(_four_point(profile, samples=5), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')

        warnings = self._warnings(sink)
        assert len(warnings) == 1
        assert warnings[0]['level'] == 'warning'
        assert "sense leads are probably open or swapped" in warnings[0]['message']

    def test_the_run_and_its_numbers_are_untouched(self, session, sink, floating_sense, profile):
        session.start(_four_point(profile, samples=5), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')

        ended = sink.of_type('run_ended')[0].payload
        assert (ended['reason'], ended['ok'], ended['samples']) == ('target_samples', True, 5)
        assert sink.of_type('error') == []
        for sample in sink.of_type('sample'):
            assert sample.payload['values']['voltage'] < 0
            values, derived = sample.payload['values'], sample.payload['derived']
            assert derived['ratio'] == pytest.approx(values['voltage'] / values['current'])
            assert derived['rs'] < 0
            assert sample.payload['compliance'] == 'OK'

    def test_a_good_contact_is_not_warned_about(self, session, sink, fake_rm, profile):
        session.start(_four_point(profile, samples=3), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')
        assert self._warnings(sink) == []

    def test_delta_mode_is_not_warned_about(self, session, sink, floating_sense, profile):
        profile = _four_point(profile, samples=2)
        profile['measurement'].update({'fpp_delta_mode': True, 'fpp_delta_settling': 0.01})
        session.start(profile, 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')
        assert self._warnings(sink) == []


class TestValidation:
    def test_strict_resolver_rejects_a_bad_request(self, session, profile):
        with pytest.raises(ValueError) as excinfo:
            session.start(profile, 'vdp', 'wafer1', 'alice',
                           overrides={'vdp_thickness_cm': 0.0})
        assert 'vdp_thickness_cm' in str(excinfo.value)

    def test_rejected_request_leaves_the_session_idle(self, session, profile):
        with pytest.raises(ValueError):
            session.start(profile, 'resistance', 'wafer1', 'alice',
                           overrides={'not_a_setting': 1})
        assert session.state == 'idle'


class TestStatus:
    def test_status_tracks_the_run(self, session, sink, fake_rm, profile):
        session.start(_four_point(profile, samples=0), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('sample'))
        status = session.status()
        assert status['mode'] == 'four_point'
        assert status['path'].endswith('.csv')
        assert status['last_seq'] > 0
        session.stop()

    def test_idle_status_is_exactly_this(self, session):
        """The reply's shape is a contract (session/status.py); pin it."""
        assert session.status() == {
            'state': 'idle', 'run_id': None, 'mode': None, 'path': None,
            'last_seq': 0, 'pending_prompt': None, 'instrument': None,
        }

    def test_a_pending_prompt_is_reported_in_full(self, session, sink, fake_rm, profile):
        profile['measurement'].update({'vdp_thickness_cm': 0.05})
        session.start(profile, 'vdp', 'wafer1', 'alice')
        assert _wait_for(lambda: session.status()['pending_prompt'] is not None)
        status = session.status()
        session.stop()

        assert status['state'] == 'awaiting_prompt'
        assert status['mode'] == 'vdp'
        prompt = status['pending_prompt']
        assert sorted(prompt) == ['detail', 'kind', 'options', 'prompt_id', 'requires_human']
        assert prompt['kind'] == 'vdp_geometry'
        assert prompt['options'] == ['proceed', 'abort']
        assert prompt['requires_human'] is True
        assert prompt['detail']['index'] == 0

    def test_the_status_model_has_the_reply_s_keys(self, session):
        from resistamet_gui.session.status import PendingPrompt, SessionStatus
        assert sorted(SessionStatus.model_fields) == sorted(session.status())
        assert all(field.is_required() for field in SessionStatus.model_fields.values())
        assert all(field.is_required() for field in PendingPrompt.model_fields.values())

    def test_the_instrument_a_run_connected_to_stays_in_the_status(self, session, sink,
                                                                     fake_rm, profile):
        """A client that reloads has lost the instrument_connected event."""
        session.start(_four_point(profile), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle' and sink.of_type('run_ended'))

        connected = sink.of_type('instrument_connected')[0].payload
        assert session.status()['instrument'] == connected
        assert connected['address'] == 'GPIB0::24::INSTR'

    def test_identify_also_sets_the_status_instrument(self, session, fake_rm):
        found = session.identify('GPIB0::24::INSTR')
        assert session.status()['instrument'] == found
        assert sorted(found) == ['address', 'idn', 'max_power_w', 'max_source_i',
                                 'max_source_v', 'model']

    def test_a_failed_identify_leaves_the_last_instrument(self, session, fake_rm, monkeypatch):
        found = session.identify('GPIB0::24::INSTR')
        from resistamet_gui import instrument

        def refuse(self):
            raise OSError("nothing there")
        monkeypatch.setattr(instrument.Keithley2400, 'connect', refuse)
        with pytest.raises(OSError):
            session.identify('GPIB0::7::INSTR')
        assert session.status()['instrument'] == found

    def test_close_joins_the_thread(self, sink, fake_rm, profile):
        session = MeasurementSession(sink)
        session.start(_four_point(profile, samples=0), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('sample'))
        session.close(timeout=5.0)
        assert session.state == 'idle'


class TestSafetyPrompt:
    """A headless client has no modal, so the run itself must ask."""

    def _hazardous(self, profile):
        profile['measurement'].update({
            'safety_voltage_warn_v': 30.0, 'safety_voltage_warn_silenced': False,
            'vsource_voltage': 60.0, 'vsource_current_compliance': 0.1,
            'vsource_duration_hours': 0.0,
        })
        return profile

    def _pending(self, session):
        return session.status()['pending_prompt']

    def test_run_waits_for_acknowledgement(self, session, sink, fake_rm, profile):
        session.start(self._hazardous(profile), 'source_v', 'wafer1', 'alice')
        assert _wait_for(lambda: self._pending(session) is not None)

        prompt = self._pending(session)
        assert prompt['kind'] == 'safety_voltage_ack'
        assert prompt['requires_human'] is True
        assert prompt['detail']['voltage_v'] == 60.0
        assert session.state == 'awaiting_prompt'
        # nothing has been energised yet
        assert sink.of_type('instrument_connected') == []

        session.answer_prompt(prompt['prompt_id'], 'acknowledge')
        assert _wait_for(lambda: sink.of_type('sample'))
        session.stop()

    def test_cancel_ends_the_run_before_the_instrument_opens(self, session, sink, fake_rm, profile):
        session.start(self._hazardous(profile), 'source_v', 'wafer1', 'alice')
        assert _wait_for(lambda: self._pending(session) is not None)
        prompt = self._pending(session)

        session.answer_prompt(prompt['prompt_id'], 'cancel')
        assert _wait_for(lambda: session.state == 'idle')

        assert [e.payload['reason'] for e in sink.of_type('run_ended')] == ['cancelled']
        assert sink.of_type('instrument_connected') == []
        assert sink.of_type('file_opened') == []

    def test_cancel_is_written_to_the_log(self, session, sink, fake_rm, profile):
        session.start(self._hazardous(profile), 'source_v', 'wafer1', 'alice')
        assert _wait_for(lambda: self._pending(session) is not None)
        session.answer_prompt(self._pending(session)['prompt_id'], 'cancel')
        assert _wait_for(lambda: session.state == 'idle')

        declined = [e.payload for e in sink.of_type('log')
                    if e.payload['code'] == 'safety_declined']
        assert [d['message'] for d in declined] == [
            "Run of 'wafer1' not started: the touch-safety warning was not "
            "acknowledged (Source V = 60 V, threshold 30 V)."]
        # The refusal is the last thing said before the run ends.
        assert sink.events[-2].payload['code'] == 'safety_declined'
        assert sink.events[-1].type == 'run_ended'

    def test_cancelling_a_van_der_pauw_run_is_logged_too(self, session, sink, fake_rm, profile):
        profile['measurement'].update({
            'safety_voltage_warn_v': 30.0, 'safety_voltage_warn_silenced': False,
            'vdp_voltage_compliance': 60.0, 'vdp_thickness_cm': 0.05})
        session.start(profile, 'vdp', 'wafer1', 'alice')
        assert _wait_for(lambda: self._pending(session) is not None)
        session.answer_prompt(self._pending(session)['prompt_id'], 'cancel')
        assert _wait_for(lambda: session.state == 'idle')

        messages = [e.payload['message'] for e in sink.of_type('log')
                    if e.payload['code'] == 'safety_declined']
        assert messages == [
            "Run of 'wafer1' not started: the touch-safety warning was not "
            "acknowledged (V compliance = 60 V, threshold 30 V)."]

    def test_acknowledging_logs_no_refusal(self, session, sink, fake_rm, profile):
        session.start(self._hazardous(profile), 'source_v', 'wafer1', 'alice')
        assert _wait_for(lambda: self._pending(session) is not None)
        session.answer_prompt(self._pending(session)['prompt_id'], 'acknowledge')
        assert _wait_for(lambda: sink.of_type('sample'))
        session.stop()
        assert _wait_for(lambda: session.state == 'idle')
        assert [e for e in sink.of_type('log') if e.payload['code'] == 'safety_declined'] == []

    def test_a_current_sourced_sweep_asks_about_its_voltage_compliance(self, session, sink,
                                                                         fake_rm, profile):
        """An open circuit takes a current source to its compliance: 60 V of
        compliance is 60 V on the leads, and is asked about exactly as a
        60 V source is."""
        profile['measurement'].update({
            'safety_voltage_warn_v': 30.0, 'safety_voltage_warn_silenced': False})
        session.start(profile, 'sweep', 'wafer1', 'alice', overrides={
            'sweep_source': 'current', 'sweep_start': 0.0, 'sweep_stop': 1e-3,
            'sweep_step': 1e-4, 'sweep_compliance': 60.0})
        assert _wait_for(lambda: self._pending(session) is not None)
        sweep_prompt = self._pending(session)

        assert sweep_prompt['kind'] == 'safety_voltage_ack'
        assert sweep_prompt['requires_human'] is True
        assert sweep_prompt['options'] == ['acknowledge', 'cancel']
        assert sweep_prompt['detail']['voltage_v'] == 60.0
        assert sweep_prompt['detail']['threshold_v'] == 30.0
        assert sweep_prompt['detail']['reason'] == 'V compliance'
        # nothing has been energised yet
        assert sink.of_type('instrument_connected') == []

        session.answer_prompt(sweep_prompt['prompt_id'], 'cancel')
        assert _wait_for(lambda: session.state == 'idle')
        assert [e.payload['reason'] for e in sink.of_type('run_ended')] == ['cancelled']
        assert sink.of_type('instrument_connected') == []

        # The same question a 60 V source raises, field for field.
        session.start(self._hazardous(profile), 'source_v', 'wafer1', 'alice')
        assert _wait_for(lambda: self._pending(session) is not None)
        source_prompt = self._pending(session)
        session.answer_prompt(source_prompt['prompt_id'], 'cancel')
        assert _wait_for(lambda: session.state == 'idle')
        for key in ('kind', 'requires_human', 'options'):
            assert sweep_prompt[key] == source_prompt[key]
        for key in ('voltage_v', 'threshold_v'):
            assert sweep_prompt['detail'][key] == source_prompt['detail'][key]
        assert sorted(sweep_prompt['detail']) == sorted(source_prompt['detail'])

    def test_acknowledged_the_60_v_sweep_runs(self, session, sink, fake_rm, profile):
        profile['measurement'].update({
            'safety_voltage_warn_v': 30.0, 'safety_voltage_warn_silenced': False})
        session.start(profile, 'sweep', 'wafer1', 'alice', overrides={
            'sweep_source': 'current', 'sweep_start': 0.0, 'sweep_stop': 1e-3,
            'sweep_step': 1e-4, 'sweep_compliance': 60.0})
        assert _wait_for(lambda: self._pending(session) is not None)
        session.answer_prompt(self._pending(session)['prompt_id'], 'acknowledge')
        assert _wait_for(lambda: session.state == 'idle')
        assert [e.payload['reason'] for e in sink.of_type('run_ended')] == ['completed']
        assert len(sink.of_type('sweep_segment')) == 1

    def test_a_low_voltage_compliance_asks_nothing(self, session, sink, fake_rm, profile):
        profile['measurement'].update({
            'safety_voltage_warn_v': 30.0, 'safety_voltage_warn_silenced': False})
        session.start(profile, 'sweep', 'wafer1', 'alice', overrides={
            'sweep_source': 'current', 'sweep_start': 0.0, 'sweep_stop': 1e-3,
            'sweep_step': 1e-4, 'sweep_compliance': 21.0})
        assert _wait_for(lambda: session.state == 'idle' and sink.of_type('run_ended'))
        assert sink.of_type('prompt') == []

    def test_safe_voltage_asks_nothing(self, session, sink, fake_rm, profile):
        profile['measurement'].update({'safety_voltage_warn_v': 30.0,
                                        'vsource_voltage': 1.0,
                                        'vsource_duration_hours': 0.0})
        session.start(profile, 'source_v', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('sample'))
        assert sink.of_type('prompt') == []
        session.stop()

    def test_silenced_profile_asks_nothing(self, session, sink, fake_rm, profile):
        profile = self._hazardous(profile)
        profile['measurement']['safety_voltage_warn_silenced'] = True
        session.start(profile, 'source_v', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('sample'))
        assert sink.of_type('prompt') == []
        session.stop()

    def test_silence_request_is_recorded(self, session, sink, fake_rm, profile):
        session.start(self._hazardous(profile), 'source_v', 'wafer1', 'alice')
        assert _wait_for(lambda: self._pending(session) is not None)
        prompt = self._pending(session)

        session.answer_prompt(prompt['prompt_id'], 'acknowledge',
                               fields={'silence_for_profile': True})
        assert _wait_for(lambda: any(e.payload['code'] == 'safety_silenced'
                                      for e in sink.of_type('log')))
        session.stop()


class TestStopLatency:
    """A stop must interrupt the wait, not queue behind it."""

    def test_stop_during_a_long_settle_returns_quickly(self, session, sink, fake_rm, profile):
        profile['measurement'].update({
            'settling_time': 10.0, 'vsource_voltage': 1.0,
            'vsource_current_compliance': 0.1, 'vsource_duration_hours': 0.0,
        })
        session.start(profile, 'source_v', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('instrument_connected'))

        began = time.time()
        session.stop()
        assert _wait_for(lambda: session.state == 'idle', timeout=5.0)
        assert time.time() - began < 3.0, "stop waited out the settle"

    def test_interrupted_settle_produces_no_sample(self, session, sink, fake_rm, profile):
        """The read after an unfinished settle would be unsettled data."""
        profile['measurement'].update({
            'settling_time': 10.0, 'vsource_voltage': 1.0,
            'vsource_current_compliance': 0.1, 'vsource_duration_hours': 0.0,
        })
        session.start(profile, 'source_v', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('instrument_connected'))
        session.stop()
        assert _wait_for(lambda: session.state == 'idle', timeout=5.0)

        assert sink.of_type('sample') == []
        assert [e.payload['reason'] for e in sink.of_type('run_ended')] == ['user_stop']

    def test_output_is_still_turned_off(self, session, sink, fake_rm, profile):
        """Unwinding early must not skip the shutdown."""
        profile['measurement'].update({
            'settling_time': 10.0, 'vsource_voltage': 1.0,
            'vsource_current_compliance': 0.1, 'vsource_duration_hours': 0.0,
        })
        session.start(profile, 'source_v', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('instrument_connected'))
        session.stop()
        assert _wait_for(lambda: session.state == 'idle', timeout=5.0)

        fake = fake_rm.opened[-1]
        assert any(cmd.upper().startswith(':OUTP OFF')
                    for op, cmd in fake.command_log if op == 'write')


class TestAbort:
    def test_abort_ends_a_running_measurement(self, session, sink, fake_rm, profile):
        session.start(_four_point(profile, samples=0), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('sample'))
        session.abort()
        assert _wait_for(lambda: session.state == 'idle')
        assert [e.payload['reason'] for e in sink.of_type('run_ended')] == ['aborted']

    def test_abort_releases_a_pending_prompt(self, session, sink, fake_rm, profile):
        profile['measurement'].update({
            'safety_voltage_warn_v': 30.0, 'safety_voltage_warn_silenced': False,
            'vsource_voltage': 60.0, 'vsource_duration_hours': 0.0,
        })
        session.start(profile, 'source_v', 'wafer1', 'alice')
        assert _wait_for(lambda: session.status()['pending_prompt'] is not None)

        session.abort()
        assert _wait_for(lambda: session.state == 'idle')
        assert sink.of_type('instrument_connected') == []

    def test_abort_without_a_run_is_harmless(self, session):
        session.abort()
        assert session.state == 'idle'


class TestPromptTimeout:
    """An unanswered prompt must not hold the instrument forever."""

    def _hazardous(self, profile):
        profile['measurement'].update({
            'safety_voltage_warn_v': 30.0, 'safety_voltage_warn_silenced': False,
            'vsource_voltage': 60.0, 'vsource_duration_hours': 0.0,
        })
        return profile

    def test_unanswered_safety_prompt_ends_the_run(self, session, sink, fake_rm, profile):
        session.start(self._hazardous(profile), 'source_v', 'wafer1', 'alice',
                       prompt_timeout_s=0.2)
        assert _wait_for(lambda: session.state == 'idle', timeout=5.0)

        assert [e.payload['reason'] for e in sink.of_type('run_ended')] == ['prompt_timeout']
        assert sink.of_type('instrument_connected') == []
        assert any(e.payload['code'] == 'prompt_timeout' for e in sink.of_type('log'))

    def test_an_answer_in_time_still_runs(self, session, sink, fake_rm, profile):
        session.start(self._hazardous(profile), 'source_v', 'wafer1', 'alice',
                       prompt_timeout_s=30.0)
        assert _wait_for(lambda: session.status()['pending_prompt'] is not None)
        prompt = session.status()['pending_prompt']
        session.answer_prompt(prompt['prompt_id'], 'acknowledge')

        assert _wait_for(lambda: sink.of_type('sample'))
        session.stop()

    def test_the_qt_path_has_no_timeout(self, fake_rm, tmp_path):
        """An operator at the bench is allowed to take as long as they like."""
        from resistamet_gui.workers import VdpMeasurementWorker

        worker = VdpMeasurementWorker('wafer1', 'alice', {'measurement': {}, 'file': {},
                                                            'display': {}, 'output': {}})
        assert worker._run._prompt_timeout_s is None


class TestPauseClock:
    """A pause must not eat into the run's measuring time."""

    def _timed(self, profile, hours):
        profile['measurement'].update({
            'vsource_voltage': 1.0, 'vsource_current_compliance': 0.1,
            'vsource_duration_hours': hours, 'sampling_rate': 50.0,
        })
        return profile

    def test_paused_time_does_not_count_toward_the_duration(self, session, sink,
                                                              fake_rm, profile):
        # 1.5 s of measuring time, then pause well past that deadline.
        session.start(self._timed(profile, 1.5 / 3600.0), 'source_v', 'wafer1', 'alice')
        assert _wait_for(lambda: sink.of_type('sample'))
        session.pause()
        assert _wait_for(lambda: session.state == 'paused')
        time.sleep(1.8)

        assert session.state == 'paused', "run ended while paused"
        session.resume()
        assert _wait_for(lambda: session.state == 'running')
        # still measuring after the wall-clock deadline has passed
        before = len(sink.of_type('sample'))
        assert _wait_for(lambda: len(sink.of_type('sample')) > before)
        session.stop()

    def test_an_unpaused_run_still_stops_on_time(self, session, sink, fake_rm, profile):
        session.start(self._timed(profile, 0.5 / 3600.0), 'source_v', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle', timeout=10.0)
        assert [e.payload['reason'] for e in sink.of_type('run_ended')] == ['duration']


class TestInstrumentHeldElsewhere:
    """Start refuses at once when another process holds the bus.

    The bench showed why: a second backend was told 'started', connected
    anyway a moment later, and its SCPI landed in the middle of a live run.
    """

    def test_start_is_refused_synchronously(self, session, sink, fake_rm, profile, tmp_path, monkeypatch):
        import subprocess, sys, textwrap
        from resistamet_gui.session import instrument_lock
        from resistamet_gui.session.instrument_lock import InstrumentBusy

        monkeypatch.setattr(instrument_lock, 'default_lock_dir', lambda: tmp_path / 'locks')
        monkeypatch.setattr(instrument_lock, 'ACQUIRE_GRACE_S', 0.3)
        holder_script = textwrap.dedent(f"""
            import sys, time
            from resistamet_gui.session.instrument_lock import hold_instrument
            with hold_instrument('GPIB0::24::INSTR', {str(tmp_path / 'locks')!r}):
                print('held', flush=True)
                time.sleep(10)
        """)
        holder = subprocess.Popen([sys.executable, '-c', holder_script],
                                   stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == 'held'
            with pytest.raises(InstrumentBusy, match='in use by another ResistaMet process'):
                session.start(_four_point(profile), 'four_point', 'wafer1', 'alice')
            assert session.state == 'idle'
            assert sink.events == []  # nothing started, nothing reported
        finally:
            holder.kill()
            holder.wait(timeout=5)

    def test_the_run_releases_the_lock_the_session_took(self, session, sink, fake_rm, profile, tmp_path, monkeypatch):
        from resistamet_gui.session import instrument_lock

        monkeypatch.setattr(instrument_lock, 'default_lock_dir', lambda: tmp_path / 'locks')
        session.start(_four_point(profile), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')
        # Free again: a fresh hold succeeds without waiting.
        with instrument_lock.hold_instrument('GPIB0::24::INSTR', wait_s=0.0):
            pass


class TestSpot:
    SPOT = {'map_id': 'wafer7', 'index': 2, 'label': 'edge', 'x_mm': 1.0, 'y_mm': 2.0}

    def test_rides_in_the_run_settings(self, session, sink, fake_rm, profile):
        session.start(_four_point(profile), 'four_point', 'wafer1', 'alice', spot=self.SPOT)
        assert _wait_for(lambda: session.state == 'idle')
        settings = sink.of_type('run_started')[0].payload['settings']
        assert settings['spot'] == {**self.SPOT, 'angle_deg': None}

    def test_absent_means_no_key_at_all(self, session, sink, fake_rm, profile):
        session.start(_four_point(profile), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')
        assert 'spot' not in sink.of_type('run_started')[0].payload['settings']

    def test_other_modes_are_refused_before_anything_starts(self, session, sink, fake_rm, profile):
        with pytest.raises(ValueError, match="four_point"):
            session.start(profile, 'resistance', 'wafer1', 'alice', spot=self.SPOT)
        assert session.state == 'idle'
        assert sink.events == []
        assert fake_rm.opened == []

    def test_a_bad_spot_is_refused_before_anything_starts(self, session, sink, fake_rm, profile):
        with pytest.raises(ValueError):
            session.start(_four_point(profile), 'four_point', 'wafer1', 'alice',
                          spot={**self.SPOT, 'map_id': '../wafer7'})
        assert session.state == 'idle'
        assert fake_rm.opened == []


def _on_a_wafer(profile, samples=2):
    """A 50.8 mm wafer, described by the legacy keys a profile holds today."""
    _four_point(profile, samples=samples)
    profile['measurement'].update({'fpp_geometry': 'circle', 'fpp_diameter_cm': 5.08})
    return profile


def _writes(fake_rm):
    return [cmd.upper() for fake in fake_rm.opened
            for op, cmd in fake.command_log if op == 'write']


class TestSpotGeometry:
    def test_a_tip_off_the_sample_is_refused_before_the_output_turns_on(
            self, session, sink, fake_rm, profile):
        spot = {'map_id': 'wafer7', 'index': 3, 'label': 'too far', 'x_mm': 25.0, 'y_mm': 0.0}
        session.start(_on_a_wafer(profile), 'four_point', 'wafer1', 'alice', spot=spot)
        assert _wait_for(lambda: session.state == 'idle')

        # The instrument was never opened, so nothing at all was written to it.
        assert fake_rm.opened == []
        assert not any(cmd.startswith(':OUTP ON') for cmd in _writes(fake_rm))

        warning = sink.of_type('geometry_warning')[0].payload
        assert warning['refused'] is True
        assert warning['reason'] == 'off_sample'
        assert warning['spot']['label'] == 'too far'
        assert warning['edge_clearance_s'] < 0
        assert warning['factor_here'] is None
        errors = sink.of_type('error')
        assert [e.payload['code'] for e in errors] == ['spot_off_sample']
        assert 'off the sample' in errors[0].payload['message']
        ended = sink.of_type('run_ended')[0].payload
        assert (ended['reason'], ended['ok'], ended['path']) == ('spot_refused', False, None)
        assert sink.of_type('file_opened') == []

    def test_the_instrument_is_free_again_after_a_refusal(self, session, sink, fake_rm, profile):
        spot = {'map_id': 'wafer7', 'index': 3, 'label': 'too far', 'x_mm': 25.0, 'y_mm': 0.0}
        session.start(_on_a_wafer(profile), 'four_point', 'wafer1', 'alice', spot=spot)
        assert _wait_for(lambda: session.state == 'idle')
        session.start(_on_a_wafer(profile), 'four_point', 'wafer1', 'alice',
                      spot={**spot, 'x_mm': 0.0})
        assert _wait_for(lambda: session.state == 'idle')
        assert sink.of_type('run_ended')[-1].payload['reason'] == 'target_samples'

    def test_a_spot_near_the_edge_warns_and_still_runs(self, session, sink, fake_rm, profile):
        spot = {'map_id': 'wafer7', 'index': 2, 'label': 'rim', 'x_mm': 20.0, 'y_mm': 0.0,
                'angle_deg': 90.0}
        session.start(_on_a_wafer(profile), 'four_point', 'wafer1', 'alice', spot=spot)
        assert _wait_for(lambda: session.state == 'idle')

        warning = sink.of_type('geometry_warning')[0].payload
        assert warning['refused'] is False
        assert warning['reason'] == 'near_edge'
        assert abs(warning['relative_error']) * 100.0 > warning['edge_warn_pct'] == 1.0
        assert warning['edge_clearance_s'] > 0
        assert len(sink.of_type('sample')) == 2
        assert sink.of_type('run_ended')[0].payload['ok'] is True
        # Said before the first sample, and before the instrument is touched.
        types = sink.types()
        assert types.index('geometry_warning') < types.index('instrument_connected')

    def test_a_centred_spot_raises_nothing(self, session, sink, fake_rm, profile):
        spot = {'map_id': 'wafer7', 'index': 0, 'label': 'centre', 'x_mm': 0.0, 'y_mm': 0.0}
        session.start(_on_a_wafer(profile), 'four_point', 'wafer1', 'alice', spot=spot)
        assert _wait_for(lambda: session.state == 'idle')
        assert sink.of_type('geometry_warning') == []
        assert len(sink.of_type('sample')) == 2

    def test_a_raised_threshold_silences_the_warning(self, session, sink, fake_rm, profile):
        spot = {'map_id': 'wafer7', 'index': 2, 'label': 'rim', 'x_mm': 20.0, 'y_mm': 0.0,
                'angle_deg': 90.0}
        session.start(_on_a_wafer(profile), 'four_point', 'wafer1', 'alice', spot=spot,
                      overrides={'fpp_edge_warn_pct': 50.0})
        assert _wait_for(lambda: session.state == 'idle')
        assert sink.of_type('geometry_warning') == []

    def test_an_outline_that_cannot_be_described_refuses_the_run(self, sink, fake_rm, profile):
        """The strict resolver catches this for a session; a run started with
        hand-built settings (the PySide6 path) must refuse it too."""
        from resistamet_gui.session.continuous_run import ContinuousRun
        from resistamet_gui.session.control import RunControl
        from resistamet_gui.session.emitter import EventEmitter

        settings = _four_point(profile)
        settings['measurement']['fpp_sample_shape'] = 'circle'   # and no diameter
        settings['spot'] = {'map_id': 'wafer7', 'index': 0, 'label': 'centre'}
        ContinuousRun('four_point', 'wafer1', 'alice', settings, RunControl(),
                      EventEmitter(sink)).execute()

        assert fake_rm.opened == []
        assert [e.payload['code'] for e in sink.of_type('error')] == ['spot_invalid']
        assert sink.of_type('run_ended')[0].payload['reason'] == 'spot_refused'


class TestSpotInTheFileHeader:
    def _header(self, sink):
        from resistamet_gui.data_export import parse_metadata
        return parse_metadata(sink.of_type('run_ended')[-1].payload['path'])

    def test_a_spot_is_written_with_the_sample_and_the_position_effect(
            self, session, sink, fake_rm, profile):
        spot = {'map_id': 'wafer7', 'index': 2, 'label': 'rim', 'x_mm': 20.0, 'y_mm': 0.0,
                'angle_deg': 90.0}
        profile['measurement']['fpp_thickness_um'] = 100.0
        session.start(_on_a_wafer(profile), 'four_point', 'wafer1', 'alice', spot=spot)
        assert _wait_for(lambda: session.state == 'idle')

        header = self._header(sink)
        warning = sink.of_type('geometry_warning')[0].payload
        assert header['spot.map_id'] == 'wafer7'
        assert header['spot.index'] == 2
        assert header['spot.label'] == 'rim'
        assert (header['spot.x_mm'], header['spot.y_mm']) == (20.0, 0.0)
        assert header['spot.angle_deg'] == 90.0
        assert header['spot.sample.shape'] == 'circle'
        assert header['spot.sample.diameter_mm'] == pytest.approx(50.8)
        assert header['spot.position_correction'] == 'warn'
        assert header['spot.edge_warn_pct'] == 1.0
        assert header['spot.factor_here'] == warning['factor_here']
        assert header['spot.factor_centre'] == warning['factor_centre']
        assert header['spot.relative_error'] == warning['relative_error']
        assert header['spot.edge_clearance_s'] == warning['edge_clearance_s']
        # The wafer is described by the legacy keys, so the rows use F84
        # Table 3 for it; with a thickness entered they have an Rs to compare.
        assert header['spot.factor_rows'] == warning['factor_rows'] == pytest.approx(4.517)
        assert header['spot.relative_error_rows'] == warning['relative_error_rows']
        assert header['spot.relative_error_rows'] == pytest.approx(
            4.517 / header['spot.factor_here'] - 1.0)

    def test_a_label_only_spot_has_no_position_effect(self, session, sink, fake_rm, profile):
        session.start(_four_point(profile), 'four_point', 'wafer1', 'alice',
                      spot={'map_id': 'wafer7', 'index': 0, 'label': 'somewhere'})
        assert _wait_for(lambda: session.state == 'idle')
        header = self._header(sink)
        assert header['spot.label'] == 'somewhere'
        assert header['spot.x_mm'] is None
        assert header['spot.sample.shape'] == 'unbounded'
        assert 'spot.factor_here' not in header

    def test_a_run_without_a_spot_has_no_spot_keys(self, session, sink, fake_rm, profile):
        session.start(_four_point(profile), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')
        assert not [key for key in self._header(sink) if key.startswith('spot.')]


def _csv_columns(path):
    import csv
    with open(path) as handle:
        rows = [r for r in csv.reader(handle) if r and not r[0].startswith('#')]
    header, body = rows[0], rows[1:]
    return {name: [row[i] for row in body] for i, name in enumerate(header)}


class TestSpotStatisticsAtTheEndOfARun:
    #: What a four-point header held before spots existed. A run without a
    #: spot must still write exactly this.
    HEADER_KEYS = {
        'resistamet_format_version', 'user', 'sample', 'mode', 'started_at',
        'software_version', 'instrument', 'gpib_address', 'sampling_rate_hz', 'nplc',
        'settling_time_s', 'units',
        'params.source_current_A', 'params.voltage_compliance_V',
        'params.voltage_auto_range', 'params.probe_spacing_cm', 'params.thickness_um',
        'params.k_factor', 'params.alpha', 'params.model', 'params.target_samples',
        'params.auto_zero',
    }
    FOOTER_KEYS_BEFORE = {'ended_at', 'total_samples', 'duration_s'}
    #: The one thing that changed for a run without a spot.
    FOOTER_KEYS_ADDED = {'spot_stats.n', 'spot_stats.n_excluded'} | {
        f'spot_stats.{quantity}.{field}'
        for quantity in ('rs', 'rho', 'sigma')
        for field in ('n', 'mean', 'sd', 'rsd_pct', 'u_stat', 'u_inst', 'u_total')
    }

    def _run(self, session, sink, profile, mode='four_point', **kwargs):
        session.start(profile, mode, 'wafer1', 'alice', **kwargs)
        assert _wait_for(lambda: session.state == 'idle')
        return sink.of_type('run_ended')[-1].payload['path']

    def test_without_a_spot_only_the_footer_statistics_are_new(
            self, session, sink, fake_rm, profile):
        from resistamet_gui.data_export import parse_metadata
        path = self._run(session, sink, _four_point(profile, samples=3))
        keys = set(parse_metadata(path))
        assert keys == self.HEADER_KEYS | self.FOOTER_KEYS_BEFORE | self.FOOTER_KEYS_ADDED

    def test_other_modes_write_the_footer_they_always_did(self, session, sink, fake_rm, profile):
        profile['measurement'].update({'res_test_current': 1e-3, 'res_voltage_compliance': 5.0})
        session.start(profile, 'resistance', 'wafer1', 'alice')
        assert _wait_for(lambda: len(sink.of_type('sample')) >= 2)
        session.stop()
        assert _wait_for(lambda: session.state == 'idle')
        finalized = sink.of_type('file_finalized')[0].payload
        assert set(finalized['end_metadata']) == self.FOOTER_KEYS_BEFORE
        assert sink.of_type('spot_complete') == []

    def test_the_footer_equals_the_statistics_of_the_rows(self, session, sink, fake_rm, profile):
        from resistamet_gui.data_export import parse_metadata
        profile['measurement']['fpp_thickness_um'] = 100.0
        path = self._run(session, sink, _four_point(profile, samples=4))
        footer = parse_metadata(path)
        columns = _csv_columns(path)

        rs = [float(v) for v in columns['Rs_ohm_sq']]
        assert footer['spot_stats.n'] == 4 == len(rs)
        assert footer['spot_stats.n_excluded'] == 0
        assert footer['spot_stats.rs.n'] == 4
        assert footer['spot_stats.rs.mean'] == pytest.approx(sum(rs) / 4, rel=1e-5)
        rho = [float(v) for v in columns['rho_ohm_cm']]
        assert footer['spot_stats.rho.mean'] == pytest.approx(sum(rho) / 4, rel=1e-5)
        sigma = [float(v) for v in columns['sigma_S_cm']]
        assert footer['spot_stats.sigma.mean'] == pytest.approx(sum(sigma) / 4, rel=1e-5)
        assert footer['spot_stats.rs.u_inst'] > 0
        assert footer['spot_stats.rs.u_total'] >= footer['spot_stats.rs.u_inst']

    def test_a_quantity_the_rows_do_not_have_is_empty_not_wrong(
            self, session, sink, fake_rm, profile):
        """With no thickness entered the rows hold no conductivity."""
        import math
        from resistamet_gui.data_export import parse_metadata
        profile['measurement'].update({'fpp_thickness_um': 0.0, 'fpp_model': 'thin_film'})
        path = self._run(session, sink, _four_point(profile, samples=2))
        footer = parse_metadata(path)
        assert footer['spot_stats.rs.n'] == 2
        assert footer['spot_stats.sigma.n'] == 0
        assert math.isnan(footer['spot_stats.sigma.mean'])
        assert math.isnan(footer['spot_stats.sigma.u_total'])

    def test_spot_complete_carries_the_footer_and_the_spot(self, session, sink, fake_rm, profile):
        spot = {'map_id': 'wafer7', 'index': 4, 'label': 'D'}
        path = self._run(session, sink, _four_point(profile, samples=3), spot=spot)
        complete = sink.of_type('spot_complete')[0].payload
        finalized = sink.of_type('file_finalized')[0].payload

        assert complete['path'] == path
        assert complete['spot'] == {**spot, 'x_mm': None, 'y_mm': None, 'angle_deg': None}
        assert complete['stats']['n'] == 3
        assert complete['stats']['rs']['mean'] == finalized['end_metadata']['spot_stats']['rs']['mean']
        types = sink.types()
        assert types.index('file_finalized') < types.index('spot_complete') < types.index('run_ended')

    def test_a_run_without_a_spot_still_reports_its_statistics(self, session, sink, fake_rm, profile):
        self._run(session, sink, _four_point(profile, samples=2))
        complete = sink.of_type('spot_complete')[0].payload
        assert complete['spot'] is None
        assert complete['stats']['n'] == 2

    def test_samples_in_compliance_are_counted_and_left_out(self, session, sink, fake_rm, profile):
        # 100 ohm DUT at 0.1 A wants 10 V; the 5 V limit puts every sample in
        # compliance, so each row is a bound and none is a measurement.
        profile = _four_point(profile, samples=2)
        profile['measurement'].update({'fpp_current': 0.1, 'fpp_power_warn_w': 5.0,
                                       'fpp_power_stop_w': 10.0})
        self._run(session, sink, profile)
        stats = sink.of_type('spot_complete')[0].payload['stats']
        assert (stats['n'], stats['n_excluded']) == (0, 2)
        assert stats['rs']['n'] == 0


class TestSpotRoundTripsThroughHdf5:
    def test_header_footer_and_map(self, session, sink, fake_rm, profile):
        h5py = pytest.importorskip("h5py")
        from pathlib import Path
        from resistamet_gui.session.spot_map import assemble_map

        profile['output']['format'] = 'hdf5'
        # The F84 path reports Rs through the thickness; without one it is NaN.
        profile['measurement']['fpp_thickness_um'] = 100.0
        spot = {'map_id': 'wafer7', 'index': 1, 'label': 'rim', 'x_mm': 20.0, 'y_mm': 0.0,
                'angle_deg': 90.0}
        session.start(_on_a_wafer(profile, samples=3), 'four_point', 'wafer1', 'alice', spot=spot)
        assert _wait_for(lambda: session.state == 'idle')

        path = sink.of_type('run_ended')[0].payload['path']
        complete = sink.of_type('spot_complete')[0].payload
        with h5py.File(path, 'r') as handle:
            attrs = dict(handle.attrs)
        assert attrs['spot.map_id'] == 'wafer7'
        assert attrs['spot.label'] == 'rim'
        assert attrs['spot.sample.shape'] == 'circle'
        assert attrs['spot.edge_clearance_s'] > 0
        assert attrs['spot_stats.n'] == 3
        assert attrs['spot_stats.rs.mean'] == complete['stats']['rs']['mean']

        found = assemble_map(Path(path).parent, 'wafer7')
        assert [s.label for s in found.spots] == ['rim']
        assert found.spots[0].stats.rs.mean == complete['stats']['rs']['mean']
        assert (Path(path).parent / 'wafer7_map.json').exists()


class TestASpotCheckThatRaises:
    """run_ended is the last event of every run, and the lock comes back."""

    SPOT = {'map_id': 'wafer7', 'index': 0, 'label': 'centre', 'x_mm': 0.0, 'y_mm': 0.0}

    def _assert_refused_and_free(self, session, sink, fake_rm, profile):
        assert _wait_for(lambda: session.state == 'idle')
        assert sink.types()[0] == 'run_started'
        assert sink.types()[-1] == 'run_ended'
        ended = sink.events[-1].payload
        assert (ended['reason'], ended['ok'], ended['path']) == ('spot_refused', False, None)
        assert fake_rm.opened == []
        # Another process would be refused while the lock is held; taking it
        # here proves the refused run let go.
        from resistamet_gui.session.instrument_lock import HeldInstrument
        HeldInstrument(profile['measurement']['gpib_address']).release()

    @pytest.mark.parametrize("failure", [OverflowError("math range error"),
                                         TypeError("not a number"), RuntimeError("anything")])
    def test_any_exception_is_a_refusal(self, session, sink, fake_rm, profile, monkeypatch,
                                         failure):
        from resistamet_gui.session import continuous_run

        def explode(settings):
            raise failure
        monkeypatch.setattr(continuous_run, 'spot_record_from_settings', explode)
        session.start(_on_a_wafer(profile), 'four_point', 'wafer1', 'alice', spot=self.SPOT)
        self._assert_refused_and_free(session, sink, fake_rm, profile)
        error = sink.of_type('error')[0].payload
        assert error['code'] == 'spot_invalid'
        assert str(failure) in error['message']

    def test_a_null_threshold_in_hand_built_settings(self, sink, fake_rm, profile):
        """float(None): a TypeError, not the ValueError the check used to expect."""
        from resistamet_gui.session.continuous_run import ContinuousRun
        from resistamet_gui.session.control import RunControl
        from resistamet_gui.session.emitter import EventEmitter

        settings = _on_a_wafer(profile)
        settings['measurement']['fpp_edge_warn_pct'] = None
        settings['spot'] = dict(self.SPOT)
        ContinuousRun('four_point', 'wafer1', 'alice', settings, RunControl(),
                      EventEmitter(sink)).execute()

        assert sink.types()[-1] == 'run_ended'
        assert sink.events[-1].payload['reason'] == 'spot_refused'
        assert [e.payload['code'] for e in sink.of_type('error')] == ['spot_invalid']
        assert fake_rm.opened == []
        from resistamet_gui.session.instrument_lock import HeldInstrument
        HeldInstrument(settings['measurement']['gpib_address']).release()

    def test_a_payload_the_event_model_rejects(self, session, sink, fake_rm, profile, monkeypatch):
        """The check's own emit is inside the guard too."""
        from resistamet_gui.session import continuous_run, spot_record

        def off_sample_with_a_bad_number(settings):
            record = spot_record.spot_record_from_settings(settings)
            position = spot_record.SpotPosition(edge_clearance_s='not a number')
            return spot_record.SpotRecord(
                spot=record.spot, geometry=record.geometry, angle_deg=0.0,
                position_correction='warn', edge_warn_pct=1.0, position=position)
        monkeypatch.setattr(continuous_run, 'spot_record_from_settings',
                            off_sample_with_a_bad_number)
        session.start(_on_a_wafer(profile), 'four_point', 'wafer1', 'alice', spot=self.SPOT)
        self._assert_refused_and_free(session, sink, fake_rm, profile)


class TestKeepingSamplesForStatisticsCannotHurtTheRun:
    def test_a_failure_is_not_a_write_failure(self, session, sink, fake_rm, profile, monkeypatch):
        """Three write failures stop a run; this is not one of them."""
        from resistamet_gui.session import spot_stats

        def explode(self, *args, **kwargs):
            raise RuntimeError("statistics are broken")
        monkeypatch.setattr(spot_stats.SpotSamples, 'add', explode)

        session.start(_four_point(profile, samples=5), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')

        ended = sink.of_type('run_ended')[0].payload
        assert (ended['reason'], ended['ok'], ended['samples']) == ('target_samples', True, 5)
        codes = [e.payload['code'] for e in sink.of_type('log')]
        assert codes.count('spot_sample_failed') == 1          # said once, not per sample
        assert 'write_failed' not in codes
        assert sink.of_type('error') == []
        # The file is whole; its statistics are empty rather than wrong.
        assert sink.of_type('spot_complete')[0].payload['stats']['n'] == 0

    def test_a_row_that_was_not_written_is_not_counted(self, session, sink, fake_rm, profile,
                                                       monkeypatch):
        from resistamet_gui import data_export

        real_write = data_export.CsvExporter.write_row
        calls = {'n': 0}

        def fail_the_second(self, row):
            calls['n'] += 1
            if calls['n'] == 2:
                raise OSError("disk hiccup")
            return real_write(self, row)
        monkeypatch.setattr(data_export.CsvExporter, 'write_row', fail_the_second)

        session.start(_four_point(profile, samples=4), 'four_point', 'wafer1', 'alice')
        assert _wait_for(lambda: session.state == 'idle')

        finalized = sink.of_type('file_finalized')[0].payload['end_metadata']
        assert finalized['total_samples'] == 3
        assert finalized['spot_stats']['n'] == 3


class TestAHandEditedPositionCorrection:
    def test_the_file_says_warn_and_the_log_says_why(self, sink, fake_rm, profile):
        """The PySide6 path: settings no schema has validated."""
        from resistamet_gui.data_export import parse_metadata
        from resistamet_gui.session.continuous_run import ContinuousRun
        from resistamet_gui.session.control import RunControl
        from resistamet_gui.session.emitter import EventEmitter

        settings = _four_point(profile)
        settings['measurement']['fpp_position_correction'] = 'apply'
        settings['spot'] = {'map_id': 'wafer7', 'index': 0, 'label': 'centre'}
        ContinuousRun('four_point', 'wafer1', 'alice', settings, RunControl(),
                      EventEmitter(sink)).execute()

        ended = sink.of_type('run_ended')[0].payload
        assert ended['reason'] == 'target_samples'
        assert parse_metadata(ended['path'])['spot.position_correction'] == 'warn'
        warnings = [e.payload for e in sink.of_type('log')
                    if e.payload['code'] == 'position_correction_ignored']
        assert len(warnings) == 1 and warnings[0]['level'] == 'warning'
        assert "'apply'" in warnings[0]['message']


class TestTheWarningIsAboutTheFactorTheRowsUse:
    SQUARE = {'fpp_spacing_cm': 0.1, 'fpp_sample_shape': 'rectangle',
              'fpp_sample_width_mm': 20.0, 'fpp_sample_length_mm': 20.0}

    def test_rows_that_assume_a_larger_sample_warn_even_at_the_centre(
            self, session, sink, fake_rm, profile):
        """Outline: a 20 s square. Rows: K = 4.532, an unbounded sheet. The
        centre of the outline costs nothing against its own centred factor
        and 1.8 % against what the file's Rs was computed with."""
        spot = {'map_id': 'chip3', 'index': 0, 'label': 'centre', 'x_mm': 0.0, 'y_mm': 0.0}
        session.start(_four_point(profile), 'four_point', 'chip3', 'alice', spot=spot,
                      overrides=self.SQUARE)
        assert _wait_for(lambda: session.state == 'idle')

        warning = sink.of_type('geometry_warning')[0].payload
        assert warning['compared_with'] == 'rows'
        assert warning['relative_error'] == pytest.approx(0.0, abs=1e-12)
        assert warning['relative_error_rows'] == pytest.approx(0.0185, abs=5e-4)
        assert 'the geometry factor this run applies (4.532)' in warning['message']
        assert '1.8 %' in warning['message']
        assert sink.of_type('run_ended')[0].payload['ok'] is True

    def test_without_a_rows_factor_the_message_says_centre(self, session, sink, fake_rm, profile):
        # The F84 path with no thickness has no Rs, so nothing to compare.
        spot = {'map_id': 'wafer7', 'index': 2, 'label': 'rim', 'x_mm': 20.0, 'y_mm': 0.0,
                'angle_deg': 90.0}
        session.start(_on_a_wafer(profile), 'four_point', 'wafer1', 'alice', spot=spot)
        assert _wait_for(lambda: session.state == 'idle')
        warning = sink.of_type('geometry_warning')[0].payload
        assert warning['compared_with'] == 'centre'
        assert warning['factor_rows'] is None and warning['relative_error_rows'] is None
        assert 'the factor at the centre of the sample' in warning['message']

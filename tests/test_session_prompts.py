"""Operator prompts: who may answer, with what, and what a stop does to one.

A prompt stands between the run and an energised output, so everything here
is about failing closed: an answer the prompt did not offer is refused, only
'proceed' turns the output on, and a stop always wins over a question.
"""
import threading
import time

import pytest

from resistamet_gui.session.control import InvalidPromptChoice, RunControl
from resistamet_gui.session.emitter import EventEmitter, ListSink
from resistamet_gui.session.vdp_run import VdpRun


def _vdp_settings(tmp_path):
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


def _writes(fake_rm):
    return [cmd.upper() for fake in fake_rm.opened
            for op, cmd in fake.command_log if op == 'write']


class TestAnAnswerMustBeOneThePromptOffered:
    def test_an_unknown_choice_is_refused_and_the_prompt_stays_open(self):
        control = RunControl()
        prompt = control.raise_prompt('vdp_geometry', ['proceed', 'abort'])
        with pytest.raises(InvalidPromptChoice) as refused:
            control.answer_prompt(prompt.prompt_id, 'cancel')
        assert "'cancel'" in str(refused.value)
        assert 'proceed' in str(refused.value)
        assert control.pending_prompt is prompt
        assert not control.proceed_event.is_set()
        # The real answer is still accepted afterwards.
        assert control.answer_prompt(prompt.prompt_id, 'abort') is True

    def test_the_refusal_is_a_value_error(self):
        assert issubclass(InvalidPromptChoice, ValueError)

    def test_a_stale_id_is_still_false_whatever_the_choice(self):
        control = RunControl()
        control.raise_prompt('vdp_geometry', ['proceed', 'abort'])
        assert control.answer_prompt('nope', 'garbage') is False


class TestPromptIdsNameTheirRun:
    def test_the_same_prompt_in_two_runs_has_two_ids(self):
        first = RunControl(run_id='run-1').raise_prompt('safety_voltage_ack', ['acknowledge'])
        second = RunControl(run_id='run-2').raise_prompt('safety_voltage_ack', ['acknowledge'])
        assert first.prompt_id != second.prompt_id
        assert 'run-1' in first.prompt_id and 'run-2' in second.prompt_id

    def test_an_answer_meant_for_the_last_run_is_stale(self):
        old = RunControl(run_id='run-1').raise_prompt('safety_voltage_ack', ['acknowledge'])
        control = RunControl(run_id='run-2')
        control.raise_prompt('safety_voltage_ack', ['acknowledge'])
        assert control.answer_prompt(old.prompt_id, 'acknowledge') is False

    def test_without_a_run_id_the_id_is_what_it_always_was(self):
        assert RunControl().raise_prompt('vdp_geometry', ['proceed']).prompt_id == 'vdp_geometry-1'


class TestOnlyProceedEnergisesAGeometry:
    """The loop used to go ahead on anything that was not 'abort'."""

    def _start(self, tmp_path):
        control, sink = RunControl(), ListSink()
        run = VdpRun("wafer1", "alice", _vdp_settings(tmp_path), control, EventEmitter(sink))
        thread = threading.Thread(target=run.execute, daemon=True)
        thread.start()
        assert _wait_for(lambda: control.pending_prompt is not None)
        return control, sink, thread

    def test_an_unoffered_answer_leaves_the_output_off(self, fake_rm, tmp_path):
        control, sink, thread = self._start(tmp_path)
        with pytest.raises(InvalidPromptChoice):
            control.answer_prompt(control.pending_prompt.prompt_id, 'cancel')
        assert control.pending_prompt is not None, "the run is still waiting"
        assert ':OUTP ON' not in _writes(fake_rm)
        control.finish('user_stop')
        thread.join(5.0)
        assert ':OUTP ON' not in _writes(fake_rm)

    def test_an_answer_that_is_not_proceed_aborts(self, fake_rm, tmp_path, monkeypatch):
        """Belt and braces: even past the control's check, only 'proceed' measures."""
        control, sink, thread = self._start(tmp_path)
        prompt = control.pending_prompt
        monkeypatch.setattr(control, 'wait_for_prompt', lambda timeout=None: ('cancel', {}))
        # Wake the real wait the run is already parked in with a valid answer;
        # the next geometry's wait is the patched one.
        control.answer_prompt(prompt.prompt_id, 'proceed')
        thread.join(5.0)
        assert not thread.is_alive()
        assert _writes(fake_rm).count(':OUTP ON') == 1
        assert len(sink.of_type('vdp_geometry_complete')) == 1
        assert sink.of_type('vdp_result') == []

    def test_proceed_measures(self, fake_rm, tmp_path):
        control, sink, thread = self._start(tmp_path)
        control.answer_prompt(control.pending_prompt.prompt_id, 'proceed')
        assert _wait_for(lambda: sink.of_type('vdp_geometry_complete'))
        control.finish('user_stop')
        thread.join(5.0)
        assert ':OUTP ON' in _writes(fake_rm)


class TestTheSessionRefusesWithoutRaising:
    """The HTTP route maps False to a conflict and knows no other outcome."""

    def test_an_unoffered_choice_is_false_and_the_run_waits_on(self, fake_rm, tmp_path, caplog):
        from resistamet_gui.session.manager import MeasurementSession

        profile = _vdp_settings(tmp_path)
        from resistamet_gui.constants import DEFAULT_SETTINGS
        import copy
        merged = copy.deepcopy({k: DEFAULT_SETTINGS[k] for k in
                                ('measurement', 'display', 'file', 'output')})
        merged['measurement'].update(profile['measurement'])
        merged['file']['data_directory'] = str(tmp_path / 'data')

        session = MeasurementSession(ListSink())
        try:
            run_id = session.start(merged, 'vdp', 'wafer1', 'alice')
            assert _wait_for(lambda: session.status()['pending_prompt'] is not None)
            prompt = session.status()['pending_prompt']
            assert prompt['prompt_id'].startswith(run_id)
            with caplog.at_level('WARNING'):
                assert session.answer_prompt(prompt['prompt_id'], 'cancel') is False
            assert 'cancel' in caplog.text
            assert session.status()['pending_prompt'] is not None
            assert ':OUTP ON' not in _writes(fake_rm)
        finally:
            session.close(timeout=5.0)


def _returns_within(seconds, target):
    """Run ``target`` on a thread; its result, or fail if it is still parked."""
    result = []
    thread = threading.Thread(target=lambda: result.append(target()), daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "still waiting: the stop was swallowed"
    return result[0]


class TestAStopIsNeverSwallowedByAPrompt:
    """finish() sets the proceed gate; raising a prompt used to clear it again.

    With no timeout, which is how the Qt workers run, the run then sat on the
    question for ever with the instrument held and Stop already pressed.
    """

    def test_a_prompt_raised_after_the_stop_does_not_wait(self):
        control = RunControl()
        control.finish('user_stop')
        control.raise_prompt('vdp_geometry', ['proceed', 'abort'])
        assert _returns_within(2.0, lambda: control.wait_for_prompt(None)) == (None, {})

    def test_the_wait_returns_on_a_stop_even_if_the_gate_was_cleared(self):
        control = RunControl()
        control.raise_prompt('vdp_geometry', ['proceed', 'abort'])
        control.finish('user_stop')
        control.proceed_event.clear()
        assert _returns_within(2.0, lambda: control.wait_for_prompt(None)) == (None, {})

    def test_a_prompt_before_any_stop_still_waits(self):
        control = RunControl()
        control.raise_prompt('vdp_geometry', ['proceed', 'abort'])
        assert control.wait_for_prompt(0.05) == (None, {})
        assert not control.stopped()

    def test_the_geometry_prompt(self, fake_rm, tmp_path):
        """The stop lands between the loop's running check and the prompt."""
        control, sink = RunControl(), ListSink()
        run = VdpRun("wafer1", "alice", _vdp_settings(tmp_path), control, EventEmitter(sink),
                      prompt_timeout_s=None)
        raise_prompt = control.raise_prompt

        def stop_then_raise(*args, **kwargs):
            control.finish('user_stop')
            return raise_prompt(*args, **kwargs)

        control.raise_prompt = stop_then_raise
        _returns_within(5.0, run.execute)
        assert [e.payload['reason'] for e in sink.of_type('run_ended')] == ['user_stop']
        assert not any(cmd.startswith(':OUTP ON') for cmd in _writes(fake_rm))

    def test_the_safety_prompt(self, tmp_path):
        """A stop that is already in when the question would be asked."""
        from resistamet_gui.session.continuous_run import ContinuousRun

        settings = _vdp_settings(tmp_path)
        settings['measurement'].update({'vsource_voltage': 60.0})
        control, sink = RunControl(), ListSink()
        run = ContinuousRun('source_v', "wafer1", "alice", settings, control,
                             EventEmitter(sink), safety_ack='prompt', prompt_timeout_s=None)
        control.finish('user_stop')
        assert _returns_within(2.0, run._safety_prompt_declined) is True
        assert sink.of_type('prompt') == [], "nobody is asked a question after Stop"

    def test_the_safety_prompt_of_a_van_der_pauw_run(self, tmp_path):
        settings = _vdp_settings(tmp_path)
        settings['measurement'].update({'vdp_voltage_compliance': 60.0})
        control, sink = RunControl(), ListSink()
        run = VdpRun("wafer1", "alice", settings, control, EventEmitter(sink),
                      safety_ack='prompt', prompt_timeout_s=None)
        control.finish('user_stop')
        assert _returns_within(2.0, run._safety_prompt_declined) is True
        assert sink.of_type('prompt') == []

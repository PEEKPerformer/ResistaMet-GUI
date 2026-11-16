"""The 4PP current finder's controls and its Proceed/Abort dialog in the window.

The settings round trip (gather, user switch, profile files) is covered with
every other tab key in ``test_gui_tab_settings.py``; the worker side in
``test_workers.py``.
"""
import pytest

pytest.importorskip("PySide6")


class _FakeWorker:
    def __init__(self):
        self.responses = []

    def autoselect_respond(self, proceed):
        self.responses.append(proceed)


class TestControls:
    def test_off_leaves_the_current_editable_and_the_target_greyed(self, main_window):
        w = main_window.tab_four_point
        assert w.fpp_autoselect_current.isChecked() is False
        assert w.fpp_current.isEnabled() is True
        assert w.fpp_autoselect_sigfigs.isEnabled() is False

    def test_on_greys_the_current_and_enables_the_target(self, main_window):
        w = main_window.tab_four_point
        w.fpp_autoselect_current.setChecked(True)
        assert w.fpp_current.isEnabled() is False
        assert w.fpp_autoselect_sigfigs.isEnabled() is True
        w.fpp_autoselect_current.setChecked(False)
        assert w.fpp_current.isEnabled() is True
        assert w.fpp_autoselect_sigfigs.isEnabled() is False

    def test_gather_and_update_round_trip(self, main_window):
        w = main_window.tab_four_point
        w.fpp_autoselect_current.setChecked(True)
        w.fpp_autoselect_sigfigs.setValue(5)
        gathered = main_window.gather_settings_for_mode('four_point')['measurement']
        assert gathered['fpp_autoselect_current'] is True
        assert gathered['fpp_autoselect_sigfigs'] == 5

        # The stored profile still has it off at 4: loading it puts both back,
        # and the greying follows.
        main_window.update_ui_from_settings()
        assert w.fpp_autoselect_current.isChecked() is False
        assert w.fpp_autoselect_sigfigs.value() == 4
        assert w.fpp_current.isEnabled() is True

        main_window.user_settings['measurement'].update(
            {'fpp_autoselect_current': True, 'fpp_autoselect_sigfigs': 3})
        main_window.update_ui_from_settings()
        assert w.fpp_autoselect_current.isChecked() is True
        assert w.fpp_autoselect_sigfigs.value() == 3
        assert w.fpp_current.isEnabled() is False

    def test_the_chosen_current_is_shown_in_the_field(self, main_window):
        w = main_window.tab_four_point
        main_window.on_autoselect_current_chosen(2.5e-4)
        assert w.fpp_current.value() == pytest.approx(2.5e-4)


class TestDecisionDialog:
    INFO = {'verdict': 'marginal', 'message': 'R≈0.001 Ω: 1.3 significant figures',
            'current_a': 0.02}

    def test_proceed_reaches_the_worker(self, main_window, monkeypatch):
        main_window.measurement_worker = worker = _FakeWorker()
        monkeypatch.setattr(main_window, "_ask_autoselect_proceed", lambda *a: True)
        main_window.on_autoselect_decision(dict(self.INFO))
        assert worker.responses == [True]

    def test_abort_reaches_the_worker(self, main_window, monkeypatch):
        main_window.measurement_worker = worker = _FakeWorker()
        monkeypatch.setattr(main_window, "_ask_autoselect_proceed", lambda *a: False)
        main_window.on_autoselect_decision(dict(self.INFO))
        assert worker.responses == [False]

    def test_no_worker_asks_nothing(self, main_window, monkeypatch):
        main_window.measurement_worker = None
        monkeypatch.setattr(main_window, "_ask_autoselect_proceed",
                            lambda *a: pytest.fail("asked with no run"))
        main_window.on_autoselect_decision(dict(self.INFO))

    @pytest.mark.parametrize('verdict, word', [
        ('marginal', 'not reached'), ('too_conductive', 'conductive'),
        ('too_resistive', 'resistive')])
    def test_the_dialog_says_the_verdict_the_reason_and_the_current(
            self, main_window, monkeypatch, verdict, word):
        seen = []
        main_window.measurement_worker = _FakeWorker()
        monkeypatch.setattr(main_window, "_ask_autoselect_proceed",
                            lambda *a: seen.append(a) or False)
        main_window.on_autoselect_decision({**self.INFO, 'verdict': verdict})
        title, text, informative = seen[0]
        assert word in title.lower()
        assert text == self.INFO['message']
        assert informative == "Run at 20 mA?"

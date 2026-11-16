"""How long a run gives the instrument, and when it asks for the next reading.
"""
import copy
import csv
import time

import pytest

from resistamet_gui.constants import DEFAULT_SETTINGS, MAX_SWEEP_POINTS
from resistamet_gui.session import ListSink, MeasurementSession


@pytest.fixture(autouse=True)
def _no_sleep_inhibitor(monkeypatch):
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)


class TestSweepLimits:
    def test_more_points_than_the_instrument_has_is_refused_before_any_command(self, fake_rm):
        from resistamet_gui.instrument import Keithley2400
        keithley = Keithley2400('GPIB0::24::INSTR')
        keithley.connect()
        fake = fake_rm.opened[-1]
        before = len(fake.command_log)
        with pytest.raises(ValueError, match=str(MAX_SWEEP_POINTS)):
            keithley.setup_sweep('VOLT', 0.0, 10.0, 0.001, 0.1, 1.0, 0.0)
        assert len(fake.command_log) == before
        assert keithley.setup_sweep('VOLT', 0.0, 2.499, 0.001, 0.1, 1.0, 0.0) == MAX_SWEEP_POINTS


class TestReadsAreGivenTheTimeTheyTake:
    def _run(self, **measurement):
        from resistamet_gui.session.continuous_run import ContinuousRun
        settings = copy.deepcopy(DEFAULT_SETTINGS['measurement'])
        settings.update(measurement)
        return ContinuousRun._estimate_reading_time_s(settings, 60.0)

    def test_slow_settings_take_longer_than_the_old_five_seconds(self):
        slow = self._run(nplc=10.0, filter_enabled=True, filter_type='repeat',
                         filter_count=10, auto_zero='on')
        assert slow > 5.0
        assert self._run(nplc=0.01, filter_enabled=False, auto_zero='off') < 0.05

    def test_the_timeout_is_raised_to_three_readings(self):
        from resistamet_gui.session.continuous_run import ContinuousRun

        class Dev:
            timeout = 5000

        class Keithley:
            dev = Dev()

        run = ContinuousRun.__new__(ContinuousRun)
        run.keithley = Keithley()
        run._reading_time_s = 5.07
        run._allow_for_reading_time()
        assert Keithley.dev.timeout == int(5.07 * 3000) + 2000
        run._reading_time_s = 0.01
        run._allow_for_reading_time()
        assert Keithley.dev.timeout == int(5.07 * 3000) + 2000    # never lowered


class TestTheSamplingRateIsTheOneAskedFor:
    """The next reading was scheduled from when the last one came back, so the
    read time was added to every period: 10 Hz with a 50 ms read ran at 6 Hz
    under a header that said 10."""

    def test_period_does_not_include_the_read_time(self, fake_rm, tmp_path, monkeypatch):
        from resistamet_gui._simulator import FakeKeithley
        real = FakeKeithley._compute_read

        def slow(self):
            time.sleep(0.05)
            return real(self)
        monkeypatch.setattr(FakeKeithley, '_compute_read', slow)

        profile = copy.deepcopy({k: DEFAULT_SETTINGS[k] for k in ('measurement', 'display')})
        profile['file'] = {'auto_save_interval': 60, 'data_directory': str(tmp_path / 'data')}
        profile['measurement'].update({
            'gpib_address': 'GPIB0::24::INSTR', 'sampling_rate': 10.0, 'settling_time': 0.0,
            'isource_current': 1e-3, 'isource_voltage_compliance': 5.0,
            'isource_duration_hours': 0.0})
        sink = ListSink()
        session = MeasurementSession(sink)
        try:
            session.start(profile, 'source_i', 'wafer', 'alice')
            deadline = time.time() + 10.0
            while len(sink.of_type('sample')) < 9 and time.time() < deadline:
                time.sleep(0.01)
            session.stop()
            deadline = time.time() + 5.0
            while session.state != 'idle' and time.time() < deadline:
                time.sleep(0.02)
            path = sink.of_type('run_ended')[-1].payload['path']
        finally:
            session.close(timeout=5.0)
        with open(path) as handle:
            rows = [r for r in csv.reader(handle) if r and not r[0].startswith('#')]
        elapsed = [float(r[0]) for r in rows[1:9]]
        periods = [b - a for a, b in zip(elapsed, elapsed[1:])]
        # The median: one slow first period on a busy runner (0.32 s was seen
        # on Windows) put the mean over the line with every other period right.
        typical = sorted(periods)[len(periods) // 2]
        # 0.100 s asked for; 0.15 s and more with the read time added. The
        # loop polls every tenth of the interval, hence the allowance.
        assert typical < 0.135, periods

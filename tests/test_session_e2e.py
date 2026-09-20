"""End to end through the headless session, against the in-package simulator.

The GUI has this coverage in test_e2e_simulator.py; these assert the same
physics and the same files through MeasurementSession, so the API and MCP
layers inherit a path that is known to work rather than one that merely
compiles. No Qt is imported here.

Ported from test_e2e_simulator.py:
  resistance Ohm's law            <- test_resistance_records_ohms_law
  source-V current               <- test_voltage_source_records_correct_current
  source-I voltage               <- test_current_source_records_correct_voltage
  4PP V and I at the source      <- test_four_point_probe_records_v_i_at_source
  sweep CSV is linear            <- test_iv_sweep_writes_linear_csv
  marks land in the CSV          <- test_mark_event_lands_in_csv
  pause/resume keeps the data    <- test_pause_then_resume_preserves_data
  CSV header schema              <- test_csv_headers_match_documented_schema
"""
import copy
import csv
import glob
import os
import time

import pytest

from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.session.emitter import ListSink
from resistamet_gui.session.manager import MeasurementSession

DUT_OHMS = 100.0


@pytest.fixture
def simulated(monkeypatch, tmp_path):
    """The simulator, patched in process-wide, with data under tmp_path."""
    from resistamet_gui.simulator import enable_simulation

    enable_simulation(dut_resistance_ohms=DUT_OHMS, model="2420", sim_temp_c=25.0)
    monkeypatch.chdir(tmp_path)
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)
    return tmp_path


@pytest.fixture
def profile(simulated):
    settings = copy.deepcopy({
        'measurement': DEFAULT_SETTINGS['measurement'],
        'display': DEFAULT_SETTINGS['display'],
        'file': DEFAULT_SETTINGS['file'],
        'output': DEFAULT_SETTINGS['output'],
    })
    settings['file']['data_directory'] = str(simulated / 'measurement_data')
    settings['measurement'].update({
        'sampling_rate': 50.0, 'settling_time': 0.0, 'nplc': 0.1,
        'filter_enabled': False,
    })
    return settings


@pytest.fixture
def session():
    sink = ListSink()
    made = MeasurementSession(sink)
    made.sink = sink
    yield made
    made.close(timeout=5.0)


def _wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _run_until_samples(session, profile, mode, count=3, **overrides):
    session.start(profile, mode, 'E2E-DUT', 'e2e', overrides=overrides)
    assert _wait_for(lambda: len(session.sink.of_type('sample')) >= count), (
        f"{mode}: fewer than {count} samples")
    session.stop()
    assert _wait_for(lambda: session.state == 'idle')
    return session.sink


def _csv_rows(path):
    with open(path) as handle:
        rows = [r for r in csv.reader(handle) if r and not r[0].startswith('#')]
    return rows[0], rows[1:]


def _log_codes(sink):
    return [e.payload['code'] for e in sink.of_type('log')]


def _values(sink, key):
    return [e.payload['values'][key] for e in sink.of_type('sample')
            if key in e.payload['values']]


class TestPhysics:
    def test_resistance_records_ohms_law(self, session, profile):
        sink = _run_until_samples(session, profile, 'resistance',
                                   res_test_current=1e-3, res_voltage_compliance=5.0)
        for r in _values(sink, 'resistance'):
            assert r == pytest.approx(DUT_OHMS, rel=0.05)

    def test_voltage_source_records_correct_current(self, session, profile):
        sink = _run_until_samples(session, profile, 'source_v', vsource_voltage=1.0,
                                   vsource_current_compliance=0.1,
                                   vsource_duration_hours=0.0)
        for i in _values(sink, 'current'):
            assert i == pytest.approx(1.0 / DUT_OHMS, rel=0.05)

    def test_current_source_records_correct_voltage(self, session, profile):
        sink = _run_until_samples(session, profile, 'source_i', isource_current=1e-3,
                                   isource_voltage_compliance=5.0,
                                   isource_duration_hours=0.0)
        for v in _values(sink, 'voltage'):
            assert v == pytest.approx(1e-3 * DUT_OHMS, rel=0.05)

    def test_four_point_records_v_and_i(self, session, profile):
        sink = _run_until_samples(session, profile, 'four_point', fpp_current=1e-3,
                                   fpp_voltage_compliance=5.0, fpp_samples=0)
        for v, i in zip(_values(sink, 'voltage'), _values(sink, 'current')):
            assert i == pytest.approx(1e-3, rel=0.05)
            assert v == pytest.approx(1e-3 * DUT_OHMS, rel=0.05)


class TestFiles:
    def test_csv_matches_the_samples(self, session, profile):
        sink = _run_until_samples(session, profile, 'resistance',
                                   res_test_current=1e-3, res_voltage_compliance=5.0)
        path = sink.of_type('run_ended')[0].payload['path']
        header, rows = _csv_rows(path)

        assert header[:3] == ['elapsed_s', 'V_meas', 'I_meas']
        assert 'event' in header
        assert len(rows) == len(sink.of_type('sample'))

    def test_file_events_describe_the_file_written(self, session, profile):
        sink = _run_until_samples(session, profile, 'resistance',
                                   res_test_current=1e-3, res_voltage_compliance=5.0)
        opened = sink.of_type('file_opened')[0].payload
        finalized = sink.of_type('file_finalized')[0].payload

        assert opened['path'] == finalized['path']
        assert os.path.exists(opened['path'])
        assert finalized['end_metadata']['total_samples'] == len(sink.of_type('sample'))

    def test_sweep_writes_a_linear_csv(self, session, profile):
        session.start(profile, 'sweep', 'E2E-DUT', 'e2e', overrides={
            'sweep_source': 'voltage', 'sweep_start': 0.0, 'sweep_stop': 1.0,
            'sweep_step': 0.1, 'sweep_compliance': 0.1, 'sweep_delay': 0.0,
            'sweep_direction': 'up',
        })
        assert _wait_for(lambda: session.state == 'idle')

        sink = session.sink
        segments = sink.of_type('sweep_segment')
        assert len(segments) == 1
        voltages = segments[0].payload['voltages']
        currents = segments[0].payload['currents']
        assert len(voltages) == 11
        for v, i in zip(voltages, currents):
            assert i == pytest.approx(v / DUT_OHMS, abs=1e-6)

    def test_sweep_up_down_reports_both_directions(self, session, profile):
        session.start(profile, 'sweep', 'E2E-DUT', 'e2e', overrides={
            'sweep_source': 'voltage', 'sweep_start': 0.0, 'sweep_stop': 0.5,
            'sweep_step': 0.1, 'sweep_compliance': 0.1, 'sweep_delay': 0.0,
            'sweep_direction': 'up_down',
        })
        assert _wait_for(lambda: session.state == 'idle')

        directions = [e.payload['direction'] for e in session.sink.of_type('sweep_segment')]
        assert directions == ['forward', 'reverse']


    def test_sweep_log_counts_every_point_written(self, session, profile):
        session.start(profile, 'sweep', 'E2E-DUT', 'e2e', overrides={
            'sweep_source': 'voltage', 'sweep_start': 0.0, 'sweep_stop': 0.5,
            'sweep_step': 0.1, 'sweep_compliance': 0.1, 'sweep_delay': 0.0,
            'sweep_direction': 'up_down',
        })
        assert _wait_for(lambda: session.state == 'idle')

        sink = session.sink
        messages = {e.payload['code']: e.payload['message'] for e in sink.of_type('log')}
        assert messages['sweep_started'] == "Running I-V sweep (6 points each way)..."
        assert messages['sweep_finished'] == (
            "Sweep complete: 12 points acquired (6 forward, 6 reverse)")
        total = sink.of_type('file_finalized')[0].payload['end_metadata']['total_samples']
        assert total == 12

    def test_one_way_sweep_log_is_unchanged(self, session, profile):
        session.start(profile, 'sweep', 'E2E-DUT', 'e2e', overrides={
            'sweep_source': 'voltage', 'sweep_start': 0.0, 'sweep_stop': 0.5,
            'sweep_step': 0.1, 'sweep_compliance': 0.1, 'sweep_delay': 0.0,
            'sweep_direction': 'up',
        })
        assert _wait_for(lambda: session.state == 'idle')

        messages = {e.payload['code']: e.payload['message']
                    for e in session.sink.of_type('log')}
        assert messages['sweep_started'] == "Running I-V sweep (6 points)..."
        assert messages['sweep_finished'] == "Sweep complete: 6 points acquired"


class TestStopInsideTheSettle:
    """A stop during the settle unwinds by exception. It must still end the
    run the way every other stop does: footer in the file, output-off and
    data-saved lines in the log."""

    def _stop_in_the_settle(self, session, profile):
        profile['measurement']['settling_time'] = 10.0
        session.start(profile, 'resistance', 'E2E-DUT', 'e2e',
                      overrides={'res_test_current': 1e-3, 'res_voltage_compliance': 5.0})
        assert _wait_for(lambda: 'settling' in _log_codes(session.sink))
        session.stop()
        assert _wait_for(lambda: session.state == 'idle')
        return session.sink

    def test_the_file_gets_its_footer(self, session, profile):
        sink = self._stop_in_the_settle(session, profile)
        path = sink.of_type('file_opened')[0].payload['path']
        text = open(path, encoding='utf-8').read()
        assert "# --- run completed ---" in text
        assert "# total_samples: 0" in text
        assert "# ended_at:" in text
        assert "# duration_s:" in text

    def test_the_closing_events_match_any_other_stop(self, session, profile):
        sink = self._stop_in_the_settle(session, profile)
        path = sink.of_type('file_opened')[0].payload['path']

        codes = _log_codes(sink)
        assert codes.index('stopping') < codes.index('output_off') < codes.index('completed')
        completed = [e.payload['message'] for e in sink.of_type('log')
                     if e.payload['code'] == 'completed']
        assert completed == [f"Measurement (Resistance) completed! Data saved to: {path}"]

        finalized = sink.of_type('file_finalized')
        assert [e.payload['path'] for e in finalized] == [path]
        assert finalized[0].payload['end_metadata']['total_samples'] == 0
        assert len(sink.of_type('acquisition_finished')) == 1

        ended = sink.of_type('run_ended')[0].payload
        assert (ended['reason'], ended['ok'], ended['samples']) == ('user_stop', True, 0)
        assert sink.of_type('sample') == []
        assert sink.of_type('error') == []

    def test_a_stop_in_a_delta_settle_also_finalizes(self, session, profile):
        session.start(profile, 'four_point', 'E2E-DUT', 'e2e', overrides={
            'fpp_current': 1e-3, 'fpp_voltage_compliance': 5.0, 'fpp_samples': 0,
            'fpp_delta_mode': True, 'fpp_delta_settling': 5.0})
        assert _wait_for(lambda: 'starting' in _log_codes(session.sink))
        time.sleep(0.3)  # into the first polarity's settle
        session.stop()
        assert _wait_for(lambda: session.state == 'idle')

        sink = session.sink
        path = sink.of_type('file_opened')[0].payload['path']
        assert "# --- run completed ---" in open(path, encoding='utf-8').read()
        assert 'output_off' in _log_codes(sink)
        assert sink.of_type('error') == []


class TestOperatorActions:
    def test_mark_event_lands_in_the_csv(self, session, profile):
        session.start(profile, 'resistance', 'E2E-DUT', 'e2e',
                       overrides={'res_test_current': 1e-3, 'res_voltage_compliance': 5.0})
        assert _wait_for(lambda: session.sink.of_type('sample'))
        session.mark_event('PROBE_MOVED')
        assert _wait_for(lambda: any(e.payload['event_marker'] == 'PROBE_MOVED'
                                      for e in session.sink.of_type('sample')))
        session.stop()
        assert _wait_for(lambda: session.state == 'idle')

        path = session.sink.of_type('run_ended')[0].payload['path']
        header, rows = _csv_rows(path)
        events = [row[header.index('event')] for row in rows]
        assert 'PROBE_MOVED' in events

    def test_pause_then_resume_keeps_the_data(self, session, profile):
        session.start(profile, 'resistance', 'E2E-DUT', 'e2e',
                       overrides={'res_test_current': 1e-3, 'res_voltage_compliance': 5.0})
        assert _wait_for(lambda: len(session.sink.of_type('sample')) >= 2)
        session.pause()
        assert _wait_for(lambda: session.state == 'paused')
        # pause() returns at once; a reading already on the bus still lands.
        # The run says when it has actually parked.
        assert _wait_for(lambda: session.sink.of_type('paused'))
        during_pause = len(session.sink.of_type('sample'))
        time.sleep(0.3)
        assert len(session.sink.of_type('sample')) == during_pause, "sampled while paused"

        session.resume()
        assert _wait_for(lambda: len(session.sink.of_type('sample')) > during_pause)
        session.stop()
        assert _wait_for(lambda: session.state == 'idle')

        path = session.sink.of_type('run_ended')[0].payload['path']
        _, rows = _csv_rows(path)
        assert len(rows) == len(session.sink.of_type('sample'))


class TestEventStream:
    def test_a_run_is_reconstructable_from_its_events(self, session, profile):
        """run_started ... run_ended, in order, with the settings it used."""
        sink = _run_until_samples(session, profile, 'resistance',
                                   res_test_current=1e-3, res_voltage_compliance=5.0)
        types = sink.types()

        assert types[0] == 'run_started'
        assert types[-1] == 'run_ended'
        assert types.index('instrument_connected') < types.index('file_opened')
        assert types.index('file_opened') < types.index('sample')
        started = sink.of_type('run_started')[0].payload
        assert started['settings']['measurement']['res_test_current'] == 1e-3
        assert all(e.run_id == 'run-1' for e in sink.events)

    def test_sequence_numbers_are_contiguous(self, session, profile):
        sink = _run_until_samples(session, profile, 'resistance',
                                   res_test_current=1e-3, res_voltage_compliance=5.0)
        assert [e.seq for e in sink.events] == list(range(1, len(sink.events) + 1))


class TestResistanceCompliance:
    """The ohms function does not raise the compliance bit; the reading has to say it.

    Bench, 2026-09-18, Keithley 2400 and 2420: with the source pinned at its
    voltage limit the status word stays clear, and in manual range the CURR
    element is the programmed current, so V/I is a wrong resistance that
    looks like data. The simulator reproduces that, and the parser must catch
    it from the measured voltage sitting at the limit.
    """

    def test_a_pinned_voltage_is_flagged_not_recorded_as_good(self, session, profile):
        # 100 Ω at 10 mA wants 1 V; a 0.5 V limit pins it.
        sink = _run_until_samples(session, profile, 'resistance', res_auto_range=False,
                                   res_test_current=10e-3, res_voltage_compliance=0.5)
        samples = sink.of_type('sample')
        assert samples, 'no samples'
        assert {s.payload['compliance'] for s in samples} == {'V_COMP'}
        assert sink.of_type('compliance'), 'no compliance event'
        # And the value it would have archived is indeed wrong, which is why the flag matters.
        assert all(v['resistance'] < DUT_OHMS * 0.6 for v in (s.payload['values'] for s in samples))

    def test_stop_on_compliance_ends_the_run(self, session, profile):
        profile['measurement']['stop_on_compliance'] = True
        session.start(profile, 'resistance', 'pinned', 'alice',
                      overrides={'res_auto_range': False, 'res_test_current': 10e-3,
                                 'res_voltage_compliance': 0.5})
        assert _wait_for(lambda: session.state == 'idle')
        ended = session.sink.of_type('run_ended')[-1].payload
        assert ended['reason'] == 'compliance_stop'

    def test_within_compliance_is_still_ok(self, session, profile):
        sink = _run_until_samples(session, profile, 'resistance', res_auto_range=False,
                                   res_test_current=1e-3, res_voltage_compliance=5.0)
        assert {s.payload['compliance'] for s in sink.of_type('sample')} == {'OK'}

    def test_the_file_records_the_limit_the_instrument_has(self, session, profile):
        sink = _run_until_samples(session, profile, 'resistance', res_auto_range=False,
                                   res_test_current=1e-3, res_voltage_compliance=2.0)
        with open(sink.of_type('run_ended')[-1].payload['path']) as handle:
            comments = [line for line in handle if line.startswith('#')]
        assert any('effective' in line and 'voltage_compliance_V' in line and '2.0' in line
                   for line in comments), comments

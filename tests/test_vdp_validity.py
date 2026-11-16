"""A van der Pauw result says when it is not one.

Each of these was a run, or a set of readings, that came out as an ordinary
result: readings taken in compliance, sense leads reversed at two of the four
geometries, an overflow averaged in with good readings, a row the file did not
take. The numbers stay available for diagnosis; what changes is that the
result, the file and the run's end say what happened.
"""
import csv
import math
import threading
import time

import pytest

from resistamet_gui import calculations_vdp as vdp
from resistamet_gui.accuracy import voltage_uncertainty
from resistamet_gui.constants import (KEITHLEY_COMPLIANCE_MAGIC_NUMBER,
                                      KEITHLEY_STAT_BIT_COMPLIANCE)
from resistamet_gui.data_export import get_column_config, parse_metadata
from resistamet_gui.session.control import RunControl
from resistamet_gui.session.emitter import EventEmitter, ListSink
from resistamet_gui.session.vdp_run import VdpRun

CURRENT = 1e-3
LABELS = ("V_21,34", "V_12,34", "V_32,41", "V_23,41",
          "V_43,12", "V_34,12", "V_14,23", "V_41,23")


def _readings(r1, r2, r3, r4, current=CURRENT):
    """The eight voltages of geometries reading R_1..R_4, no offsets."""
    volts = {}
    for (pos, neg), r in zip(zip(LABELS[0::2], LABELS[1::2]), (r1, r2, r3, r4)):
        volts[pos], volts[neg] = current * r, -current * r
    return volts


# --------------------------------------------------------------------------
# The calculation
# --------------------------------------------------------------------------


class TestSignsOfTheReadings:
    def test_a_good_set_is_valid(self):
        out = vdp.calculate_van_der_pauw(_readings(100.0, 300.0, 100.0, 300.0), CURRENT, 0.05)
        assert out.valid and out.invalid_reason == ""
        assert out.q_a == pytest.approx(3.0) and out.homogeneous
        assert out.sheet_resistance > 0 and out.rho_avg > 0

    def test_sense_leads_swapped_at_geometries_2_and_4(self):
        """Used to give R_s at half its value, homogeneous, Q and f ordinary."""
        good = vdp.calculate_van_der_pauw(_readings(100.0, 300.0, 100.0, 300.0), CURRENT, 0.05)
        out = vdp.calculate_van_der_pauw(_readings(100.0, -300.0, 100.0, -300.0), CURRENT, 0.05)
        assert not out.valid
        assert "sign inconsistency" in out.invalid_reason
        for label in ("V_32,41", "V_23,41", "V_14,23", "V_41,23"):
            assert label in out.invalid_reason
        assert "V_21,34" not in out.invalid_reason
        assert not out.homogeneous
        assert math.isnan(out.sheet_resistance)
        assert all(math.isnan(x) for x in (out.rho_a, out.rho_b, out.rho_avg))
        # Q and f are still the readings', to diagnose with.
        assert out.q_a == pytest.approx(good.q_a) and out.f_a == pytest.approx(good.f_a)

    def test_one_geometry_reversed(self):
        out = vdp.calculate_van_der_pauw(_readings(100.0, 100.0, -100.0, 100.0), CURRENT, 0.05)
        assert not out.valid and not out.homogeneous
        assert "V_43,12 - V_34,12 is negative" in out.invalid_reason

    def test_one_group_reversed_is_not_just_inhomogeneous(self):
        out = vdp.calculate_van_der_pauw(_readings(-100.0, -100.0, 300.0, 300.0), CURRENT, 0.05)
        assert not out.valid and math.isnan(out.sheet_resistance)

    def test_everything_reversed_gives_no_resistivity(self):
        """Used to write rho_a, rho_b and rho_avg negative beside a NaN R_s."""
        out = vdp.calculate_van_der_pauw(_readings(-100.0, -100.0, -100.0, -100.0), CURRENT, 0.05)
        assert not out.valid and not out.homogeneous
        assert "all four" in out.invalid_reason
        assert math.isnan(out.sheet_resistance)
        assert all(math.isnan(x) for x in (out.rho_a, out.rho_b, out.rho_avg))


class TestStatisticalTermIsNotTheShapeOfTheSample:
    def _u(self, volts, **kwargs):
        result = vdp.calculate_van_der_pauw(volts, CURRENT, 0.05)
        return result, vdp.vdp_combined_uncertainty(
            volts, CURRENT, result.sheet_resistance, result.rho_avg, **kwargs)

    @pytest.mark.parametrize("q", [1.0, 3.0, 30.0])
    def test_a_noiseless_sample_of_any_q_has_none(self, q):
        """Q = 3 used to give 31 %: R_A and R_B taken as repeats of one R."""
        result, u = self._u(_readings(100.0, 100.0 * q, 100.0, 100.0 * q))
        assert u.u_stat_R == pytest.approx(0.0, abs=1e-12)
        assert u.u_total_R == pytest.approx(u.u_inst_R, rel=1e-12)
        # What is left is the voltmeter's accuracy, a fraction of a percent.
        assert u.u_rs / result.sheet_resistance < 0.01

    def test_it_is_the_disagreement_of_the_reciprocal_pairs(self):
        # Geometries 1 and 3 are one quantity, 2 and 4 the other.
        _, u = self._u(_readings(100.0, 300.0, 102.0, 297.0))
        assert u.u_stat_R == pytest.approx(0.25 * math.hypot(2.0, 3.0), rel=1e-12)
        assert u.u_total_R == pytest.approx(math.hypot(u.u_inst_R, u.u_stat_R), rel=1e-12)

    def test_the_instrument_term_follows_a_fixed_range(self):
        volts = _readings(100.0, 100.0, 100.0, 100.0)       # +/-0.1 V readings
        _, auto = self._u(volts)
        _, fixed = self._u(volts, v_fixed_range=20.0)
        expected = math.sqrt(2.0) * voltage_uncertainty(0.1, fixed_range=20.0) / (2 * CURRENT)
        assert fixed.u_inst_R == pytest.approx(expected, rel=1e-9)
        assert fixed.u_inst_R > auto.u_inst_R


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def _settings(tmp_path, **measurement):
    settings = {
        "measurement": {
            "nplc": 1.0, "gpib_address": "GPIB0::24::INSTR", "auto_zero": "on",
            "filter_enabled": False,
            "vdp_current": CURRENT, "vdp_voltage_compliance": 5.0,
            "vdp_voltage_range_auto": True, "vdp_settling_s": 0.0,
            "vdp_readings_per_polarity": 1, "vdp_thickness_cm": 0.05,
        },
        "display": {"enable_plot": False, "plot_update_interval": 100, "buffer_size": 100},
        "file": {"auto_save_interval": 60, "data_directory": str(tmp_path / "data")},
        "output": {"format": "csv", "compression": "never", "compression_threshold_mb": 5},
    }
    settings["measurement"].update(measurement)
    return settings


@pytest.fixture(autouse=True)
def _no_sleep_inhibitor(monkeypatch):
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)


def _script_reads(monkeypatch, reads):
    """Every ``:READ?`` answers from ``reads``: voltages, or (voltage, stat)."""
    from resistamet_gui._simulator import FakeKeithley
    query = FakeKeithley.query
    pending = list(reads)

    def scripted(self, cmd):
        if cmd.strip().upper() == ':READ?':
            item = pending.pop(0)
            voltage, stat = item if isinstance(item, tuple) else (item, 0)
            return f"{voltage:+.6E},{CURRENT:+.6E},{stat:+.6E}"
        return query(self, cmd)
    monkeypatch.setattr(FakeKeithley, 'query', scripted)


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _run(tmp_path, **measurement):
    """Drive a run, pressing Measure at every prompt, until it ends."""
    control, sink = RunControl(), ListSink()
    run = VdpRun('wafer1', 'alice', _settings(tmp_path, **measurement), control,
                 EventEmitter(sink))
    thread = threading.Thread(target=run.execute, daemon=True)
    thread.start()
    answered = set()
    while thread.is_alive():
        prompt = control.pending_prompt
        if prompt is not None and prompt.prompt_id not in answered:
            answered.add(prompt.prompt_id)
            run.proceed()
        time.sleep(0.005)
    thread.join(10.0)
    assert sink.types()[-1] == 'run_ended'
    return sink


def _rows(sink):
    path = sink.of_type('run_ended')[0].payload['path']
    with open(path, encoding='utf-8') as handle:
        lines = [line for line in handle if not line.startswith('#')]
    return list(csv.DictReader(lines))


def _footer(sink):
    return parse_metadata(sink.of_type('run_ended')[0].payload['path'])


#: A square sample reading 0.1 V at every geometry, +I then -I.
CLEAN = [0.1, -0.1] * 4


class TestComplianceIsRecorded:
    def test_a_clean_run_is_valid_and_says_so(self, fake_rm, tmp_path, monkeypatch):
        _script_reads(monkeypatch, CLEAN)
        sink = _run(tmp_path)
        assert [row['compliance'] for row in _rows(sink)] == ['OK'] * 4
        footer = _footer(sink)
        assert footer['vdp_result.valid'] is True
        assert footer['vdp_result.compliance'] is False
        assert not footer.get('vdp_result.invalid_reason')
        assert footer['vdp_result.homogeneous'] is True
        assert sink.of_type('vdp_result')[0].payload['homogeneous'] is True
        assert sink.of_type('compliance') == []

    def test_the_column_is_the_other_modes_column(self):
        columns, units = get_column_config('vdp', {})
        assert columns[-1] == 'compliance' and len(columns) == len(units)

    def test_a_reading_with_the_status_bit_set(self, fake_rm, tmp_path, monkeypatch):
        """Used to end as homogeneous: true, with one event and nothing in the file."""
        reads = list(CLEAN)
        reads[2] = (0.1, KEITHLEY_STAT_BIT_COMPLIANCE)        # geometry 2, +I
        _script_reads(monkeypatch, reads)
        sink = _run(tmp_path)

        assert [row['compliance'] for row in _rows(sink)] == ['OK', 'V_COMP', 'OK', 'OK']
        footer = _footer(sink)
        assert footer['end_reason'] == 'completed'
        assert footer['vdp_result.valid'] is False
        assert footer['vdp_result.homogeneous'] is False
        assert footer['vdp_result.compliance'] is True
        assert footer['vdp_result.compliance_readings'] == 'V_32,41'
        assert 'compliance at V_32,41' in footer['vdp_result.invalid_reason']
        # The numbers are still there to look at.
        assert footer['vdp_result.sheet_resistance'] > 0
        assert footer['vdp_result.asymmetry_pct'] == pytest.approx(0.0, abs=1e-9)

        assert sink.of_type('vdp_result')[0].payload['homogeneous'] is False
        assert len(sink.of_type('compliance')) == 1
        logs = {e.payload['code']: e.payload for e in sink.of_type('log')}
        assert logs['vdp_result_invalid']['level'] == 'warning'
        assert 'INVALID' in logs['completed']['message']
        assert 'homogeneous' not in logs['completed']['message']

    def test_a_reading_at_the_limit_without_the_bit(self, fake_rm, tmp_path, monkeypatch):
        reads = list(CLEAN)
        reads[7] = -4.96                                       # geometry 4, -I; limit 5 V
        _script_reads(monkeypatch, reads)
        sink = _run(tmp_path)
        assert [row['compliance'] for row in _rows(sink)] == ['OK', 'OK', 'OK', 'V_COMP']
        footer = _footer(sink)
        assert footer['vdp_result.valid'] is False
        assert footer['vdp_result.compliance_readings'] == 'V_41,23'

    def test_one_read_of_several_in_compliance(self, fake_rm, tmp_path, monkeypatch):
        reads = [0.1, 0.1, 0.1, -0.1, -0.1, -0.1] * 4
        reads[1] = (0.1, KEITHLEY_STAT_BIT_COMPLIANCE)
        _script_reads(monkeypatch, reads)
        sink = _run(tmp_path, vdp_readings_per_polarity=3)
        assert _rows(sink)[0]['compliance'] == 'V_COMP'
        assert _footer(sink)['vdp_result.compliance_readings'] == 'V_21,34'


class TestSignInconsistencyReachesTheFile:
    def test_swapped_sense_leads_end_as_an_invalid_result(self, fake_rm, tmp_path, monkeypatch):
        _script_reads(monkeypatch, [0.1, -0.1, -0.3, 0.3, 0.1, -0.1, -0.3, 0.3])
        sink = _run(tmp_path)
        footer = _footer(sink)
        assert footer['vdp_result.valid'] is False
        assert 'sign inconsistency' in footer['vdp_result.invalid_reason']
        assert footer['vdp_result.compliance'] is False
        assert math.isnan(footer['vdp_result.sheet_resistance'])
        assert math.isnan(footer['vdp_result.rho_avg'])
        assert sink.of_type('vdp_result')[0].payload['homogeneous'] is False


class TestEveryRawReadingIsChecked:
    def test_one_overflow_among_several_is_not_averaged_away(self, fake_rm, tmp_path,
                                                             monkeypatch):
        """Three reads per polarity, one of them 9.9e37: the mean used to be
        3.3e37, below the overflow value, and went into the calculation."""
        reads = [0.1, KEITHLEY_COMPLIANCE_MAGIC_NUMBER, 0.1, -0.1, -0.1, -0.1] * 4
        _script_reads(monkeypatch, reads)
        sink = _run(tmp_path, vdp_readings_per_polarity=3)

        ended = sink.of_type('run_ended')[0].payload
        assert (ended['reason'], ended['ok']) == ('worker_error', False)
        errors = [e.payload['message'] for e in sink.of_type('error')]
        assert any('V_21,34' in m and '1 of 3' in m for m in errors), errors
        assert sink.of_type('vdp_result') == []
        # The row keeps the instrument's own value, not a mean.
        rows = _rows(sink)
        assert len(rows) == 1
        assert float(rows[0]['V_pos']) == pytest.approx(KEITHLEY_COMPLIANCE_MAGIC_NUMBER)
        assert float(rows[0]['V_neg']) == pytest.approx(-0.1)
        assert rows[0]['compliance'] == 'V_COMP'
        footer = _footer(sink)
        assert footer['end_reason'] == 'worker_error'
        assert not any(key.startswith('vdp_result') for key in footer)

    def test_good_reads_are_still_averaged(self, fake_rm, tmp_path, monkeypatch):
        _script_reads(monkeypatch, [0.09, 0.10, 0.11, -0.09, -0.10, -0.11] * 4)
        sink = _run(tmp_path, vdp_readings_per_polarity=3)
        first = sink.of_type('vdp_geometry_complete')[0].payload
        assert first['v_pos'] == pytest.approx(0.10) and first['v_neg'] == pytest.approx(-0.10)
        assert _footer(sink)['vdp_result.valid'] is True


class TestARowTheFileDoesNotTake:
    def test_the_run_ends_as_a_write_error(self, fake_rm, tmp_path, monkeypatch):
        """Used to be a line in the log file and a run that 'completed'."""
        from resistamet_gui.data_export import CsvExporter
        write_row = CsvExporter.write_row
        calls = {'n': 0}

        def fail_the_second(self, row):
            calls['n'] += 1
            if calls['n'] == 2:
                raise OSError(28, "No space left on device")
            return write_row(self, row)
        monkeypatch.setattr(CsvExporter, 'write_row', fail_the_second)
        _script_reads(monkeypatch, CLEAN)
        sink = _run(tmp_path)

        ended = sink.of_type('run_ended')[0].payload
        assert (ended['reason'], ended['ok']) == ('write_error', False)
        errors = [e.payload for e in sink.of_type('error')]
        assert [(e['code'], e['source']) for e in errors] == [('write_failed', 'file')]
        assert 'Geometry 2 of 4' in errors[0]['message']
        # It stopped there: no third geometry, no result.
        assert len(sink.of_type('vdp_geometry_complete')) == 1
        assert sink.of_type('vdp_result') == []
        footer = _footer(sink)
        assert footer['end_reason'] == 'write_error'
        assert not any(key.startswith('vdp_result') for key in footer)
        messages = [e.payload['message'] for e in sink.of_type('log')]
        assert not any('by user' in message for message in messages), messages
        writes = [cmd.upper() for op, cmd in fake_rm.opened[-1].command_log if op == 'write']
        assert writes[-1] == ':OUTP OFF'


class TestTheFilterInTheHeaderIsTheFilterSent:
    def _sent(self, fake_rm):
        return [cmd.upper() for op, cmd in fake_rm.opened[-1].command_log if op == 'write']

    def test_a_repeat_filter(self, fake_rm, tmp_path, monkeypatch):
        _script_reads(monkeypatch, CLEAN)
        sink = _run(tmp_path, filter_enabled=True, filter_type='repeat', filter_count=7)
        sent = self._sent(fake_rm)
        assert ':SENS:AVER:TCON REP' in sent and ':SENS:AVER:COUN 7' in sent
        header = _footer(sink)
        assert (header['filter.enabled'], header['filter.type'], header['filter.count']) == \
            (True, 'repeat', 7)
        assert not [e for e in sink.of_type('log')
                    if e.payload['code'] == 'filter_type_overridden']

    def test_a_moving_filter_is_sent_as_repeat_and_said(self, fake_rm, tmp_path, monkeypatch):
        _script_reads(monkeypatch, CLEAN)
        sink = _run(tmp_path, filter_enabled=True, filter_type='moving', filter_count=5)
        sent = self._sent(fake_rm)
        assert ':SENS:AVER:TCON REP' in sent and ':SENS:AVER:TCON MOV' not in sent
        assert _footer(sink)['filter.type'] == 'repeat'
        warned = [e.payload for e in sink.of_type('log')
                  if e.payload['code'] == 'filter_type_overridden']
        assert len(warned) == 1 and warned[0]['level'] == 'warning'

    def test_no_filter(self, fake_rm, tmp_path, monkeypatch):
        _script_reads(monkeypatch, CLEAN)
        sink = _run(tmp_path)
        assert not any(cmd.startswith(':SENS:AVER') for cmd in self._sent(fake_rm))
        assert _footer(sink)['filter.enabled'] is False


class TestUncertaintyOnAFixedRange:
    def test_the_run_passes_the_range_it_sent(self, fake_rm, tmp_path, monkeypatch):
        """Auto range off: the run sends the compliance value as the range."""
        _script_reads(monkeypatch, CLEAN)
        auto = _footer(_run(tmp_path / 'auto'))
        _script_reads(monkeypatch, CLEAN)
        fixed = _footer(_run(tmp_path / 'fixed', vdp_voltage_range_auto=False,
                             vdp_voltage_compliance=20.0))
        assert fixed['vdp_result.sheet_resistance'] == pytest.approx(
            auto['vdp_result.sheet_resistance'])
        assert fixed['vdp_result.sheet_resistance_uncertainty'] > \
            auto['vdp_result.sheet_resistance_uncertainty']


# --------------------------------------------------------------------------
# The thickness box
# --------------------------------------------------------------------------


class TestThicknessBox:
    @pytest.mark.parametrize("nanometres", [1, 15, 123])
    def test_it_holds_a_nanometre(self, main_window, nanometres):
        """At 6 decimals of a cm, 15 nm became 20 nm: +33 % on rho."""
        box = main_window.tab_vdp.vdp_thickness_cm
        box.setValue(nanometres * 1e-7)
        assert box.value() == pytest.approx(nanometres * 1e-7, rel=1e-9)

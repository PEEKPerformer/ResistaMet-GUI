"""What a data file says about the run that wrote it.

Two files with identical headers once held different numbers, and nothing in
either said why: the header left out the averaging filter and the inputs that
choose the four-point formula, and every footer read "run completed".
"""
import copy
import csv
import time

import pytest

from resistamet_gui.calculations import four_point_method
from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.data_export import (
    build_metadata, effective_filter_type, parse_metadata,
)
from resistamet_gui.session import ListSink, MeasurementSession
from resistamet_gui.session.samples import build_row


@pytest.fixture(autouse=True)
def _no_sleep_inhibitor(monkeypatch):
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)


def _fpp(**overrides):
    settings = {
        'fpp_current': 1e-3, 'fpp_voltage_compliance': 5.0, 'fpp_voltage_range_auto': True,
        'fpp_spacing_cm': 0.1016, 'fpp_thickness_um': 0.0, 'fpp_k_factor': 4.532,
        'fpp_alpha': 1.0, 'fpp_model': 'thin_film', 'fpp_diameter_cm': 0.0,
        'fpp_geometry': 'circle', 'fpp_temperature_c': float('nan'),
        'fpp_dopant_type': 'none',
    }
    settings.update(overrides)
    return settings


def _row(settings, volts, amps, compliance='OK'):
    row, derived = build_row('four_point', 0.0, {'voltage': volts, 'current': amps},
                             compliance, '', settings, 1.0, False, '2420', None)
    return row, derived


class TestTheHeaderSaysHowTheRowsWereMade:
    def _meta(self, mode, **measurement):
        base = copy.deepcopy(DEFAULT_SETTINGS['measurement'])
        base.update(measurement)
        return build_metadata('alice', 'wafer', mode, {'measurement': base})

    def test_the_filter_is_recorded_in_every_mode(self):
        for mode in ('resistance', 'source_v', 'source_i', 'four_point', 'sweep', 'vdp'):
            meta = self._meta(mode, filter_enabled=True, filter_type='repeat', filter_count=10)
            assert meta['filter'] == {'enabled': True, 'type': 'repeat', 'count': 10}, mode

    def test_a_filter_that_is_off_has_no_type_or_count(self):
        assert self._meta('resistance', filter_enabled=False)['filter'] == {'enabled': False}

    def test_two_runs_that_differ_only_in_the_filter_have_different_headers(self):
        assert self._meta('four_point', filter_enabled=True) != \
            self._meta('four_point', filter_enabled=False)

    def test_four_point_method_and_its_inputs(self):
        legacy = self._meta('four_point')['params']
        assert legacy['method'] == 'legacy'
        f84 = self._meta('four_point', fpp_diameter_cm=5.08, fpp_geometry='square',
                         fpp_temperature_c=25.0, fpp_dopant_type='n')['params']
        assert (f84['method'], f84['diameter_cm'], f84['geometry'],
                f84['temperature_c'], f84['dopant_type']) == ('f84', 5.08, 'square', 25.0, 'n')

    def test_the_method_is_the_one_the_rows_use(self):
        for settings in (_fpp(), _fpp(fpp_diameter_cm=1.0), _fpp(fpp_geometry='square'),
                         _fpp(fpp_temperature_c=25.0, fpp_dopant_type='p'),
                         _fpp(fpp_temperature_c=25.0)):
            _, derived = _row(settings, 0.1, 1e-3)
            assert derived['method'] == four_point_method(settings)

    def test_delta_mode_and_its_settling(self):
        params = self._meta('four_point', fpp_delta_mode=True, fpp_delta_settling=0.3)['params']
        assert params['delta_mode'] is True and params['delta_settling_s'] == 0.3
        assert 'delta_settling_s' not in self._meta('four_point')['params']

    def test_the_cable_null_that_was_subtracted(self):
        assert self._meta('resistance', res_cable_null=0.35)['params']['cable_null_ohm'] == 0.35
        assert self._meta('resistance')['params']['cable_null_ohm'] == 0.0

    def test_no_sampling_rate_for_a_run_that_has_none(self):
        for mode in ('sweep', 'vdp'):
            meta = self._meta(mode)
            assert 'sampling_rate_hz' not in meta and 'settling_time_s' not in meta
        assert 'sampling_rate_hz' in self._meta('resistance')


class TestAReversingRunNeverUsesAMovingFilter:
    """A moving stack averages the last N conversions, so it would average
    the two polarities of a delta or van der Pauw reading together."""

    @pytest.mark.parametrize("mode, settings, expected", [
        ('four_point', {'filter_type': 'moving', 'fpp_delta_mode': True}, 'repeat'),
        ('vdp', {'filter_type': 'moving'}, 'repeat'),
        ('four_point', {'filter_type': 'moving', 'fpp_delta_mode': False}, 'moving'),
        ('resistance', {'filter_type': 'moving'}, 'moving'),
        ('resistance', {'filter_type': 'repeat'}, 'repeat'),
    ])
    def test_type(self, mode, settings, expected):
        assert effective_filter_type(mode, settings) == expected


class TestCsvKeepsTheInstrumentsDigits:
    def test_seven_digit_readings_and_long_elapsed_times(self, tmp_path):
        from resistamet_gui.data_export import make_exporter
        exporter = make_exporter(
            base_path=tmp_path / 'run', metadata={'mode': 'resistance'},
            columns=['elapsed_s', 'R_ohm'], units=['s', 'Ω'], output_settings=None)
        exporter.write_row([123456.789, 100.00042])
        exporter.write_row([123456.889, 5.810327e1])
        exporter.finalize({'ended_at': 'now'})
        with open(exporter.output_paths[0]) as handle:
            rows = [r for r in csv.reader(handle) if r and not r[0].startswith('#')]
        assert rows[1:] == [['123456.789', '100.00042'], ['123456.889', '58.10327']]


class TestTheFooterSaysWhyTheRunEnded:
    def test_end_reason_in_every_mode(self, fake_rm, tmp_path):
        profile = copy.deepcopy({k: DEFAULT_SETTINGS[k] for k in ('measurement', 'display')})
        profile['file'] = {'auto_save_interval': 60, 'data_directory': str(tmp_path / 'data')}
        profile['measurement'].update({
            'gpib_address': 'GPIB0::24::INSTR', 'sampling_rate': 100.0, 'settling_time': 0.0,
            'res_test_current': 1e-3, 'res_voltage_compliance': 5.0})
        sink = ListSink()
        session = MeasurementSession(sink)
        try:
            session.start(profile, 'resistance', 'wafer', 'alice')
            deadline = time.time() + 5.0
            while len(sink.of_type('sample')) < 2 and time.time() < deadline:
                time.sleep(0.02)
            session.stop()
            deadline = time.time() + 5.0
            while session.state != 'idle' and time.time() < deadline:
                time.sleep(0.02)
            ended = sink.of_type('run_ended')[-1].payload
        finally:
            session.close(timeout=5.0)
        footer = parse_metadata(ended['path'])
        assert footer['end_reason'] == ended['reason']
        assert footer['end_reason'] != 'completed'

"""Resolver behaviour beyond parity: strict checks, issues, derived values."""
import copy

import pytest

from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.schema.resolve import allowed_override_keys, resolve_run_settings


@pytest.fixture
def profile():
    return copy.deepcopy({
        'measurement': DEFAULT_SETTINGS['measurement'],
        'display': DEFAULT_SETTINGS['display'],
        'file': DEFAULT_SETTINGS['file'],
        'output': DEFAULT_SETTINGS['output'],
    })


def _keys(resolved):
    return sorted(issue.key for issue in resolved.issues)


class TestProfileOwnedKeys:
    def test_settling_time_and_address_come_from_the_profile(self, profile):
        profile['measurement']['settling_time'] = 0.7
        profile['measurement']['gpib_address'] = 'GPIB0::9::INSTR'
        resolved = resolve_run_settings(profile, 'resistance', {
            'settling_time': 99.0, 'gpib_address': 'GPIB0::24::INSTR',
        })
        assert resolved.settings['measurement']['settling_time'] == 0.7
        assert resolved.settings['measurement']['gpib_address'] == 'GPIB0::9::INSTR'

    def test_strict_rejects_them(self, profile):
        resolved = resolve_run_settings(profile, 'resistance',
                                         {'settling_time': 0.5}, strict=True)
        assert _keys(resolved) == ['settling_time']
        assert not resolved.ok

    def test_they_are_not_offered(self):
        assert 'settling_time' not in allowed_override_keys('resistance')
        assert 'gpib_address' not in allowed_override_keys('resistance')


class TestStrictKeyChecking:
    def test_unknown_key_rejected(self, profile):
        resolved = resolve_run_settings(profile, 'resistance',
                                         {'smaple_rate': 5}, strict=True)
        assert _keys(resolved) == ['smaple_rate']

    def test_other_modes_keys_rejected(self, profile):
        resolved = resolve_run_settings(profile, 'resistance',
                                         {'vdp_current': 1e-3}, strict=True)
        assert _keys(resolved) == ['vdp_current']

    def test_shared_groups_accepted(self, profile):
        resolved = resolve_run_settings(profile, 'resistance', {
            'nplc': 2.0, 'filter_count': 20, 'aux_log_enabled': True,
            'safety_voltage_warn_v': 50.0,
        }, strict=True)
        assert resolved.issues == []
        assert resolved.settings['measurement']['nplc'] == 2.0

    def test_lenient_mode_allows_anything(self, profile):
        resolved = resolve_run_settings(profile, 'resistance', {'smaple_rate': 5})
        assert resolved.issues == []


class TestStartTimeChecks:
    def test_vdp_requires_a_thickness(self, profile):
        resolved = resolve_run_settings(profile, 'vdp',
                                         {'vdp_thickness_cm': 0.0}, strict=True)
        assert _keys(resolved) == ['vdp_thickness_cm']

    def test_vdp_thickness_accepted_when_set(self, profile):
        resolved = resolve_run_settings(profile, 'vdp',
                                         {'vdp_thickness_cm': 0.05}, strict=True)
        assert resolved.issues == []

    def test_four_point_power_envelope(self, profile):
        resolved = resolve_run_settings(profile, 'four_point', {
            'fpp_current': 1e-3, 'fpp_voltage_compliance': 100.0, 'fpp_power_stop_w': 0.05,
        }, strict=True)
        assert _keys(resolved) == ['fpp_power_stop_w']

    def test_aux_logging_unavailable_for_sweep(self, profile):
        resolved = resolve_run_settings(profile, 'sweep',
                                         {'aux_log_enabled': True}, strict=True)
        assert _keys(resolved) == ['aux_log_enabled']

    def test_start_time_checks_are_strict_only(self, profile):
        resolved = resolve_run_settings(profile, 'vdp', {'vdp_thickness_cm': 0.0})
        assert resolved.issues == []


class TestRangeIssues:
    def test_out_of_range_profile_still_opens(self, profile):
        profile['measurement']['nplc'] = 99.0
        resolved = resolve_run_settings(profile, 'resistance', {})
        assert _keys(resolved) == ['nplc']
        assert resolved.settings['measurement']['nplc'] == 99.0, "value passes through"


class TestDerived:
    def test_sweep_points(self, profile):
        resolved = resolve_run_settings(profile, 'sweep', {
            'sweep_start': 0.0, 'sweep_stop': 1.0, 'sweep_step': 0.1, 'sweep_direction': 'up',
        })
        assert resolved.derived['sweep_points'] == 11

    def test_sweep_points_double_for_up_down(self, profile):
        resolved = resolve_run_settings(profile, 'sweep', {
            'sweep_start': 0.0, 'sweep_stop': 1.0, 'sweep_step': 0.1,
            'sweep_direction': 'up_down',
        })
        assert resolved.derived['sweep_points'] == 22

    def test_worst_case_power(self, profile):
        resolved = resolve_run_settings(profile, 'four_point', {
            'fpp_current': 1e-3, 'fpp_voltage_compliance': 5.0,
        })
        assert resolved.derived['worst_case_power_w'] == pytest.approx(5e-3)

    def test_max_rate_is_reported_not_enforced(self, profile):
        resolved = resolve_run_settings(profile, 'resistance', {'sampling_rate': 100.0})
        assert resolved.derived['max_rate_hz'] > 0
        assert resolved.settings['measurement']['sampling_rate'] == 100.0


class TestHazard:
    def test_hazard_uses_resolved_values(self, profile):
        profile['measurement']['vsource_voltage'] = 1.0
        resolved = resolve_run_settings(profile, 'source_v', {'vsource_voltage': 60.0})
        assert resolved.hazard.hazardous
        assert resolved.hazard.voltage_v == 60.0

    def test_safe_configuration(self, profile):
        resolved = resolve_run_settings(profile, 'source_v', {'vsource_voltage': 1.0})
        assert not resolved.hazard.hazardous


class TestRunUntilStopped:
    def test_continuous_flag_zeroes_the_duration(self, profile):
        resolved = resolve_run_settings(profile, 'source_v', {
            'vsource_duration_hours': 3.0, 'vsource_run_continuous': True,
        })
        assert resolved.settings['measurement']['vsource_duration_hours'] == 0.0

    def test_flag_is_not_stored_as_a_setting(self, profile):
        resolved = resolve_run_settings(profile, 'source_v', {'vsource_run_continuous': True})
        assert 'vsource_run_continuous' not in resolved.settings['measurement']


class TestInvalidMode:
    def test_unknown_mode_raises(self, profile):
        with pytest.raises(ValueError):
            resolve_run_settings(profile, 'hall', {})


class TestSampleOutline:
    """The outline must be describable; the position-correction mode is fixed."""

    def test_defaults_raise_nothing(self, profile):
        assert resolve_run_settings(profile, 'four_point', {}, strict=True).issues == []

    def test_a_legacy_outline_raises_nothing(self, profile):
        resolved = resolve_run_settings(profile, 'four_point', {
            'fpp_geometry': 'rectangle_2', 'fpp_diameter_cm': 1.0}, strict=True)
        assert resolved.issues == []

    def test_a_shape_without_its_dimensions_is_an_error(self, profile):
        resolved = resolve_run_settings(profile, 'four_point',
                                         {'fpp_sample_shape': 'circle'}, strict=True)
        assert _keys(resolved) == ['fpp_sample_shape']
        assert 'needs diameter_mm' in resolved.issues[0].message
        assert not resolved.ok

    def test_an_outline_the_look_up_does_not_share_is_a_warning(self, profile):
        """The rows still use the legacy keys; the client is told, not stopped."""
        resolved = resolve_run_settings(profile, 'four_point', {
            'fpp_sample_shape': 'circle', 'fpp_sample_diameter_mm': 50.8}, strict=True)
        assert [(i.key, i.severity) for i in resolved.issues] == [('fpp_sample_shape', 'warning')]
        assert 'unbounded' in resolved.issues[0].message
        assert resolved.ok

    def test_matching_legacy_and_new_outline_raises_nothing(self, profile):
        resolved = resolve_run_settings(profile, 'four_point', {
            'fpp_sample_shape': 'circle', 'fpp_sample_diameter_mm': 50.8,
            'fpp_geometry': 'circle', 'fpp_diameter_cm': 5.08}, strict=True)
        assert resolved.issues == []

    def test_position_correction_can_only_warn_for_now(self, profile):
        resolved = resolve_run_settings(profile, 'four_point',
                                         {'fpp_position_correction': 'apply'}, strict=True)
        assert _keys(resolved) == ['fpp_position_correction']
        assert not resolved.ok

    def test_other_modes_are_not_checked(self, profile):
        profile['measurement']['fpp_sample_shape'] = 'circle'
        assert resolve_run_settings(profile, 'resistance', {}, strict=True).issues == []

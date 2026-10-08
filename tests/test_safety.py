"""Tests for the human-touch-safety voltage check."""

import math

import pytest

from resistamet_gui.safety import (
    DEFAULT_THRESHOLD_V,
    HazardCheck,
    is_potentially_hazardous,
    warning_message,
    warning_silenced,
)


def _settings(measurement: dict) -> dict:
    """Build a minimal settings dict shaped like user_settings."""
    return {'measurement': measurement}


class TestDefaultThreshold:
    def test_default_is_30v(self):
        # IEC 61010-1 SELV upper bound. If this changes, downstream UX
        # copy in the dialog needs review.
        assert DEFAULT_THRESHOLD_V == 30.0


class TestResistanceMode:
    def test_below_threshold_not_hazardous(self):
        s = _settings({'res_voltage_compliance': 5.0})
        check = is_potentially_hazardous(s, 'resistance')
        assert not check.hazardous
        assert check.voltage_v == 5.0

    def test_at_threshold_is_hazardous(self):
        # >= threshold — paranoid bound; 30 V exactly should warn.
        s = _settings({'res_voltage_compliance': 30.0})
        check = is_potentially_hazardous(s, 'resistance')
        assert check.hazardous

    def test_above_threshold_is_hazardous(self):
        s = _settings({'res_voltage_compliance': 100.0})
        check = is_potentially_hazardous(s, 'resistance')
        assert check.hazardous
        assert check.reason == 'V compliance'

    def test_negative_compliance_uses_abs(self):
        # The Keithley accepts negative compliance (rare but legal); we
        # care about the magnitude on the leads.
        s = _settings({'res_voltage_compliance': -50.0})
        check = is_potentially_hazardous(s, 'resistance')
        assert check.hazardous
        assert check.voltage_v == 50.0


class TestSourceVMode:
    def test_high_sourced_voltage_warns(self):
        # Source 200 V — directly above threshold even before compliance.
        s = _settings({'vsource_voltage': 200.0})
        check = is_potentially_hazardous(s, 'source_v')
        assert check.hazardous
        assert check.reason == 'Source V'


class TestSourceIMode:
    def test_open_circuit_swings_to_compliance(self):
        # Even at I=1 mA, a 200 V compliance can shock if the DUT opens.
        # This is the case the design memo specifically calls out.
        s = _settings({'isource_voltage_compliance': 200.0, 'isource_current': 1e-3})
        check = is_potentially_hazardous(s, 'source_i')
        assert check.hazardous
        assert check.voltage_v == 200.0


class TestFourPointAndVdp:
    def test_4pp_uses_fpp_voltage_compliance(self):
        s = _settings({'fpp_voltage_compliance': 50.0})
        check = is_potentially_hazardous(s, 'four_point')
        assert check.hazardous
        assert check.voltage_v == 50.0

    def test_vdp_uses_vdp_voltage_compliance(self):
        # An unknown mode is also "not hazardous", so only a hazardous
        # value and the voltage read back show the vdp key was used.
        s = _settings({'vdp_voltage_compliance': 50.0})
        check = is_potentially_hazardous(s, 'vdp')
        assert check.hazardous
        assert check.voltage_v == 50.0
        assert check.reason == 'V compliance'

        check = is_potentially_hazardous(_settings({'vdp_voltage_compliance': 5.0}), 'vdp')
        assert not check.hazardous
        assert check.voltage_v == 5.0


class TestSweep:
    def test_voltage_sweep_uses_max_endpoint(self):
        # V sweep from 0 to 100 V → max |endpoint| matters.
        s = _settings({
            'sweep_source': 'voltage',
            'sweep_start': 0.0,
            'sweep_stop': 100.0,
            'sweep_compliance': 0.1,
        })
        check = is_potentially_hazardous(s, 'sweep')
        assert check.hazardous
        assert check.voltage_v == 100.0
        assert check.reason == 'Sweep V range'

    def test_negative_sweep_endpoint_uses_abs(self):
        s = _settings({
            'sweep_source': 'voltage',
            'sweep_start': -50.0,
            'sweep_stop': 0.0,
            'sweep_compliance': 0.1,
        })
        check = is_potentially_hazardous(s, 'sweep')
        assert check.hazardous
        assert check.voltage_v == 50.0

    def test_current_sweep_uses_compliance(self):
        # I sweep — the lead voltage rises to compliance.
        s = _settings({
            'sweep_source': 'current',
            'sweep_start': 0.0,
            'sweep_stop': 1e-3,
            'sweep_compliance': 100.0,
        })
        check = is_potentially_hazardous(s, 'sweep')
        assert check.hazardous
        assert check.voltage_v == 100.0


    def test_current_sweep_at_60_v_is_the_same_hazard_as_a_60_v_source(self):
        """The compliance of a current-sourced sweep is the voltage an open
        circuit puts on the leads, exactly like a source at that voltage."""
        sweep = is_potentially_hazardous(_settings({
            'sweep_source': 'current', 'sweep_start': 0.0, 'sweep_stop': 1e-3,
            'sweep_compliance': 60.0,
        }), 'sweep')
        source = is_potentially_hazardous(_settings({'vsource_voltage': 60.0}), 'source_v')
        assert (sweep.hazardous, sweep.voltage_v, sweep.threshold_v) == (
            source.hazardous, source.voltage_v, source.threshold_v) == (True, 60.0, 30.0)
        assert sweep.reason == 'V compliance'

    def test_voltage_sweep_ignores_its_current_compliance(self):
        # 3 A of compliance is a current; it must not be read as 3 V, nor
        # hide the 60 V endpoint.
        check = is_potentially_hazardous(_settings({
            'sweep_source': 'voltage', 'sweep_start': 0.0, 'sweep_stop': 60.0,
            'sweep_compliance': 3.0,
        }), 'sweep')
        assert (check.hazardous, check.voltage_v, check.reason) == (True, 60.0, 'Sweep V range')


class TestThresholdOverride:
    def test_per_profile_threshold(self):
        # User dialled threshold up to 60 V → 50 V compliance no longer warns.
        s = _settings({
            'res_voltage_compliance': 50.0,
            'safety_voltage_warn_v': 60.0,
        })
        check = is_potentially_hazardous(s, 'resistance')
        assert not check.hazardous
        assert check.threshold_v == 60.0

    def test_zero_threshold_disables_check(self):
        # The memo: "0 disables." Useful for users who pre-acknowledge
        # via Settings rather than the modal.
        s = _settings({
            'res_voltage_compliance': 1000.0,
            'safety_voltage_warn_v': 0.0,
        })
        check = is_potentially_hazardous(s, 'resistance')
        assert not check.hazardous

    def test_explicit_threshold_argument_wins_over_settings(self):
        s = _settings({
            'res_voltage_compliance': 50.0,
            'safety_voltage_warn_v': 0.0,   # would disable
        })
        check = is_potentially_hazardous(s, 'resistance', threshold_v=30.0)
        assert check.hazardous


class TestEdgeCases:
    def test_empty_settings_returns_not_hazardous(self):
        check = is_potentially_hazardous({}, 'resistance')
        assert not check.hazardous

    def test_unknown_mode_returns_not_hazardous(self):
        s = _settings({'res_voltage_compliance': 200.0})
        check = is_potentially_hazardous(s, 'made_up_mode')
        assert not check.hazardous
        assert math.isnan(check.voltage_v)

    def test_non_numeric_compliance_no_crash(self):
        s = _settings({'res_voltage_compliance': 'not a number'})
        # Should not raise; treats as not-hazardous.
        check = is_potentially_hazardous(s, 'resistance')
        assert not check.hazardous


class TestWarningMessage:
    def test_includes_value_and_threshold(self):
        check = HazardCheck(True, 200.0, 30.0, 'V compliance')
        msg = warning_message(check)
        assert '200' in msg
        assert '30' in msg
        assert 'V compliance' in msg
        # The memo: warn-then-proceed, not block.
        assert 'will proceed' in msg.lower()


class TestWarningSilenced:
    """The one rule for "is the warning silenced now", with the clock given."""

    NOW = 1_800_000_000.0

    def test_nothing_set_is_not_silenced(self):
        assert warning_silenced(_settings({}), self.NOW) is False

    def test_the_sticky_flag_silences(self):
        assert warning_silenced(_settings({'safety_voltage_warn_silenced': True}), self.NOW)

    def test_a_future_time_silences(self):
        s = _settings({'safety_voltage_warn_silenced': False,
                       'safety_voltage_warn_silenced_until': self.NOW + 60.0})
        assert warning_silenced(s, self.NOW) is True

    def test_a_past_time_is_ignored(self):
        s = _settings({'safety_voltage_warn_silenced': False,
                       'safety_voltage_warn_silenced_until': self.NOW - 60.0})
        assert warning_silenced(s, self.NOW) is False

    def test_the_moment_it_expires_the_warning_is_back(self):
        s = _settings({'safety_voltage_warn_silenced_until': self.NOW})
        assert warning_silenced(s, self.NOW) is False

    def test_none_is_not_silenced(self):
        s = _settings({'safety_voltage_warn_silenced': False,
                       'safety_voltage_warn_silenced_until': None})
        assert warning_silenced(s, self.NOW) is False

    def test_the_flag_wins_over_an_expired_time(self):
        s = _settings({'safety_voltage_warn_silenced': True,
                       'safety_voltage_warn_silenced_until': self.NOW - 60.0})
        assert warning_silenced(s, self.NOW) is True

    @pytest.mark.parametrize('until', ['2100-01-01', True, float('inf'), float('nan')])
    def test_a_value_that_is_not_a_time_does_not_silence(self, until):
        s = _settings({'safety_voltage_warn_silenced_until': until})
        assert warning_silenced(s, self.NOW) is False


class TestSafetyAckFields:
    """What an answer to the touch-safety prompt stores, with the clock given."""

    NOW = 1_800_000_000.0

    def _change(self, **fields):
        from resistamet_gui.session.status import SafetyAckFields
        return SafetyAckFields.model_validate(fields).profile_change(self.NOW)

    def test_for_good_is_the_sticky_flag(self):
        assert self._change(silence_for_profile=True) == {'safety_voltage_warn_silenced': True}

    def test_for_seven_days_is_a_time_seven_days_on(self):
        change = self._change(silence_for_days=7)
        assert change == {'safety_voltage_warn_silenced_until': self.NOW + 7 * 86400.0}
        assert warning_silenced(_settings(change), self.NOW + 7 * 86400.0 - 1)
        assert not warning_silenced(_settings(change), self.NOW + 7 * 86400.0)

    def test_no_fields_store_nothing(self):
        assert self._change() == {}

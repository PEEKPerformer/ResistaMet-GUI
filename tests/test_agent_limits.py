"""The worst case of a run, and the agent-limit verdict on it.

Every expected value here is worked out by hand from the settings in the
test, not from the code under test.
"""
import pytest

from resistamet_gui.schema.agent_limits import (
    AUTO_OHMS_MAX_V,
    check_agent_limits,
    worst_case,
)


def run(**measurement):
    return {'measurement': measurement}


#: A 2400 as identify reports it: 210 V, 1.05 A, 22 W.
K2400 = {'address': 'GPIB0::24::INSTR', 'idn': 'KEITHLEY,MODEL 2400', 'model': '2400',
         'max_source_v': 210.0, 'max_source_i': 1.05, 'max_power_w': 22.0}

NO_CAPS = {'max_voltage_v': None, 'max_current_a': None, 'max_power_w': None}


class TestWorstCase:
    def test_resistance_in_manual_range(self):
        case = worst_case(run(res_test_current=-2e-3, res_voltage_compliance=10.0,
                              res_auto_range=False), 'resistance')
        assert (case.voltage_v, case.current_a) == (10.0, 2e-3)
        assert case.power_w == pytest.approx(0.02)
        assert case.voltage_keys == ('res_voltage_compliance',)
        assert case.current_keys == ('res_test_current',)

    def test_resistance_in_auto_range_is_the_instruments_choice(self):
        # Auto-ohms picks its own test current and voltage limit: the current
        # is unknown, the voltage is at most the top ranges' 21 V.
        case = worst_case(run(res_test_current=1e-6, res_voltage_compliance=5.0,
                              res_auto_range=True), 'resistance')
        assert AUTO_OHMS_MAX_V == 21.0
        assert (case.voltage_v, case.current_a, case.power_w) == (21.0, None, None)
        assert case.voltage_keys == ('res_auto_range',)
        assert case.current_keys == ('res_auto_range',)

    def test_a_compliance_above_auto_ohms_ceiling_still_counts(self):
        case = worst_case(run(res_test_current=1e-3, res_voltage_compliance=50.0,
                              res_auto_range=True), 'resistance')
        assert case.voltage_v == 50.0
        assert case.voltage_keys == ('res_voltage_compliance',)

    def test_voltage_source(self):
        case = worst_case(run(vsource_voltage=-12.0, vsource_current_compliance=0.25),
                          'source_v')
        assert (case.voltage_v, case.current_a, case.power_w) == (12.0, 0.25, 3.0)
        assert case.voltage_keys == ('vsource_voltage',)
        assert case.current_keys == ('vsource_current_compliance',)

    def test_current_source_takes_its_compliance_as_the_voltage(self):
        case = worst_case(run(isource_current=-0.5, isource_voltage_compliance=40.0),
                          'source_i')
        assert (case.voltage_v, case.current_a, case.power_w) == (40.0, 0.5, 20.0)
        assert case.voltage_keys == ('isource_voltage_compliance',)
        assert case.current_keys == ('isource_current',)

    def test_four_point_is_not_lowered_by_its_power_stop(self):
        case = worst_case(run(fpp_current=1e-3, fpp_voltage_compliance=5.0,
                              fpp_power_stop_w=1e-4), 'four_point')
        assert (case.voltage_v, case.current_a) == (5.0, 1e-3)
        assert case.power_w == pytest.approx(5e-3)

    @pytest.mark.parametrize('direction', ['up', 'down', 'up_down'])
    def test_voltage_sweep_takes_its_whole_range(self, direction):
        case = worst_case(run(sweep_source='voltage', sweep_start=-3.0, sweep_stop=2.0,
                              sweep_step=0.5, sweep_compliance=0.1,
                              sweep_direction=direction), 'sweep')
        assert (case.voltage_v, case.current_a) == (3.0, 0.1)
        assert case.power_w == pytest.approx(0.3)
        assert case.voltage_keys == ('sweep_start',)
        assert case.current_keys == ('sweep_compliance',)

    def test_current_sweep_takes_its_compliance_as_the_voltage(self):
        case = worst_case(run(sweep_source='current', sweep_start=0.0, sweep_stop=0.5,
                              sweep_step=0.1, sweep_compliance=20.0), 'sweep')
        assert (case.voltage_v, case.current_a, case.power_w) == (20.0, 0.5, 10.0)
        assert case.voltage_keys == ('sweep_compliance',)
        assert case.current_keys == ('sweep_stop',)

    def test_a_symmetric_sweep_names_both_ends(self):
        case = worst_case(run(sweep_source='voltage', sweep_start=-2.0, sweep_stop=2.0,
                              sweep_compliance=0.01), 'sweep')
        assert case.voltage_v == 2.0
        assert case.voltage_keys == ('sweep_start', 'sweep_stop')

    def test_van_der_pauw(self):
        case = worst_case(run(vdp_current=0.01, vdp_voltage_compliance=8.0), 'vdp')
        assert (case.voltage_v, case.current_a) == (8.0, 0.01)
        assert case.power_w == pytest.approx(0.08)

    def test_a_missing_key_is_the_models_default(self):
        # DEFAULT_SETTINGS: vsource_voltage 1.0 V, compliance 0.1 A.
        case = worst_case(run(), 'source_v')
        assert (case.voltage_v, case.current_a) == (1.0, 0.1)

    def test_an_unknown_mode_is_an_error(self):
        with pytest.raises(ValueError):
            worst_case(run(), 'pulse')


class TestTheAgentLimits:
    def test_the_default_voltage_cap_is_30_v_inclusive(self):
        assert check_agent_limits(run(vsource_voltage=30.0), 'source_v').ok
        verdict = check_agent_limits(run(vsource_voltage=30.5), 'source_v')
        assert not verdict.ok
        [violation] = verdict.violations
        assert (violation.limit, violation.source, violation.value, violation.allowed) == \
            ('max_voltage_v', 'agent_limits', 30.5, 30.0)
        assert violation.keys == ['vsource_voltage']
        assert 'vsource_voltage' in violation.message and '30 V' in violation.message

    def test_current_and_power_are_uncapped_by_default(self):
        # 20 V x 3 A = 60 W, under 30 V: within the defaults.
        assert check_agent_limits(run(vsource_voltage=20.0, vsource_current_compliance=3.0),
                                  'source_v').ok

    def test_a_current_cap(self):
        limits = {'max_current_a': 0.1}
        settings = run(isource_current=0.1, isource_voltage_compliance=5.0)
        assert check_agent_limits(settings, 'source_i', limits).ok
        settings['measurement']['isource_current'] = -0.11
        [violation] = check_agent_limits(settings, 'source_i', limits).violations
        assert (violation.limit, violation.value, violation.allowed) == \
            ('max_current_a', 0.11, 0.1)
        assert violation.keys == ['isource_current']

    def test_a_power_cap_names_both_keys(self):
        settings = run(vsource_voltage=12.0, vsource_current_compliance=0.25)  # 3 W
        assert check_agent_limits(settings, 'source_v', {'max_power_w': 3.0}).ok
        [violation] = check_agent_limits(settings, 'source_v', {'max_power_w': 2.5}).violations
        assert (violation.limit, violation.value, violation.allowed) == \
            ('max_power_w', 3.0, 2.5)
        assert violation.keys == ['vsource_voltage', 'vsource_current_compliance']

    def test_every_limit_broken_is_reported(self):
        limits = {'max_voltage_v': 10.0, 'max_current_a': 0.01, 'max_power_w': 0.05}
        verdict = check_agent_limits(run(isource_current=0.02, isource_voltage_compliance=11.0),
                                     'source_i', limits)
        assert [v.limit for v in verdict.violations] == \
            ['max_voltage_v', 'max_current_a', 'max_power_w']

    def test_none_caps_nothing(self):
        assert check_agent_limits(run(vsource_voltage=200.0, vsource_current_compliance=1.0),
                                  'source_v', NO_CAPS).ok

    def test_a_missing_section_is_the_defaults(self):
        assert not check_agent_limits(run(vsource_voltage=31.0), 'source_v', None).ok
        assert not check_agent_limits(run(vsource_voltage=31.0), 'source_v', {}).ok

    def test_auto_ohms_is_within_the_defaults(self):
        settings = run(res_test_current=1e-3, res_voltage_compliance=5.0, res_auto_range=True)
        assert check_agent_limits(settings, 'resistance').ok

    @pytest.mark.parametrize('limits, limit', [({'max_current_a': 1.0}, 'max_current_a'),
                                               ({'max_power_w': 1.0}, 'max_power_w')])
    def test_auto_ohms_cannot_be_held_to_a_current_or_power_cap(self, limits, limit):
        settings = run(res_test_current=1e-3, res_voltage_compliance=5.0, res_auto_range=True)
        [violation] = check_agent_limits(settings, 'resistance', limits).violations
        assert (violation.limit, violation.value) == (limit, None)
        assert 'res_auto_range' in violation.keys
        settings['measurement']['res_auto_range'] = False
        assert check_agent_limits(settings, 'resistance', limits).ok

    def test_auto_ohms_against_a_lower_voltage_cap(self):
        settings = run(res_test_current=1e-3, res_voltage_compliance=5.0, res_auto_range=True)
        [violation] = check_agent_limits(settings, 'resistance',
                                         {'max_voltage_v': 20.0}).violations
        assert (violation.limit, violation.value, violation.keys) == \
            ('max_voltage_v', 21.0, ['res_auto_range'])

    @pytest.mark.parametrize('bad', [0, -1.0, 'abc', True, float('inf')])
    def test_a_cap_that_is_not_valid_refuses_every_run(self, bad):
        verdict = check_agent_limits(run(vsource_voltage=1.0), 'source_v',
                                     {'max_current_a': bad})
        [violation] = verdict.violations
        assert (violation.limit, violation.allowed, violation.keys) == \
            ('max_current_a', None, [])


class TestTheConnectedModel:
    def test_within_the_model_is_ok(self):
        settings = run(isource_current=1.05, isource_voltage_compliance=20.0)  # 21 W
        assert check_agent_limits(settings, 'source_i', NO_CAPS, K2400).ok

    @pytest.mark.parametrize('measurement, mode, limit, value', [
        ({'isource_current': 2.0, 'isource_voltage_compliance': 1.0}, 'source_i',
         'max_source_i', 2.0),
        ({'vsource_voltage': 250.0, 'vsource_current_compliance': 0.01}, 'source_v',
         'max_source_v', 250.0),
        # 25 V x 1 A = 25 W, inside 210 V and 1.05 A but above 22 W.
        ({'vsource_voltage': 25.0, 'vsource_current_compliance': 1.0}, 'source_v',
         'max_power_w', 25.0),
    ])
    def test_beyond_the_model_is_refused_naming_it(self, measurement, mode, limit, value):
        [violation] = check_agent_limits(run(**measurement), mode, NO_CAPS, K2400).violations
        assert (violation.source, violation.model, violation.limit, violation.value) == \
            ('model', '2400', limit, value)
        assert '2400' in violation.message

    def test_both_kinds_are_reported(self):
        verdict = check_agent_limits(run(vsource_voltage=250.0,
                                         vsource_current_compliance=0.01),
                                     'source_v', None, K2400)
        assert [(v.source, v.limit) for v in verdict.violations] == \
            [('agent_limits', 'max_voltage_v'), ('model', 'max_source_v')]

    @pytest.mark.parametrize('instrument', [None, {**K2400, 'model': None}])
    def test_an_unknown_model_is_not_checked(self, instrument):
        settings = run(isource_current=2.0, isource_voltage_compliance=1.0)
        assert check_agent_limits(settings, 'source_i', NO_CAPS, instrument).ok

    def test_auto_ohms_current_is_left_to_the_instrument(self):
        settings = run(res_test_current=1e-3, res_voltage_compliance=5.0, res_auto_range=True)
        assert check_agent_limits(settings, 'resistance', NO_CAPS, K2400).ok

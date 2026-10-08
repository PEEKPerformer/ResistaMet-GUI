"""describe_mode's lines: value, where it comes from, default, what a key accepts."""
from resistamet_gui.mcp.describe import accepts, describe, line

CURRENT = {'type': 'number', 'minimum': 1e-7, 'maximum': 3.0, 'default': 0.001, 'unit': 'A'}
AUTO_ZERO = {'type': 'string', 'enum': ['on', 'once', 'off'], 'default': 'once',
             'description': 'on: re-zero with every reading.'}


class TestAccepts:
    def test_bounds(self):
        assert accepts(CURRENT) == '1e-07 <= x <= 3'

    def test_exclusive_and_integer_bounds(self):
        assert accepts({'type': 'number', 'exclusiveMinimum': 0.0, 'maximum': 200.0}) == \
            '0 < x <= 200'
        assert accepts({'type': 'integer', 'minimum': 0, 'maximum': 1000000}) == \
            'integer 0 <= x <= 1000000'

    def test_choices_and_null(self):
        assert accepts(AUTO_ZERO) == 'one of on|once|off'
        assert accepts({'type': 'number', 'minimum': -50.0, 'nullable': True}) == \
            '-50 <= x, or null'
        assert accepts({'type': 'boolean'}) == 'true|false'


class TestLine:
    def test_a_profile_value_that_is_not_the_default(self):
        assert line(CURRENT, 0.002) == \
            '0.002 A, from the profile; default 0.001; 1e-07 <= x <= 3'

    def test_a_profile_value_that_is_the_default(self):
        assert line(CURRENT, 0.001) == '0.001 A, from the profile (the default); 1e-07 <= x <= 3'

    def test_a_value_the_mode_fixes_names_the_profile_s(self):
        assert line(AUTO_ZERO, 'on', fixed=True, profile_value='once') == (
            '"on", fixed by the mode (the profile\'s "once" is not used); '
            'on: re-zero with every reading.')

    def test_without_a_user_there_is_no_value(self):
        assert line(CURRENT) == 'in A; default 0.001; 1e-07 <= x <= 3'

    def test_a_value_not_measured_is_null(self):
        temperature = {'type': 'number', 'minimum': -50.0, 'maximum': 200.0, 'nullable': True,
                       'default': None, 'unit': 'degC'}
        assert line(temperature, float('nan')) == \
            'null, from the profile (the default); -50 <= x <= 200, or null'

    def test_a_compound_unit_is_said_apart_from_the_value(self):
        start = {'type': 'number', 'minimum': -200.0, 'maximum': 200.0, 'default': 0.0,
                 'unit': 'V or A (sweep_source)'}
        assert line(start, 1.0) == ('1.0, from the profile; in V or A (sweep_source); '
                                    'default 0.0; -200 <= x <= 200')


def test_describe_groups_a_mode_s_own_keys_from_the_shared_ones():
    entry = {'fields': ['fpp_current'], 'override_keys': ['auto_zero', 'fpp_current'],
             'keys': {'fpp_current': CURRENT, 'auto_zero': AUTO_ZERO},
             'fixed': {'auto_zero': 'on'}}
    assert describe(entry, {'fpp_current': 0.002, 'auto_zero': 'on'},
                    {'auto_zero': 'once'}) == {
        'mode_keys': {'fpp_current': '0.002 A, from the profile; default 0.001; '
                                     '1e-07 <= x <= 3'},
        'shared_keys': {'auto_zero': '"on", fixed by the mode (the profile\'s "once" is not '
                                     'used); on: re-zero with every reading.'},
    }

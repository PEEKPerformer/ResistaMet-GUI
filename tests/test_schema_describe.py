"""What GET /schema/settings says about each key: fragment, unit, fixed values."""
import pytest

from resistamet_gui.schema.describe import fixed_values, key_descriptions, unit_of
from resistamet_gui.schema.resolve import allowed_override_keys
from resistamet_gui.schema.settings_modes import MODE_MODELS


@pytest.mark.parametrize("key,unit", [
    ('res_test_current', 'A'),
    ('res_voltage_compliance', 'V'),
    ('vsource_current_compliance', 'A'),
    ('vsource_voltage', 'V'),
    ('vsource_duration_hours', 'h'),
    ('fpp_delta_settling', 's'),
    ('vdp_settling_s', 's'),
    ('sweep_delay', 's'),
    ('fpp_thickness_um', 'um'),
    ('vdp_thickness_cm', 'cm'),
    ('fpp_sample_width_mm', 'mm'),
    ('fpp_array_angle_deg', 'deg'),
    ('fpp_edge_warn_pct', '%'),
    ('fpp_power_warn_w', 'W'),
    ('fpp_temperature_c', 'degC'),
    ('sampling_rate', 'Hz'),
    ('nplc', 'PLC'),
    ('res_cable_null', 'ohm'),
    ('sweep_start', 'V or A (sweep_source)'),
    ('sweep_compliance', 'A or V (the measured quantity)'),
])
def test_the_unit_follows_the_key_name(key, unit):
    assert unit_of(key) == unit


@pytest.mark.parametrize("key", ['fpp_k_factor', 'fpp_alpha', 'filter_count', 'fpp_samples',
                                 'auto_zero', 'stop_on_compliance', 'aux_address'])
def test_a_name_that_does_not_say_has_no_unit(key):
    assert unit_of(key) is None


def test_an_enum_carries_its_choices_default_and_meaning():
    fragment = key_descriptions('four_point')['fpp_geometry']
    assert fragment['type'] == 'string'
    assert fragment['enum'] == ['circle', 'square', 'rectangle_2', 'rectangle_3', 'rectangle_4']
    assert fragment['default'] == 'circle'
    assert 'F84' in fragment['description']
    assert 'title' not in fragment and 'unit' not in fragment


def test_a_bounded_number_carries_its_bounds_and_unit():
    assert key_descriptions('resistance')['res_test_current'] == {
        'type': 'number', 'minimum': 1e-7, 'maximum': 3.0, 'default': 0.001, 'unit': 'A'}


def test_an_exclusive_bound_is_kept_as_one():
    fragment = key_descriptions('sweep')['sweep_step']
    assert fragment['exclusiveMinimum'] == 0.0 and 'minimum' not in fragment


def test_an_optional_number_is_flattened_and_nullable():
    fragment = key_descriptions('four_point')['fpp_temperature_c']
    assert (fragment['type'], fragment['minimum'], fragment['maximum']) == ('number', -50.0, 200.0)
    assert fragment['nullable'] is True and fragment['default'] is None
    assert 'anyOf' not in fragment


def test_a_single_allowed_value_is_a_one_item_enum():
    fragment = key_descriptions('four_point')['fpp_position_correction']
    assert fragment['enum'] == ['warn'] and 'const' not in fragment


def test_a_mode_s_control_key_is_described():
    assert key_descriptions('source_v')['vsource_run_continuous'] == {
        'type': 'boolean', 'default': False,
        'description': "true: run until stopped (vsource_duration_hours becomes 0)."}


@pytest.mark.parametrize("mode", sorted(MODE_MODELS))
def test_every_override_key_is_described_with_a_type(mode):
    described = key_descriptions(mode)
    assert set(described) == allowed_override_keys(mode)
    assert all('type' in fragment for fragment in described.values())


def test_the_values_a_mode_fixes():
    assert fixed_values('four_point') == {'auto_zero': 'on', 'filter_count': 10}
    assert fixed_values('vdp') == {'auto_zero': 'on', 'filter_count': 10}
    assert fixed_values('resistance') == {}

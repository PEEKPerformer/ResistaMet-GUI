"""format_power: the one way a power is written into a message."""
import pytest

from resistamet_gui.formatting import format_power, four_point_power_warning


@pytest.mark.parametrize("watts, text", [
    (1e-4, "100 µW"),     # the hard stop that used to read "0 mW"
    (5e-4, "500 µW"),
    (9e-4, "900 µW"),
    (2e-3, "2 mW"),
    (1.5, "1.5 W"),
    (2.5e-3, "2.5 mW"),
    (0.25, "250 mW"),
    (22.0, "22 W"),
    (1.234e-3, "1.23 mW"),
    (0.0, "0 W"),
    (3e-8, "30 nW"),
    (5e-12, "0.005 nW"),
])
def test_prefix_and_digits(watts, text):
    assert format_power(watts) == text


def test_rounding_up_moves_to_the_next_prefix():
    assert format_power(0.9996e-3) == "1 mW"
    assert format_power(999.6) == "1000 W"


def test_large_values_have_no_exponent():
    assert format_power(1234.0) == "1230 W"


def test_not_a_number_does_not_raise():
    assert format_power(float('nan')) == "nan W"
    assert format_power(float('inf')) == "inf W"


def test_the_power_warning_says_what_lowers_it():
    # 5 mA x 5 V = 25 mW over 10 mW: 10 mW / 5 mA = 2 V of compliance at most.
    text = four_point_power_warning(25e-3, 10e-3, -5e-3)
    assert text.endswith("lower fpp_voltage_compliance (to 2 V or less at this current) "
                         "or fpp_current to bring it under.")


def test_without_a_current_it_names_the_knobs_alone():
    text = four_point_power_warning(0.0, 10e-3, 0.0)
    assert text.endswith("; lower fpp_voltage_compliance or fpp_current to bring it under.")

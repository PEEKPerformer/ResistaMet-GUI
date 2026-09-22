"""format_power: the one way a power is written into a message."""
import pytest

from resistamet_gui.formatting import format_power


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

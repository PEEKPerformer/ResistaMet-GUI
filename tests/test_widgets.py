"""Unit tests for EngineeringSpinBox parsing and formatting."""
import math
import pytest

from resistamet_gui.ui.widgets import parse_engineering, format_engineering


class TestParseEngineering:
    """Test engineering notation parsing."""

    def test_milliamps(self):
        assert parse_engineering("1mA") == pytest.approx(0.001)

    def test_microamps_u(self):
        assert parse_engineering("100uA") == pytest.approx(0.0001)

    def test_microamps_mu(self):
        assert parse_engineering("100µA") == pytest.approx(0.0001)

    def test_nanoamps(self):
        assert parse_engineering("500nA") == pytest.approx(5e-7)

    def test_amps_bare(self):
        assert parse_engineering("1.5A") == pytest.approx(1.5)

    def test_millivolts(self):
        assert parse_engineering("10mV") == pytest.approx(0.01)

    def test_volts(self):
        assert parse_engineering("0.5V") == pytest.approx(0.5)

    def test_negative(self):
        assert parse_engineering("-1.5mA") == pytest.approx(-0.0015)

    def test_bare_number(self):
        assert parse_engineering("0.001") == pytest.approx(0.001)

    def test_scientific_notation(self):
        assert parse_engineering("1e-3") == pytest.approx(0.001)

    def test_with_spaces(self):
        assert parse_engineering("  1.5 mA  ") == pytest.approx(0.0015)

    def test_kiloohms(self):
        assert parse_engineering("4.7kΩ") == pytest.approx(4700.0)

    def test_megaohms(self):
        assert parse_engineering("1MΩ") == pytest.approx(1e6)

    def test_empty_string(self):
        assert parse_engineering("") is None

    def test_garbage(self):
        assert parse_engineering("hello") is None

    def test_zero(self):
        assert parse_engineering("0") == pytest.approx(0.0)

    def test_negative_bare(self):
        assert parse_engineering("-0.5") == pytest.approx(-0.5)

    def test_prefix_only_no_number(self):
        assert parse_engineering("mA") is None


class TestFormatEngineering:
    """Test engineering notation formatting."""

    def test_milliamps(self):
        result = format_engineering(0.001, 'A')
        assert 'mA' in result
        assert '1.00' in result

    def test_microamps(self):
        result = format_engineering(0.0001, 'A')
        assert 'µA' in result
        assert '100' in result

    def test_nanoamps(self):
        result = format_engineering(5e-7, 'A')
        assert 'nA' in result
        assert '500' in result

    def test_volts(self):
        result = format_engineering(1.5, 'V')
        assert 'V' in result
        assert '1.5' in result

    def test_millivolts(self):
        result = format_engineering(0.01, 'V')
        assert 'mV' in result

    def test_zero(self):
        assert format_engineering(0, 'A') == '0 A'

    def test_nan(self):
        assert format_engineering(float('nan'), 'V') == '-- V'

    def test_inf(self):
        assert format_engineering(float('inf'), 'A') == '-- A'

    def test_negative(self):
        result = format_engineering(-0.001, 'A')
        assert '-' in result
        assert 'mA' in result

    def test_large_value(self):
        result = format_engineering(1e6, 'Ω')
        assert 'MΩ' in result

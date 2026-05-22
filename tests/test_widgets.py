"""Unit tests for EngineeringSpinBox parsing and formatting."""
import math
import pytest

from resistamet_gui.ui.widgets import (
    format_engineering, format_with_uncertainty, parse_engineering,
    precision_for_nplc,
)


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


class TestFormatWithUncertainty:
    """Shared-prefix value ± uncertainty formatting."""

    def test_resistance_kohm(self):
        # 1487 ± 1.3 Ω in a 2 V / 1 mA resistance run. Shared kΩ prefix.
        # σ rounded to 2 sig figs → 0.0013 kΩ; value to same decimal → 1.4870.
        result = format_with_uncertainty(1487.0, 1.3, 'Ω')
        assert result == "1.4870 ± 0.0013 kΩ"

    def test_low_resistance_milliohm(self):
        # 99.5 mΩ ± 1.0 mΩ. σ=1.0 already has 2 sig figs (trailing 0 counts),
        # so we round at the 1-decimal place in mΩ.
        result = format_with_uncertainty(0.0995, 0.001, 'Ω')
        assert result == "99.5 ± 1.0 mΩ"

    def test_current_microamps(self):
        # 100 µA ± 25 nA → 100.000 ± 0.025 µA.
        result = format_with_uncertainty(100e-6, 25e-9, 'A')
        assert result == "100.025 ± 0.025 µA" or result == "100.000 ± 0.025 µA"

    def test_falls_back_when_uncertainty_nonfinite(self):
        # NaN σ → plain engineering format (no ± shown).
        assert format_with_uncertainty(1.5, float('nan'), 'V') == format_engineering(1.5, 'V')

    def test_falls_back_when_uncertainty_zero(self):
        # 0 σ would be a divide-by-zero in the rounding step; fall back.
        assert format_with_uncertainty(1.5, 0.0, 'V') == format_engineering(1.5, 'V')

    def test_nan_value(self):
        assert format_with_uncertainty(float('nan'), 0.1, 'Ω') == "-- Ω"

    def test_unc_sig_figs_one(self):
        # σ rounded to 1 sf instead of 2.
        result = format_with_uncertainty(1487.0, 1.3, 'Ω', unc_sig_figs=1)
        assert result == "1.487 ± 0.001 kΩ"


class TestPrecisionForNplc:
    """Map NPLC → Keithley 2400 resolution → sig figs."""

    def test_normal_speed_is_six_figs(self):
        # NPLC=1 → 6½-digit mode. The headline "6½-digit precision" claim
        # in the datasheet binds to Speed = Normal.
        assert precision_for_nplc(1.0) == 6

    def test_high_nplc_still_six(self):
        # Above 1 PLC doesn't unlock more digits — the hardware caps at 6½.
        assert precision_for_nplc(10.0) == 6

    def test_medium_speed_is_five_figs(self):
        # NPLC=0.1 → 5½-digit mode.
        assert precision_for_nplc(0.1) == 5

    def test_fast_speed_is_four_figs(self):
        # NPLC=0.01 → 4½-digit mode.
        assert precision_for_nplc(0.01) == 4

    def test_boundary_low(self):
        # Step-function transitions at 0.05 and 0.5, matching the
        # accuracy.py NPLC bucketing for the offset modifier.
        assert precision_for_nplc(0.04) == 4
        assert precision_for_nplc(0.05) == 5
        assert precision_for_nplc(0.49) == 5
        assert precision_for_nplc(0.5) == 6

    def test_nonfinite_falls_back(self):
        # Garbage in, conservative-out — both NaN and infinity return the
        # narrowest precision rather than promoting to 6.
        assert precision_for_nplc(float('nan')) == 4
        assert precision_for_nplc(float('inf')) == 4

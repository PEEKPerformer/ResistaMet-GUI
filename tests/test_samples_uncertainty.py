"""The sigma_R the source-mode parsers put in each row.

``session/samples.py`` computes ``resistance_unc`` inline for source-V
(R = V_set / I_meas) and source-I (R = V_meas / I_set), and that is the
number written to file. Expected values are worked by hand from the
Series 2400 datasheet (1KW-2798-3) rows already cited in test_accuracy.py,
combined as the root sum of squares of the relative uncertainties.
"""

import math

import pytest

from resistamet_gui.session.samples import parse_source_i, parse_source_v


SETTINGS = {"vsource_current_compliance": 0.1, "isource_voltage_compliance": 21.0}


def _source_v(volts, amps):
    data, _, _ = parse_source_v([repr(volts), repr(amps), "0"], 0, False, SETTINGS,
                                1.0, "2400", None, None)
    return data


def _source_i(volts, amps):
    data, _, _ = parse_source_i([repr(volts), repr(amps), "0"], 0, False, SETTINGS,
                                1.0, "2400", None, None)
    return data


class TestSourceV:
    # Source 1 V on the 2 V range: 0.02 % x 1 V + 600 uV = 800 uV.
    # Measure 1 mA on the 1 mA range: 0.027 % x 1 mA + 60 nA = 330 nA.
    # R = 1000 Ohm; sigma_R / R = sqrt(8.0e-4 ** 2 + 3.3e-4 ** 2)
    #                           = sqrt(6.4e-7 + 1.089e-7) = 8.6539e-4.
    SIGMA_R = 0.86539

    @pytest.mark.parametrize("sign", [1.0, -1.0])
    def test_resistance_unc(self, sign):
        data = _source_v(sign * 1.0, sign * 1e-3)
        assert data["resistance_unc"] == pytest.approx(self.SIGMA_R, rel=1e-4)

    def test_component_uncertainties(self):
        data = _source_v(1.0, 1e-3)
        assert data["voltage_unc"] == pytest.approx(800e-6, rel=1e-9)
        assert data["current_unc"] == pytest.approx(330e-9, rel=1e-9)

    def test_no_current_gives_nan(self):
        assert math.isnan(_source_v(1.0, 0.0)["resistance_unc"])


class TestSourceI:
    # Source 1 mA on the 1 mA range: 0.034 % x 1 mA + 200 nA = 540 nA.
    # Measure 1 V on the 2 V range: 0.012 % x 1 V + 300 uV = 420 uV.
    # R = 1000 Ohm; sigma_R / R = sqrt(4.2e-4 ** 2 + 5.4e-4 ** 2)
    #                           = sqrt(1.764e-7 + 2.916e-7) = 6.8411e-4.
    SIGMA_R = 0.68411

    @pytest.mark.parametrize("sign", [1.0, -1.0])
    def test_resistance_unc(self, sign):
        data = _source_i(sign * 1.0, sign * 1e-3)
        assert data["resistance_unc"] == pytest.approx(self.SIGMA_R, rel=1e-4)

    def test_component_uncertainties(self):
        data = _source_i(1.0, 1e-3)
        assert data["voltage_unc"] == pytest.approx(420e-6, rel=1e-9)
        assert data["current_unc"] == pytest.approx(540e-9, rel=1e-9)

    def test_no_voltage_gives_nan(self):
        assert math.isnan(_source_i(0.0, 1e-3)["resistance_unc"])

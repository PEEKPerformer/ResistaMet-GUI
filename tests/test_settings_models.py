"""Model behaviour that is not a bound: derived values and request shape.

No Qt here — these are the checks a headless client relies on.
"""
import pytest
from pydantic import ValidationError

from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.schema.settings_modes import (
    MODE_MODELS,
    FourPointSettings,
    ResistanceSettings,
    RunRequest,
    VdpSettings,
)


class TestWorstCasePower:
    """The number the worker's pre-flight refuses to start above."""

    def test_product_of_magnitudes(self):
        settings = FourPointSettings(fpp_current=1e-3, fpp_voltage_compliance=100.0)
        assert settings.worst_case_power_w() == pytest.approx(0.1)

    def test_negative_current_counts_the_same(self):
        forward = FourPointSettings(fpp_current=1e-3, fpp_voltage_compliance=5.0)
        reverse = FourPointSettings(fpp_current=-1e-3, fpp_voltage_compliance=5.0)
        assert reverse.worst_case_power_w() == forward.worst_case_power_w()


class TestFourPointTemperature:
    """'Not measured' is None here; the sentinel and NaN stay outside."""

    def test_defaults_to_not_measured(self):
        assert FourPointSettings().fpp_temperature_c is None

    def test_accepts_a_measured_value(self):
        assert FourPointSettings(fpp_temperature_c=23.5).fpp_temperature_c == 23.5

    def test_rejects_out_of_range(self):
        with pytest.raises(ValidationError):
            FourPointSettings(fpp_temperature_c=500.0)


class TestModeModels:
    def test_every_mode_has_a_model(self):
        assert set(MODE_MODELS) == {
            'resistance', 'source_v', 'source_i', 'four_point', 'sweep', 'vdp'
        }

    def test_fields_exist_in_default_settings(self):
        """A field the profile cannot hold would never round-trip."""
        for mode, model in MODE_MODELS.items():
            for name in model.model_fields:
                assert name in DEFAULT_SETTINGS['measurement'], f"{mode}.{name}"

    def test_unknown_keys_pass_through(self):
        settings = ResistanceSettings(res_test_current=1e-3, some_future_key=7)
        assert settings.model_dump()['some_future_key'] == 7

    def test_out_of_range_is_rejected(self):
        with pytest.raises(ValidationError):
            VdpSettings(vdp_readings_per_polarity=0)


class TestRunRequest:
    def test_minimal_request(self):
        request = RunRequest(mode='resistance', username='alice', sample_name='wafer1')
        assert request.overrides == {}
        assert request.prompt_timeout_s == 900.0

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValidationError):
            RunRequest(mode='hall', username='alice', sample_name='wafer1')

    def test_empty_sample_name_rejected(self):
        with pytest.raises(ValidationError):
            RunRequest(mode='resistance', username='alice', sample_name='')

    def test_unknown_top_level_key_rejected(self):
        """A typo in a request must fail loudly, unlike a stored profile."""
        with pytest.raises(ValidationError):
            RunRequest(mode='resistance', username='alice', sample_name='w', smaple_rate=5)

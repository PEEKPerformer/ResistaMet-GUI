"""Model behaviour that is not a bound: derived values and request shape.

No Qt here — these are the checks a headless client relies on.
"""
import pytest
from pydantic import ValidationError

from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.schema.settings_common import InstrumentSettings
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


class TestGpibInterface:
    """The Prologix interface resource: empty, or pyvisa's PRLGX INTFC grammar."""

    #: Serial on macOS, Linux and Windows (COM5), then Ethernet with and
    #: without the port, and a second board.
    EXAMPLES = (
        'PRLGX-ASRL::/dev/cu.usbserial-PX12345::INTFC',
        'PRLGX-ASRL::/dev/ttyUSB0::INTFC',
        'PRLGX-ASRL::5::INTFC',
        'PRLGX-TCPIP::192.168.1.50::1234::INTFC',
        'PRLGX-TCPIP::prologix.local::INTFC',
        'PRLGX-ASRL1::/dev/ttyUSB0::INTFC',
    )

    def test_default_is_none(self):
        assert InstrumentSettings().gpib_interface == ''

    @pytest.mark.parametrize('name', EXAMPLES)
    def test_real_names_are_accepted(self, name):
        assert InstrumentSettings(gpib_interface=name).gpib_interface == name

    @pytest.mark.parametrize('name', EXAMPLES)
    def test_what_is_accepted_is_what_pyvisa_parses(self, name):
        """The validator must not drift from the installed grammar."""
        from pyvisa import rname
        if not hasattr(rname, 'PrlgxASRLIntfc'):
            pytest.skip("this pyvisa predates the Prologix resource names")
        parsed = rname.ResourceName.from_string(name)
        assert isinstance(parsed, (rname.PrlgxASRLIntfc, rname.PrlgxTCPIPIntfc))

    def test_surrounding_whitespace_is_dropped(self):
        settings = InstrumentSettings(gpib_interface='  PRLGX-ASRL::5::INTFC ')
        assert settings.gpib_interface == 'PRLGX-ASRL::5::INTFC'

    @pytest.mark.parametrize('name', [
        'GPIB0::INTFC',                  # an interface, but not a Prologix one
        'ASRL5::INSTR',                  # the serial port itself
        'PRLGX-ASRL::/dev/ttyUSB0',      # no resource class
        'PRLGX-ASRL::INTFC',             # no serial device
        'PRLGX-USB::/dev/ttyUSB0::INTFC',
        'PRLGX-ASRL::5::intfc',          # pyvisa wants INTFC in capitals
    ])
    def test_other_names_are_rejected(self, name):
        with pytest.raises(ValidationError):
            InstrumentSettings(gpib_interface=name)


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


class TestRunRequestSpot:
    SPOT = {'map_id': 'wafer7', 'index': 2, 'label': 'edge', 'x_mm': 10.0, 'y_mm': 0.0}

    def test_absent_by_default(self):
        request = RunRequest(mode='four_point', username='alice', sample_name='wafer7')
        assert request.spot is None

    def test_four_point_may_carry_one(self):
        request = RunRequest(mode='four_point', username='alice', sample_name='wafer7',
                             spot=self.SPOT)
        assert request.spot.map_id == 'wafer7'
        assert request.spot.has_position

    @pytest.mark.parametrize("mode", ['resistance', 'source_v', 'source_i', 'sweep', 'vdp'])
    def test_no_other_mode_may(self, mode):
        with pytest.raises(ValidationError, match="belongs to a four_point run"):
            RunRequest(mode=mode, username='alice', sample_name='wafer7', spot=self.SPOT)

    def test_the_spot_is_validated(self):
        with pytest.raises(ValidationError):
            RunRequest(mode='four_point', username='alice', sample_name='wafer7',
                       spot={**self.SPOT, 'map_id': '../wafer7'})

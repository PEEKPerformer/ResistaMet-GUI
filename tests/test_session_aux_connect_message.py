"""An aux sensor that will not open is described as one, not as a Keithley."""
import pytest
import pyvisa

from resistamet_gui.session import continuous_run
from resistamet_gui.session.continuous_run import ContinuousRun
from resistamet_gui.session.control import RunControl
from resistamet_gui.session.emitter import EventEmitter, ListSink


def _settings(tmp_path, aux_address):
    return {
        "measurement": {
            "sampling_rate": 100.0, "nplc": 0.1, "settling_time": 0.0,
            "gpib_address": "GPIB0::24::INSTR", "stop_on_compliance": False,
            "auto_zero": "on", "filter_enabled": False,
            "res_test_current": 1e-3, "res_voltage_compliance": 5.0,
            "res_measurement_type": "4-wire", "res_auto_range": False,
            "res_offset_comp": False, "res_cable_null": 0.0,
            "aux_log_enabled": True, "aux_driver": "arduino_thermocouple",
            "aux_address": aux_address,
        },
        "display": {"enable_plot": False, "plot_update_interval": 100, "buffer_size": 100},
        "file": {"auto_save_interval": 60, "data_directory": str(tmp_path / "data")},
        "output": {"format": "csv", "compression": "never", "compression_threshold_mb": 5},
    }


@pytest.fixture(autouse=True)
def _no_sleep_inhibitor(monkeypatch):
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)


def _aux_error(tmp_path, aux_address='ASRL99::INSTR'):
    sink = ListSink()
    ContinuousRun('resistance', 'r100', 'alice', _settings(tmp_path, aux_address), RunControl(),
                   EventEmitter(sink)).execute()
    errors = [e.payload for e in sink.of_type('error')]
    assert [e['code'] for e in errors] == ['aux_connect_failed']
    assert sink.of_type('run_ended')[0].payload['reason'] == 'aux_connect_failed'
    return errors[0]['message']


KEITHLEY_ADVICE = ('Keithley', 'GPIB', 'Click OK', 'instrument')


class TestAuxConnectFailures:
    def test_a_port_that_is_not_there(self, fake_rm, tmp_path):
        message = _aux_error(tmp_path)
        assert message.startswith('Auxiliary sensor: ')
        assert 'ASRL99::INSTR' in message
        assert not any(word in message for word in KEITHLEY_ADVICE), message

    @pytest.mark.parametrize('code', [
        pyvisa.constants.StatusCode.error_timeout,
        pyvisa.constants.StatusCode.error_resource_busy,
        pyvisa.constants.StatusCode.error_resource_not_found,
    ])
    def test_a_visa_error(self, fake_rm, tmp_path, monkeypatch, code):
        def refuse(driver, address):
            raise pyvisa.errors.VisaIOError(code)
        monkeypatch.setattr(continuous_run, 'make_sensor', refuse)
        message = _aux_error(tmp_path, 'ASRL6::INSTR')
        assert message.startswith('Auxiliary sensor: ')
        assert 'ASRL6::INSTR' in message
        assert not any(word in message for word in KEITHLEY_ADVICE), message

    def test_the_sensor_s_own_complaint_is_passed_on(self, fake_rm, tmp_path, monkeypatch):
        def refuse(driver, address):
            raise RuntimeError("no channel description received")
        monkeypatch.setattr(continuous_run, 'make_sensor', refuse)
        assert 'no channel description received' in _aux_error(tmp_path, 'ASRL6::INSTR')


class TestTheOpenStepsAnswerTrueOrFalse:
    """Their docstrings say "False on failure"; they returned None."""

    def _run(self, tmp_path, aux_address):
        return ContinuousRun('resistance', 'r100', 'alice', _settings(tmp_path, aux_address),
                              RunControl(), EventEmitter(ListSink()))

    def test_the_aux_sensor(self, fake_rm, tmp_path):
        run = self._run(tmp_path, 'ASRL99::INSTR')
        assert run._open_aux_sensor(run.settings['measurement']) is False
        run = self._run(tmp_path, 'ASRL6::INSTR')
        try:
            assert run._open_aux_sensor(run.settings['measurement']) is True
        finally:
            run._cleanup()

    def test_the_output_file(self, fake_rm, tmp_path):
        run = self._run(tmp_path, 'ASRL6::INSTR')
        blocker = tmp_path / 'data'
        blocker.write_text('a file where the data directory should be')
        assert run._open_output_file(run.settings['measurement'], '1.00mA') is False

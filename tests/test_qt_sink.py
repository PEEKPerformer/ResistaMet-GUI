"""Events must reach the Qt Signals the GUI has always listened to.

The sink is the whole Qt surface for run events during the transition, so it
gets a direct test: feed it one event of each type and check which Signal
fired with what.
"""
import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from resistamet_gui.session.events import Event
from resistamet_gui.workers import _QtSink


class _FakeSignal:
    def __init__(self, name='', order=None):
        self.emitted = []
        self._name = name
        self._order = order

    def emit(self, *args):
        self.emitted.append(args)
        if self._order is not None:
            self._order.append(self._name)


class _FakeWorker:
    def __init__(self):
        #: Signal names in the order they fired, across all signals.
        self.order = []
        for name in ('data_point', 'sample_derived', 'compliance_hit', 'overpower_hit',
                      'sweep_complete', 'measurement_complete', 'instrument_identified',
                      'status_update', 'error_occurred'):
            setattr(self, name, _FakeSignal(name, self.order))
        for name in ('geometry_ready', 'geometry_complete', 'vdp_complete'):
            setattr(self, name, _FakeSignal(name, self.order))


def _send(event_type, payload):
    worker = _FakeWorker()
    _QtSink(worker)(Event(type=event_type, seq=1, t=0.0, payload=payload))
    return worker


def test_sample_becomes_data_point():
    worker = _send('sample', {'t_unix': 12.5, 'elapsed_s': 2.5, 'compliance': 'OK',
                               'event_marker': 'MARK', 'values': {'resistance': 100.0}})
    assert worker.data_point.emitted == [(12.5, {'resistance': 100.0}, 'OK', 'MARK')]


def test_values_are_passed_through_uncoerced():
    """aux_fault is a string; the GUI has always received the dict as parsed."""
    worker = _send('sample', {'t_unix': 1.0, 'elapsed_s': 1.0, 'compliance': 'OK',
                               'event_marker': '', 'values': {'aux_fault': '0', 'r': 1.0}})
    assert worker.data_point.emitted[0][1] == {'aux_fault': '0', 'r': 1.0}


def test_derived_values_arrive_before_their_data_point():
    """The 4PP panel pairs a data_point with the derived values it already
    holds. Were data_point first, every panel row would show this sample's
    V and I beside the previous sample's Rs and rho."""
    derived = {'ratio': 10.0, 'rs': 45.32, 'rho': None, 'sigma': None,
               'v_unc': 1e-6, 'i_unc': 1e-9, 'method': 'legacy'}
    worker = _send('sample', {'t_unix': 12.5, 'elapsed_s': 2.5, 'compliance': 'OK',
                               'event_marker': '', 'values': {'voltage': 1e-3},
                               'derived': derived})
    assert worker.order == ['sample_derived', 'data_point']
    assert worker.sample_derived.emitted == [(12.5, derived)]
    assert worker.data_point.emitted[0][0] == 12.5


def test_a_sample_without_derived_values_fires_only_data_point():
    worker = _send('sample', {'t_unix': 1.0, 'elapsed_s': 1.0, 'compliance': 'OK',
                               'event_marker': '', 'values': {'resistance': 100.0}})
    assert worker.order == ['data_point']
    assert worker.sample_derived.emitted == []


def test_compliance_becomes_compliance_hit():
    worker = _send('compliance', {'kind': 'Voltage', 'stop_on_compliance': True})
    assert worker.compliance_hit.emitted == [('Voltage',)]


def test_overpower_becomes_overpower_hit():
    worker = _send('overpower_trip', {'measured_w': 0.2, 'stop_w': 0.1})
    assert worker.overpower_hit.emitted == [(0.2, 0.1)]


def test_sweep_segment_becomes_sweep_complete():
    worker = _send('sweep_segment', {'direction': 'forward', 'voltages': [0.0, 1.0],
                                      'currents': [0.0, 0.1], 'compliance': ['OK', 'OK']})
    assert worker.sweep_complete.emitted == [([0.0, 1.0], [0.0, 0.1], ['OK', 'OK'])]


def test_acquisition_finished_becomes_measurement_complete():
    worker = _send('acquisition_finished', {'mode': 'resistance'})
    assert worker.measurement_complete.emitted == [('resistance',)]


def test_instrument_connected_becomes_instrument_identified():
    worker = _send('instrument_connected', {'address': 'GPIB0::24::INSTR', 'idn': 'KEITHLEY,2420',
                                             'model': '2420'})
    assert worker.instrument_identified.emitted == [('2420',)]


def test_line_frequency_has_no_signal():
    """The GUI reads it from the status log; the API gets the event."""
    worker = _send('line_frequency', {'hz': 60.0, 'assumed': False})
    assert all(not getattr(worker, name).emitted
                for name in ('data_point', 'status_update', 'error_occurred'))


def test_unknown_event_is_dropped_quietly():
    worker = _send('run_started', {'mode': 'resistance'})
    assert worker.data_point.emitted == []


def test_log_becomes_status_update():
    worker = _send('log', {'level': 'info', 'code': 'connecting', 'message': 'Connecting...'})
    assert worker.status_update.emitted == [('Connecting...',)]


def test_warning_log_also_goes_to_status_update():
    """The GUI has one status stream; level and code are for other clients."""
    worker = _send('log', {'level': 'warning', 'code': 'retry', 'message': 'VISA error (retry 1/5)'})
    assert worker.status_update.emitted == [('VISA error (retry 1/5)',)]
    assert worker.error_occurred.emitted == []


def test_error_becomes_error_occurred():
    worker = _send('error', {'code': 'read_error', 'source': 'smu',
                              'message': 'VISA Read Error. Stopping.', 'fatal': True})
    assert worker.error_occurred.emitted == [('VISA Read Error. Stopping.',)]


def test_vdp_geometry_complete_keeps_its_dict_shape():
    """The GUI reads this dict by key; index travels beside it, as before."""
    worker = _send('vdp_geometry_complete', {
        'index': 2, 'name': 'Configuration C', 'group': 'B',
        'label_pos': 'V_34', 'v_pos': 1.2e-3, 'label_neg': 'V_43', 'v_neg': -1.2e-3,
        'current_a': 1e-3,
    })
    index, geometry = worker.geometry_complete.emitted[0]
    assert index == 2
    assert 'index' not in geometry
    assert geometry['name'] == 'Configuration C'
    assert geometry['v_pos'] == 1.2e-3


def test_vdp_result_reaches_the_panel():
    payload = {
        'rho_a': 1.0, 'rho_b': 1.1, 'rho_avg': 1.05, 'sheet_resistance': 5.65e-3,
        'q_a': 1.01, 'q_b': 1.02, 'f_a': 0.999, 'f_b': 0.998, 'homogeneous': True,
        'asymmetry_pct': 1.4, 'voltages': {'V_12_43': 1e-3}, 'current_a': 1e-3,
        'thickness_cm': 0.05, 'sheet_resistance_uncertainty': 1e-5,
        'rho_avg_uncertainty': 2e-5,
    }
    worker = _send('vdp_result', payload)
    assert worker.vdp_complete.emitted == [(payload,)]


def test_vdp_geometry_prompt_becomes_geometry_ready():
    worker = _send('prompt', {
        'prompt_id': 'vdp_geometry-1', 'kind': 'vdp_geometry',
        'options': ['proceed', 'abort'], 'requires_human': True,
        'detail': {'index': 0, 'name': 'Configuration A', 'source_high': 1,
                   'source_low': 2, 'sense_high': 3, 'sense_low': 4,
                   'label_pos': 'V_43', 'label_neg': 'V_34', 'group': 'A'},
    })
    index, geometry = worker.geometry_ready.emitted[0]
    assert index == 0
    assert 'index' not in geometry
    assert geometry['name'] == 'Configuration A'


def test_other_prompt_kinds_do_not_reach_the_vdp_signal():
    worker = _send('prompt', {
        'prompt_id': 'safety_voltage_ack-1', 'kind': 'safety_voltage_ack',
        'options': ['acknowledge', 'cancel'], 'requires_human': True, 'detail': {},
    })
    assert worker.geometry_ready.emitted == []

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
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


class _FakeWorker:
    def __init__(self):
        for name in ('data_point', 'compliance_hit', 'overpower_hit', 'sweep_complete',
                      'measurement_complete', 'instrument_identified', 'status_update',
                      'error_occurred'):
            setattr(self, name, _FakeSignal())


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

"""The event envelope: numbering, payload validation, wire form, delivery."""
import json
import math
import threading

import pytest
from pydantic import ValidationError

from resistamet_gui.session.emitter import EventEmitter, ListSink
from resistamet_gui.session.events import EVENT_SCHEMA_VERSION, Event, LogPayload


@pytest.fixture
def sink():
    return ListSink()


@pytest.fixture
def emitter(sink):
    return EventEmitter(sink, run_id='run-1', clock=lambda: 1000.0)


class TestSequencing:
    def test_seq_starts_at_one_and_increments(self, emitter, sink):
        emitter.log('connecting', 'Connecting...')
        emitter.log('connected', 'Connected.')
        assert [event.seq for event in sink.events] == [1, 2]

    def test_run_id_and_version_stamped(self, emitter, sink):
        emitter.log('connecting', 'Connecting...')
        event = sink.events[0]
        assert event.run_id == 'run-1'
        assert event.v == EVENT_SCHEMA_VERSION
        assert event.t == 1000.0

    def test_concurrent_emits_get_unique_numbers(self, sink):
        emitter = EventEmitter(sink)
        threads = [threading.Thread(target=emitter.log, args=('progress', f'{i}'))
                    for i in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(event.seq for event in sink.events) == list(range(1, 51))


class TestDelivery:
    def test_sink_is_not_called_under_the_lock(self, sink):
        """A sink that emits again must not deadlock the acquisition thread."""
        emitter = EventEmitter(sink)
        seen = []

        def reentrant(event):
            seen.append(event.type)
            if event.type == 'log':
                emitter.error('boom', 'run', 'from inside the sink')

        emitter._sink = reentrant
        emitter.log('progress', 'first')
        assert seen == ['log', 'error']

    def test_failing_sink_does_not_raise(self):
        def broken(event):
            raise RuntimeError("client went away")

        emitter = EventEmitter(broken)
        event = emitter.log('progress', 'still returns')
        assert event.seq == 1


class TestPayloads:
    def test_log_payload_validated_at_the_emit_site(self, emitter):
        with pytest.raises(ValidationError):
            emitter.emit('log', {'level': 'shouting', 'code': 'x', 'message': 'y'})

    def test_error_defaults_to_fatal(self, emitter, sink):
        emitter.error('visa_timeout', 'smu', 'timed out')
        assert sink.events[0].payload['fatal'] is True

    def test_unknown_event_type_passes_through(self, emitter, sink):
        """Payload models arrive with the PR that first emits their event."""
        emitter.emit('not_yet_modelled', {'anything': 1})
        assert sink.events[0].payload == {'anything': 1}

    def test_extra_keys_rejected(self):
        with pytest.raises(ValidationError):
            LogPayload(level='info', code='x', message='y', extra=1)


class TestWireForm:
    def test_non_finite_floats_serialize_as_null(self):
        event = Event(type='sample', seq=1, t=1.0,
                      payload={'temperature_c': float('nan'), 'r': 100.0})
        on_wire = json.loads(event.model_dump_json())
        assert on_wire['payload']['temperature_c'] is None
        assert on_wire['payload']['r'] == 100.0

    def test_in_process_sinks_still_see_the_float(self, emitter, sink):
        emitter.emit('sample', {'temperature_c': float('nan')})
        assert math.isnan(sink.events[0].payload['temperature_c'])

"""The event hand-off: drop policy, filters, and the WebSocket.

The hub's job is to protect the acquisition thread from slow clients, so most
of this tests the queue policy directly rather than through a socket.
"""
import asyncio
import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from resistamet_gui.api import create_app
from resistamet_gui.api.event_hub import (
    DROPPABLE_HIGH_WATER, EventHub, ClientStream,
)
from resistamet_gui.session.emitter import EventEmitter, ListSink
from resistamet_gui.session.events import Event
from resistamet_gui.session.manager import MeasurementSession

TOKEN = 'test-token'


def _event(event_type, seq=1, run_id='run-1', **payload):
    return Event(type=event_type, seq=seq, t=0.0, run_id=run_id, payload=payload)


class TestDropPolicy:
    @pytest.fixture(autouse=True)
    def _loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        yield loop
        loop.close()

    def test_samples_are_shed_above_the_high_water_mark(self):
        stream = ClientStream(capacity=10, high_water=5)
        for seq in range(20):
            assert stream.offer(_event('sample', seq=seq, t_unix=0.0, elapsed_s=0.0,
                                        compliance='OK', event_marker='', values={}))
        assert stream.dropped > 0

    def test_load_bearing_events_use_the_reserve(self):
        stream = ClientStream(capacity=10, high_water=5)
        for seq in range(20):
            stream.offer(_event('sample', seq=seq, t_unix=0.0, elapsed_s=0.0,
                                 compliance='OK', event_marker='', values={}))
        # room was kept for these
        assert stream.offer(_event('run_ended', seq=99, reason='user_stop'))

    def test_client_is_dropped_when_even_the_reserve_is_full(self):
        stream = ClientStream(capacity=3, high_water=3)
        for seq in range(3):
            assert stream.offer(_event('run_ended', seq=seq, reason='user_stop'))
        assert stream.offer(_event('run_ended', seq=4, reason='user_stop')) is False
        assert stream.overflowed is True


class TestFilters:
    @pytest.fixture
    def hub(self):
        return EventHub(clock=lambda: 0.0)

    def test_compliance_forwards_only_transitions(self, hub):
        first = hub._forward(_event('compliance', kind='Voltage'))
        second = hub._forward(_event('compliance', kind='Voltage'))
        assert (first, second) == (True, False)

    def test_an_ok_sample_clears_the_compliance_state(self, hub):
        hub._forward(_event('compliance', kind='Voltage'))
        hub._forward(_event('sample', t_unix=0.0, elapsed_s=0.0, compliance='OK',
                             event_marker='', values={}))
        assert hub._forward(_event('compliance', kind='Voltage')) is True

    def test_switching_compliance_kind_is_a_transition(self, hub):
        hub._forward(_event('compliance', kind='Voltage'))
        assert hub._forward(_event('compliance', kind='Current')) is True

    def test_progress_logs_are_throttled(self):
        now = {'t': 0.0}
        hub = EventHub(clock=lambda: now['t'])
        assert hub._forward(_event('log', level='info', code='progress', message='a'))
        assert not hub._forward(_event('log', level='info', code='progress', message='b'))
        now['t'] = 1.0
        assert hub._forward(_event('log', level='info', code='progress', message='c'))

    def test_other_logs_are_never_throttled(self, hub):
        for _ in range(5):
            assert hub._forward(_event('log', level='info', code='connected', message='x'))

    def test_samples_are_always_forwarded(self, hub):
        for seq in range(5):
            assert hub._forward(_event('sample', seq=seq, t_unix=0.0, elapsed_s=0.0,
                                        compliance='OK', event_marker='', values={}))


class TestPublishIsNonBlocking:
    def test_publish_without_a_loop_is_a_no_op(self):
        """Events emitted before the server starts must not raise."""
        hub = EventHub()
        hub.publish(_event('run_started', mode='resistance', sample_name='w',
                            username='alice', settings={}, started_at=0.0))

    def test_the_run_thread_is_never_blocked(self):
        """A stalled client must not slow the emitter down."""
        hub = EventHub()
        loop = asyncio.new_event_loop()
        hub.bind(loop)
        emitter = EventEmitter(hub.publish, run_id='run-1')

        began = time.time()
        for _ in range(2000):
            emitter.emit('sample', {'t_unix': 0.0, 'elapsed_s': 0.0, 'compliance': 'OK',
                                     'event_marker': '', 'values': {}})
        assert time.time() - began < 2.0
        loop.close()


class TestWebSocket:
    @pytest.fixture
    def session(self):
        made = MeasurementSession(ListSink())
        yield made
        made.close(timeout=5.0)

    @pytest.fixture
    def app(self, session, tmp_path):
        return create_app(session, token=TOKEN, profile_provider=lambda u: {})

    def test_wrong_token_is_closed(self, app):
        with TestClient(app) as client:
            with pytest.raises(Exception):
                with client.websocket_connect('/session/events/ws?token=nope'):
                    pass

    def test_events_reach_a_connected_client(self, app):
        with TestClient(app) as client:
            with client.websocket_connect(f'/session/events/ws?token={TOKEN}') as ws:
                hub = app.state.api.hub
                hub.publish(_event('log', level='info', code='connected',
                                    message='Connected to: KEITHLEY'))
                message = ws.receive_json()
                assert message['type'] == 'log'
                assert message['payload']['message'] == 'Connected to: KEITHLEY'
                assert message['run_id'] == 'run-1'


class TestHistory:
    """A dropped connection must be resumable, or say it is not."""

    @pytest.fixture
    def hub(self):
        return EventHub(clock=lambda: 0.0)

    def _fill(self, hub, count, start=1):
        for seq in range(start, start + count):
            hub._deliver(_event('sample', seq=seq, t_unix=0.0, elapsed_s=0.0,
                                 compliance='OK', event_marker='', values={}))

    def test_resume_returns_only_what_came_after(self, hub):
        self._fill(hub, 10)
        events, gap = hub.history('run-1', since_seq=7)
        assert [e.seq for e in events] == [8, 9, 10]
        assert gap is False

    def test_a_fresh_client_gets_everything(self, hub):
        self._fill(hub, 5)
        events, gap = hub.history('run-1', since_seq=0)
        assert len(events) == 5
        assert gap is False

    def test_limit_is_respected(self, hub):
        self._fill(hub, 10)
        events, _ = hub.history('run-1', since_seq=0, limit=3)
        assert [e.seq for e in events] == [1, 2, 3]

    def test_other_runs_are_filtered_out(self, hub):
        self._fill(hub, 3)
        hub._deliver(_event('run_ended', seq=1, run_id='run-2', reason='user_stop'))
        events, _ = hub.history('run-2', since_seq=0)
        assert [e.run_id for e in events] == ['run-2']

    def test_a_gap_is_reported_when_history_has_rolled(self):
        from collections import deque

        hub = EventHub(clock=lambda: 0.0)
        hub._history = deque(maxlen=5)
        self._fill(hub, 20)
        events, gap = hub.history('run-1', since_seq=2)
        assert gap is True
        assert [e.seq for e in events] == [16, 17, 18, 19, 20]


class TestPullRoute:
    """Request/response clients should not have to hold a socket open."""

    @pytest.fixture
    def session(self):
        made = MeasurementSession(ListSink())
        yield made
        made.close(timeout=5.0)

    def test_events_can_be_polled(self, session):
        app = create_app(session, token=TOKEN, profile_provider=lambda u: {})
        with TestClient(app) as client:
            client.headers.update({'Authorization': f'Bearer {TOKEN}'})
            hub = app.state.api.hub
            hub._deliver(_event('log', seq=1, level='info', code='connected',
                                 message='Connected'))
            hub._deliver(_event('log', seq=2, level='info', code='configuring',
                                 message='Configuring'))

            body = client.get('/session/events?since_seq=1').json()
            assert [e['seq'] for e in body['events']] == [2]
            assert body['last_seq'] == 2
            assert body['gap'] is False

    def test_polling_needs_a_token(self, session):
        app = create_app(session, token=TOKEN, profile_provider=lambda u: {})
        with TestClient(app) as client:
            assert client.get('/session/events').status_code == 401

    def test_websocket_replays_from_a_cursor(self, session):
        app = create_app(session, token=TOKEN, profile_provider=lambda u: {})
        with TestClient(app) as client:
            hub = app.state.api.hub
            for seq in (1, 2, 3):
                hub._deliver(_event('log', seq=seq, level='info', code='connected',
                                     message=f'msg{seq}'))

            url = f'/session/events/ws?token={TOKEN}&run_id=run-1&since_seq=1'
            with client.websocket_connect(url) as ws:
                assert ws.receive_json()['seq'] == 2
                assert ws.receive_json()['seq'] == 3

    def test_websocket_reports_a_gap_before_replaying(self, session):
        from collections import deque

        app = create_app(session, token=TOKEN, profile_provider=lambda u: {})
        with TestClient(app) as client:
            hub = app.state.api.hub
            hub._history = deque(maxlen=3)
            for seq in range(1, 11):
                hub._deliver(_event('log', seq=seq, level='info', code='connected',
                                     message=f'msg{seq}'))

            url = f'/session/events/ws?token={TOKEN}&run_id=run-1&since_seq=2'
            with client.websocket_connect(url) as ws:
                first = ws.receive_json()
                assert first['type'] == 'gap'
                assert first['since_seq'] == 2

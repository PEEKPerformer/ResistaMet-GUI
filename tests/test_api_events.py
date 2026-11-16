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
        asyncio.set_event_loop(loop)    # Python 3.9 binds the queue to it
        # A client that is never drained and slow to take each event.
        stream = hub.add_client()
        taken = []
        stalled = {'on': True}
        real_offer = stream.offer

        def slow_offer(event):
            taken.append(event)
            if stalled['on']:
                time.sleep(0.01)
            return real_offer(event)

        stream.offer = slow_offer
        emitter = EventEmitter(hub.publish, run_id='run-1')

        began = time.time()
        for _ in range(2000):
            emitter.emit('sample', {'t_unix': 0.0, 'elapsed_s': 0.0, 'compliance': 'OK',
                                     'event_marker': '', 'values': {}})
        assert time.time() - began < 2.0
        assert taken == []              # nothing reached the client on this thread

        # ...and the hand-off did happen: the loop delivers every one.
        stalled['on'] = False
        loop.call_soon(loop.stop)
        loop.run_forever()
        assert len(taken) == 2000
        assert stream._queue.qsize() + stream.dropped == 2000
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
        from starlette.websockets import WebSocketDisconnect

        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect) as closed:
                with client.websocket_connect('/session/events/ws?token=nope'):
                    pass
        assert closed.value.code == 4401

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


class TestOverflowedClientIsDisconnected:
    """A client the hub gave up on must find out: its socket is closed."""

    def test_the_socket_is_closed_with_an_application_code(self):
        from starlette.websockets import WebSocketDisconnect

        session = MeasurementSession(ListSink())
        hub = EventHub(capacity=3, high_water=3)
        app = create_app(session, token=TOKEN, profile_provider=lambda u: {}, hub=hub)
        burst = [_event('run_ended', seq=100 + i, reason='completed') for i in range(10)]
        try:
            with TestClient(app) as client:
                with client.websocket_connect(f'/session/events/ws?token={TOKEN}') as ws:
                    # One callback, so the handler cannot drain in between:
                    # what a stalled client looks like from the hub's side.
                    hub._loop.call_soon_threadsafe(
                        lambda: [hub._deliver(event) for event in burst])

                    received = []
                    with pytest.raises(WebSocketDisconnect) as closed:
                        for _ in range(len(burst) + 1):
                            received.append(ws.receive_json()['seq'])

                assert closed.value.code == 4408
                # Whatever arrived is the unbroken head of the burst: the
                # client resumes from the last seq it saw.
                assert len(received) <= 3
                assert received == [100, 101, 102][:len(received)]
                assert hub._clients == set()
        finally:
            session.close(timeout=5.0)


class TestDisconnectWatcher:
    def test_it_returns_on_disconnect_instead_of_asking_again(self):
        """Starlette raises RuntimeError on a receive() after the disconnect."""
        from resistamet_gui.api.events_ws import _watch_for_disconnect

        class Socket:
            def __init__(self):
                self.messages = [{'type': 'websocket.receive', 'text': 'ping'},
                                 {'type': 'websocket.disconnect', 'code': 1001}]

            async def receive(self):
                if not self.messages:
                    raise RuntimeError('Cannot call "receive" once a disconnect message '
                                       'has been received.')
                return self.messages.pop(0)

        assert asyncio.run(_watch_for_disconnect(Socket())) is None

    def test_a_wrong_token_of_any_length_is_refused(self):
        from starlette.websockets import WebSocketDisconnect

        session = MeasurementSession(ListSink())
        app = create_app(session, token=TOKEN, profile_provider=lambda u: {})
        try:
            with TestClient(app) as client:
                for wrong in ('', 'x', TOKEN + 'x', 'tést'):
                    with pytest.raises(WebSocketDisconnect) as closed:
                        with client.websocket_connect(f'/session/events/ws?token={wrong}'):
                            pass
                    assert closed.value.code == 4401, wrong
        finally:
            session.close(timeout=5.0)


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


class TestCursorAcrossRuns:
    """``seq`` restarts with every run; the hub's ``cursor`` never does."""

    @pytest.fixture
    def hub(self):
        hub = EventHub(clock=lambda: 0.0)
        for seq in range(1, 6):
            hub._deliver(_event('log', seq=seq, run_id='run-1', level='info', code='x',
                                message='m'))
        for seq in range(1, 4):
            hub._deliver(_event('log', seq=seq, run_id='run-2', level='info', code='x',
                                message='m'))
        return hub

    def _ids(self, events):
        return [(e.run_id, e.seq) for e in events]

    def test_every_delivered_event_carries_the_next_cursor(self, hub):
        events, _ = hub.history()
        assert [e.cursor for e in events] == list(range(1, 9))
        assert hub.cursor == 8

    def test_an_event_the_hub_filters_out_takes_no_cursor(self):
        hub = EventHub(clock=lambda: 0.0)
        hub._deliver(_event('compliance', seq=1, kind='Voltage'))
        hub._deliver(_event('compliance', seq=2, kind='Voltage'))  # not a transition
        hub._deliver(_event('log', seq=3, level='info', code='x', message='m'))
        assert [(e.seq, e.cursor) for e in hub.history()[0]] == [(1, 1), (3, 2)]

    def test_resuming_by_cursor_crosses_into_the_next_run(self, hub):
        events, gap = hub.history(since_cursor=4)
        assert self._ids(events) == [('run-1', 5), ('run-2', 1), ('run-2', 2), ('run-2', 3)]
        assert gap is False

    def test_pages_by_cursor_neither_repeat_nor_skip(self, hub):
        first, _ = hub.history(since_cursor=0, limit=6)
        second, _ = hub.history(since_cursor=first[-1].cursor, limit=6)
        assert self._ids(first + second) == self._ids(hub.history()[0])
        assert len(first) == 6 and len(second) == 2

    def test_resuming_an_old_run_also_replays_the_runs_after_it(self, hub):
        """A run started while the socket was down must not lose its head."""
        events, gap = hub.history('run-1', since_seq=5)
        assert self._ids(events) == [('run-2', 1), ('run-2', 2), ('run-2', 3)]
        assert gap is False

        events, _ = hub.history('run-1', since_seq=3)
        assert self._ids(events) == [('run-1', 4), ('run-1', 5),
                                     ('run-2', 1), ('run-2', 2), ('run-2', 3)]

    def test_a_run_whose_head_was_evicted_is_a_gap_even_from_the_start(self):
        from collections import deque

        hub = EventHub(clock=lambda: 0.0)
        hub._history = deque(maxlen=4)
        for seq in range(1, 11):
            hub._deliver(_event('log', seq=seq, level='info', code='x', message='m'))

        assert hub.history('run-1', since_seq=0)[1] is True
        assert hub.history(since_seq=0)[1] is True
        assert hub.history(since_cursor=0)[1] is True
        assert hub.history(since_cursor=6) == (hub.history()[0], False)

    def test_a_run_that_is_gone_from_the_ring_is_a_gap(self, hub):
        events, gap = hub.history('run-0', since_seq=800)
        assert gap is True
        assert len(events) == 8

    def test_nothing_delivered_is_not_a_gap(self):
        assert EventHub().history(since_cursor=0) == ([], False)


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

    def test_websocket_resumes_by_cursor_into_a_newer_run(self, session):
        app = create_app(session, token=TOKEN, profile_provider=lambda u: {})
        with TestClient(app) as client:
            hub = app.state.api.hub
            for run, seqs in (('run-1', (1, 2)), ('run-2', (1, 2))):
                for seq in seqs:
                    hub._deliver(_event('log', seq=seq, run_id=run, level='info', code='x',
                                         message='m'))

            with client.websocket_connect(
                    f'/session/events/ws?token={TOKEN}&since_cursor=2') as ws:
                got = [ws.receive_json() for _ in range(2)]
            assert [(e['run_id'], e['seq'], e['cursor']) for e in got] == [
                ('run-2', 1, 3), ('run-2', 2, 4)]

    def test_websocket_resume_with_the_old_run_replays_the_new_runs_start(self, session):
        app = create_app(session, token=TOKEN, profile_provider=lambda u: {})
        with TestClient(app) as client:
            hub = app.state.api.hub
            hub._deliver(_event('log', seq=800, run_id='run-1', level='info', code='x',
                                 message='m'))
            hub._deliver(_event('run_started', seq=1, run_id='run-2', mode='resistance'))

            url = f'/session/events/ws?token={TOKEN}&run_id=run-1&since_seq=800'
            with client.websocket_connect(url) as ws:
                first = ws.receive_json()
            assert (first['type'], first['run_id'], first['seq']) == ('run_started', 'run-2', 1)

    def test_polling_by_cursor(self, session):
        app = create_app(session, token=TOKEN, profile_provider=lambda u: {})
        with TestClient(app) as client:
            client.headers.update({'Authorization': f'Bearer {TOKEN}'})
            hub = app.state.api.hub
            for run in ('run-1', 'run-2'):
                for seq in (1, 2, 3):
                    hub._deliver(_event('log', seq=seq, run_id=run, level='info', code='x',
                                         message='m'))

            first = client.get('/session/events?since_cursor=0&limit=4').json()
            rest = client.get(f"/session/events?since_cursor={first['cursor']}").json()

            assert first['cursor'] == 4
            assert [(e['run_id'], e['seq']) for e in first['events'] + rest['events']] == [
                ('run-1', 1), ('run-1', 2), ('run-1', 3), ('run-2', 1), ('run-2', 2), ('run-2', 3)]
            assert rest['cursor'] == 6
            assert client.get('/session/events?since_cursor=6').json()['cursor'] == 6

    @pytest.mark.parametrize('query', ['limit=0', 'limit=-1', 'limit=10001', 'since_seq=-1',
                                       'since_cursor=-1'])
    def test_out_of_range_paging_is_refused(self, session, query):
        app = create_app(session, token=TOKEN, profile_provider=lambda u: {})
        with TestClient(app) as client:
            client.headers.update({'Authorization': f'Bearer {TOKEN}'})
            assert client.get(f'/session/events?{query}').status_code == 422

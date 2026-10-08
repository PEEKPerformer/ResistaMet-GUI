"""``wait_for`` and the event digest, against a scripted backend and a fake clock.

The history stand-in pages the way ``EventHub.history`` does: ``run_id``
plus ``since_seq`` gives that run's events after the seq and every later
run's, ``since_cursor`` everything after the cursor.
"""
import asyncio

import pytest

pytest.importorskip("anyio")

from resistamet_gui.mcp import waiting  # noqa: E402
from resistamet_gui.mcp.waiting import digest, parse_until, thin  # noqa: E402


def _event(cursor, run_id, seq, event_type, **payload):
    return {'v': 1, 'type': event_type, 'run_id': run_id, 'seq': seq, 't': 100.0 + cursor,
            'cursor': cursor, 'payload': payload}


class Script:
    """Statuses one per GET /session (the last repeats), a history ring, a clock."""

    def __init__(self, statuses, history=()):
        self.statuses = list(statuses)
        self.history = list(history)
        self.now = 0.0
        self.sleeps = []
        self.requests = []

    async def get(self, path, params=None):
        self.requests.append((path, dict(params or {})))
        if path == '/session':
            return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        assert path == '/session/events'
        limit = params['limit']
        if 'since_cursor' in params:
            events = [e for e in self.history if e['cursor'] > params['since_cursor']]
        else:
            named = [e for e in self.history if e['run_id'] == params['run_id']]
            first = named[0]['cursor'] if named else 0
            events = [e for e in self.history if e['cursor'] >= first and
                      (e['run_id'] != params['run_id'] or e['seq'] > params['since_seq'])]
            gap = bool(named) and named[0]['seq'] > params['since_seq'] + 1
        events = events[:limit]
        cursor = events[-1]['cursor'] if events else params.get('since_cursor', 0)
        return {'events': events, 'gap': 'since_cursor' not in params and gap,
                'last_seq': events[-1]['seq'] if events else 0, 'cursor': cursor}

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _wait(script, until, timeout_s=10.0, known_prompt=None, ignore_prompt=None):
    return asyncio.run(waiting.wait_for(script.get, parse_until(until), timeout_s,
                                        known_prompt=known_prompt,
                                        ignore_prompt=ignore_prompt,
                                        clock=script.clock, sleep=script.sleep))


RUNNING = {'state': 'running', 'run_id': 'run-1', 'last_seq': 5, 'pending_prompt': None}
IDLE = {'state': 'idle', 'run_id': 'run-1', 'last_seq': 8, 'pending_prompt': None}
PROMPT = {'prompt_id': 'run-1:vdp_geometry-1', 'kind': 'vdp_geometry',
          'options': ['proceed', 'abort'], 'requires_human': True, 'detail': {}}


class TestParseUntil:
    @pytest.mark.parametrize('text, kind, samples, state', [
        ('run_ended', 'run_ended', 0, ''),
        ('prompt', 'prompt', 0, ''),
        ('prompt_answered', 'prompt_answered', 0, ''),
        ('samples:5', 'samples', 5, ''),
        (' state:paused ', 'state', 0, 'paused'),
    ])
    def test_the_four_forms(self, text, kind, samples, state):
        until = parse_until(text)
        assert (until.kind, until.samples, until.state) == (kind, samples, state)

    @pytest.mark.parametrize('text', ['', 'done', 'samples:', 'samples:0', 'samples:x',
                                      'samples:-2', 'state:sleeping', 'state:'])
    def test_anything_else_is_refused_with_the_forms(self, text):
        with pytest.raises(ValueError) as caught:
            parse_until(text)
        assert 'samples' in str(caught.value) or 'states' in str(caught.value)


class TestThin:
    def test_short_lists_are_kept_whole(self):
        assert thin([1, 2, 3], 5) == [1, 2, 3]

    def test_ten_to_four_keeps_the_ends_and_spreads_the_rest(self):
        # step (10 - 1) / (4 - 1) = 3: indices 0, 3, 6, 9.
        assert thin(list(range(10)), 4) == [0, 3, 6, 9]

    def test_seven_to_three(self):
        # step 3: indices 0, 3, 6.
        assert thin(list('abcdefg'), 3) == ['a', 'd', 'g']

    def test_one_is_the_latest(self):
        assert thin([1, 2, 3], 1) == [3]


def _run_events():
    return [
        _event(1, 'run-1', 1, 'run_started', mode='resistance'),
        _event(2, 'run-1', 2, 'log', code='connecting', message='Connecting'),
        _event(3, 'run-1', 3, 'sample', t_unix=1.0, elapsed_s=0.1, compliance='OK',
               event_marker='', values={'resistance': 100.0}, derived=None, delta=None),
        _event(4, 'run-1', 4, 'log', code='progress', message='1 sample'),
        _event(5, 'run-1', 5, 'sample', t_unix=2.0, elapsed_s=0.2, compliance='OK',
               event_marker='lamp on', values={'resistance': 101.0}, derived=None, delta=None),
        _event(6, 'run-1', 6, 'sample', t_unix=3.0, elapsed_s=0.3, compliance='V_COMP',
               event_marker='', values={'resistance': 102.0}, derived=None, delta=None),
        _event(7, 'run-1', 7, 'compliance', kind='Voltage', stop_on_compliance=False),
        _event(8, 'run-1', 8, 'run_ended', reason='user_stop', ok=True, samples=3,
               path='measurement_data/alice/x.csv'),
    ]


class TestDigest:
    def test_by_default_samples_and_progress_logs_are_left_out_but_counted(self):
        digested = digest(_run_events())
        assert [e['type'] for e in digested['events']] == ['run_started', 'log', 'compliance',
                                                           'run_ended']
        assert digested['samples_total'] == 3
        assert digested['samples_returned'] == 0
        assert (digested['last_seq'], digested['more']) == (8, False)

    def test_samples_are_thinned_and_compacted(self):
        digested = digest(_run_events(), include_samples=True, max_samples=2)
        samples = [e for e in digested['events'] if e['type'] == 'sample']
        assert [s['seq'] for s in samples] == [3, 6]  # first and last of three
        assert samples[0]['payload'] == {'elapsed_s': 0.1, 'compliance': 'OK',
                                         'values': {'resistance': 100.0}}
        assert digested['samples_returned'] == 2

    def test_types_choose_exactly(self):
        digested = digest(_run_events(), types=['log', 'run_ended'])
        assert [(e['type'], e['seq']) for e in digested['events']] == [
            ('log', 2), ('log', 4), ('run_ended', 8)]

    def test_naming_sample_in_types_includes_them(self):
        digested = digest(_run_events(), types=['sample'])
        assert [e['seq'] for e in digested['events']] == [3, 5, 6]

    def test_past_the_event_cap_it_stops_and_says_where(self):
        digested = digest(_run_events(), include_samples=True, max_events=2)
        assert [(e['type'], e['seq']) for e in digested['events']] == [
            ('run_started', 1), ('log', 2)]
        assert (digested['last_seq'], digested['more']) == (2, True)
        assert digested['samples_total'] == 0

    def test_max_samples_is_held_to_two_hundred(self):
        events = [_event(i, 'run-1', i, 'sample', values={}) for i in range(1, 501)]
        assert digest(events, include_samples=True, max_samples=5000)['samples_returned'] == 200

    def test_no_events(self):
        assert digest([]) == {'events': [], 'last_seq': None, 'more': False,
                              'samples_total': 0, 'samples_returned': 0}


class TestReadHistory:
    def test_pages_by_cursor_and_keeps_only_the_run(self, monkeypatch):
        monkeypatch.setattr(waiting, 'PAGE', 3)
        history = _run_events() + [_event(9, 'run-2', 1, 'run_started')]
        script = Script([RUNNING], history)
        events, gap, cursor = asyncio.run(waiting.read_history(script.get, 'run-1', 0))
        assert [e['seq'] for e in events] == [1, 2, 3, 4, 5, 6, 7, 8]
        assert (gap, cursor) == (False, 9)
        assert [params.get('since_cursor') for _, params in script.requests] == [
            None, 3, 6, 9]

    def test_a_history_that_lost_the_start_is_a_gap(self):
        script = Script([RUNNING], _run_events()[3:])
        _, gap, _ = asyncio.run(waiting.read_history(script.get, 'run-1', 0))
        assert gap is True


class TestWaitFor:
    def test_samples_fire_once_written(self):
        history = _run_events()[:3]
        script = Script([RUNNING], history)

        async def more_samples(seconds):
            script.now += seconds
            cursor = len(script.history) + 1
            script.history.append(_event(cursor, 'run-1', cursor, 'sample'))

        script.sleep = more_samples
        waited = _wait(script, 'samples:3')
        assert waited['fired'] == 'samples:3'
        assert waited['samples'] == 3  # one in the history, two more while waiting
        assert waited['waited_s'] == 1.0  # two polls of 0.5 s
        assert waited['status'] == RUNNING
        assert 'run_ended' not in waited

    def test_a_prompt_ends_any_wait(self):
        script = Script([RUNNING, {**RUNNING, 'state': 'awaiting_prompt',
                                   'pending_prompt': PROMPT}], _run_events()[:3])
        waited = _wait(script, 'samples:100')
        assert waited['fired'] == 'prompt'
        assert waited['status']['pending_prompt'] == PROMPT

    def test_prompt_fires_as_asked(self):
        script = Script([{**RUNNING, 'state': 'awaiting_prompt', 'pending_prompt': PROMPT}])
        assert _wait(script, 'prompt')['fired'] == 'prompt'

    def test_a_prompt_the_agent_was_shown_is_waited_through(self):
        # The trial: wait_for("run_ended") at a vdP prompt came back at once,
        # with the prompt it was told to wait for a person to answer.
        prompted = {**RUNNING, 'state': 'awaiting_prompt', 'pending_prompt': PROMPT}
        script = Script([prompted])
        waited = _wait(script, 'run_ended', timeout_s=2.0, known_prompt=PROMPT['prompt_id'])
        assert (waited['fired'], waited['waited_s']) == ('timeout', 2.0)
        assert waited['prompt_at_start'] == {'prompt_id': PROMPT['prompt_id'],
                                             'still_pending': True}

    def test_the_person_answering_shows_as_the_run_moving_on(self):
        prompted = {**RUNNING, 'state': 'awaiting_prompt', 'pending_prompt': PROMPT}
        script = Script([prompted, prompted, RUNNING])
        waited = _wait(script, 'state:running', known_prompt=PROMPT['prompt_id'])
        assert (waited['fired'], waited['waited_s']) == ('state:running', 1.0)
        assert waited['prompt_at_start']['still_pending'] is False

    def test_the_next_prompt_ends_the_wait_on_the_one_before(self):
        following = {**PROMPT, 'prompt_id': 'run-1:vdp_geometry-2'}
        script = Script([{**RUNNING, 'state': 'awaiting_prompt', 'pending_prompt': PROMPT},
                         RUNNING,
                         {**RUNNING, 'state': 'awaiting_prompt', 'pending_prompt': following}])
        waited = _wait(script, 'run_ended', known_prompt=PROMPT['prompt_id'])
        assert waited['fired'] == 'prompt'
        assert waited['status']['pending_prompt'] == following
        assert waited['prompt_at_start'] == {'prompt_id': PROMPT['prompt_id'],
                                             'still_pending': False}

    def test_a_pending_prompt_the_agent_was_not_shown_ends_the_wait_at_once(self):
        # Raised between start_run's reply and the first wait: nobody has
        # told the person yet.
        prompted = {**RUNNING, 'state': 'awaiting_prompt', 'pending_prompt': PROMPT}
        waited = _wait(Script([prompted]), 'samples:5', known_prompt='run-1:other-1')
        assert (waited['fired'], waited['waited_s']) == ('prompt', 0.0)
        assert 'prompt_at_start' not in waited

    def test_prompt_returns_at_once_even_at_a_prompt_the_agent_was_shown(self):
        prompted = {**RUNNING, 'state': 'awaiting_prompt', 'pending_prompt': PROMPT}
        waited = _wait(Script([prompted]), 'prompt', known_prompt=PROMPT['prompt_id'])
        assert (waited['fired'], waited['waited_s']) == ('prompt', 0.0)

    def test_a_wait_that_began_without_a_prompt_says_nothing_of_one(self):
        assert 'prompt_at_start' not in _wait(Script([RUNNING, IDLE], _run_events()),
                                              'run_ended')

    def test_the_end_of_the_run_ends_any_wait_and_says_how(self):
        script = Script([RUNNING, RUNNING, IDLE], _run_events())
        waited = _wait(script, 'state:paused')
        assert waited['fired'] == 'run_ended'
        assert waited['run_ended'] == {'reason': 'user_stop', 'ok': True, 'samples': 3,
                                       'path': 'measurement_data/alice/x.csv'}
        assert waited['waited_s'] == 1.0

    def test_run_ended_fires_as_asked(self):
        script = Script([RUNNING, IDLE], _run_events())
        assert _wait(script, 'run_ended')['fired'] == 'run_ended'

    def test_a_state_fires_as_asked(self):
        script = Script([RUNNING, {**RUNNING, 'state': 'paused'}])
        assert _wait(script, 'state:paused')['fired'] == 'state:paused'

    def test_the_timeout(self):
        script = Script([RUNNING])
        waited = _wait(script, 'run_ended', timeout_s=2.0)
        assert waited['fired'] == 'timeout'
        assert waited['waited_s'] == 2.0
        assert script.sleeps == [0.5, 0.5, 0.5, 0.5]

    def test_the_timeout_is_capped(self):
        script = Script([RUNNING])
        waited = _wait(script, 'run_ended', timeout_s=10_000)
        assert waited['waited_s'] == waiting.MAX_WAIT_S

    def test_with_no_run_in_progress_it_returns_at_once(self):
        script = Script([{**IDLE, 'run_id': None}])
        waited = _wait(script, 'prompt')
        assert (waited['fired'], waited['waited_s']) == ('run_ended', 0.0)
        assert script.sleeps == []

    def test_a_new_run_by_someone_else_ends_the_wait_on_the_old_one(self):
        other = {**RUNNING, 'run_id': 'run-2'}
        history = _run_events() + [_event(9, 'run-2', 1, 'run_started')]
        script = Script([RUNNING, other], history)
        waited = _wait(script, 'samples:50')
        assert waited['fired'] == 'run_ended'
        assert waited['run_ended']['reason'] == 'user_stop'


PROMPTED = {**RUNNING, 'state': 'awaiting_prompt', 'pending_prompt': PROMPT}
FOLLOWING = {**PROMPT, 'prompt_id': 'run-1:vdp_geometry-2'}


class TestWithoutMemory:
    """The forms that work from a server process that never showed the prompt.

    In the third trial a client started a new MCP server for every call:
    nothing remembered which prompt the agent had been shown, and every
    wait_for('run_ended') at a prompt came back at once with it.
    """

    def test_with_nothing_remembered_a_pending_prompt_ends_the_wait_at_once(self):
        # The trial's failure, kept as it is: this is what the forms below fix.
        waited = _wait(Script([PROMPTED]), 'run_ended')
        assert (waited['fired'], waited['waited_s']) == ('prompt', 0.0)

    def test_prompt_answered_fires_once_the_person_answers(self):
        script = Script([PROMPTED, PROMPTED, RUNNING])
        waited = _wait(script, 'prompt_answered')
        assert (waited['fired'], waited['waited_s']) == ('prompt_answered', 1.0)
        assert waited['status'] == RUNNING
        assert waited['prompt_at_start'] == {'prompt_id': PROMPT['prompt_id'],
                                             'still_pending': False}

    def test_prompt_answered_shows_the_next_prompt_when_it_is_already_up(self):
        script = Script([PROMPTED, {**PROMPTED, 'pending_prompt': FOLLOWING}])
        waited = _wait(script, 'prompt_answered')
        assert waited['fired'] == 'prompt_answered'
        assert waited['status']['pending_prompt'] == FOLLOWING

    def test_prompt_answered_fires_when_the_run_ends(self):
        # Released by a stop, or the prompt timed out and the run ended.
        script = Script([PROMPTED, IDLE], _run_events())
        waited = _wait(script, 'prompt_answered')
        assert waited['fired'] == 'prompt_answered'
        assert waited['run_ended']['reason'] == 'user_stop'

    def test_prompt_answered_times_out_while_nobody_answers(self):
        waited = _wait(Script([PROMPTED]), 'prompt_answered', timeout_s=2.0)
        assert (waited['fired'], waited['waited_s']) == ('timeout', 2.0)
        assert waited['prompt_at_start']['still_pending'] is True

    def test_prompt_answered_with_no_prompt_pending_holds_at_once(self):
        waited = _wait(Script([RUNNING]), 'prompt_answered')
        assert (waited['fired'], waited['waited_s']) == ('prompt_answered', 0.0)
        assert 'prompt_at_start' not in waited

    def test_prompt_answered_at_a_prompt_the_agent_did_not_expect_returns_it(self):
        # The person answered the first before the wait began, and the second
        # is up: nobody has told the user of it.
        waited = _wait(Script([{**PROMPTED, 'pending_prompt': FOLLOWING}]),
                       'prompt_answered', ignore_prompt=PROMPT['prompt_id'])
        assert (waited['fired'], waited['waited_s']) == ('prompt', 0.0)
        assert waited['status']['pending_prompt'] == FOLLOWING
        assert 'prompt_at_start' not in waited

    def test_prompt_answered_at_the_prompt_expected_waits_for_it(self):
        script = Script([PROMPTED, RUNNING])
        waited = _wait(script, 'prompt_answered', ignore_prompt=PROMPT['prompt_id'])
        assert waited['fired'] == 'prompt_answered'

    @pytest.mark.parametrize('until', ['run_ended', 'state:running', 'samples:5'])
    def test_an_ignored_prompt_does_not_end_the_wait(self, until):
        waited = _wait(Script([PROMPTED]), until, timeout_s=1.0,
                       ignore_prompt=PROMPT['prompt_id'])
        assert (waited['fired'], waited['waited_s']) == ('timeout', 1.0)
        assert waited['prompt_at_start'] == {'prompt_id': PROMPT['prompt_id'],
                                             'still_pending': True}

    def test_any_other_prompt_still_ends_it(self):
        script = Script([PROMPTED, RUNNING, {**PROMPTED, 'pending_prompt': FOLLOWING}])
        waited = _wait(script, 'run_ended', ignore_prompt=PROMPT['prompt_id'])
        assert waited['fired'] == 'prompt'
        assert waited['status']['pending_prompt'] == FOLLOWING

    def test_an_ignored_prompt_makes_prompt_wait_for_the_next(self):
        script = Script([PROMPTED, PROMPTED, {**PROMPTED, 'pending_prompt': FOLLOWING}])
        waited = _wait(script, 'prompt', ignore_prompt=PROMPT['prompt_id'])
        assert (waited['fired'], waited['waited_s']) == ('prompt', 1.0)
        assert waited['status']['pending_prompt'] == FOLLOWING

    def test_a_remembered_prompt_does_not_hold_back_prompt(self):
        # Only the id said in the call does; wait_for('prompt') is how an
        # agent asks what is pending.
        waited = _wait(Script([PROMPTED]), 'prompt', known_prompt=PROMPT['prompt_id'])
        assert (waited['fired'], waited['waited_s']) == ('prompt', 0.0)


def _wait_then_stop(script, until, timeout_s=10.0, ends_with=None):
    """As _wait, with a stop that records when it was sent (and ends the run)."""
    stops = []

    async def stop():
        stops.append(script.now)
        if ends_with is not None:
            cursor = len(script.history) + 1
            script.history.append(_event(cursor, 'run-1', cursor, 'run_ended', **ends_with))

    waited = asyncio.run(waiting.wait_for(script.get, parse_until(until), timeout_s,
                                          stop=stop, clock=script.clock, sleep=script.sleep))
    return waited, stops


class TestThenStop:
    def test_the_run_is_stopped_when_the_condition_holds_and_followed_to_its_end(self):
        # run_ended will be the run's fifth event (seq 5).
        idle = {**IDLE, 'last_seq': 5}
        script = Script([RUNNING, RUNNING, {**RUNNING, 'state': 'stopping'}, idle],
                        _run_events()[:3])

        async def more_samples(seconds):
            script.now += seconds
            cursor = len(script.history) + 1
            script.history.append(_event(cursor, 'run-1', cursor, 'sample'))

        script.sleep = more_samples
        ended = {'reason': 'user_stop', 'ok': True, 'samples': 3, 'path': 'alice/x.csv'}
        waited, stops = _wait_then_stop(script, 'samples:2', ends_with=ended)
        # One sample in the history, a second after one poll: stopped then,
        # at 0.1 s, the poll a wait that stops uses.
        assert stops == [pytest.approx(0.1)]
        assert (waited['fired'], waited['stopped'], waited['samples']) == ('samples:2', True, 2)
        assert waited['status'] == idle
        assert waited['run_ended'] == ended
        assert 'stop_note' not in waited

    def test_a_run_slow_to_end_is_said_to_be_still_ending(self):
        stopping = {**RUNNING, 'state': 'stopping'}
        script = Script([{**RUNNING, 'state': 'paused'}, stopping])
        waited, stops = _wait_then_stop(script, 'state:paused', timeout_s=0.3)
        assert stops == [0.0]
        assert (waited['fired'], waited['stopped']) == ('state:paused', True)
        assert waited['status'] == stopping
        assert 'wait_for run_ended' in waited['stop_note']
        assert waited['waited_s'] == pytest.approx(0.3)

    def test_a_prompt_or_a_timeout_does_not_stop_the_run(self):
        prompted = {**RUNNING, 'state': 'awaiting_prompt', 'pending_prompt': PROMPT}
        waited, stops = _wait_then_stop(Script([prompted]), 'samples:5')
        assert (waited['fired'], stops) == ('prompt', [])
        waited, stops = _wait_then_stop(Script([RUNNING]), 'state:paused', timeout_s=0.2)
        assert (waited['fired'], stops) == ('timeout', [])
        assert 'stopped' not in waited

    def test_a_run_already_over_is_not_stopped(self):
        waited, stops = _wait_then_stop(Script([RUNNING, IDLE], _run_events()), 'run_ended')
        assert (waited['fired'], stops) == ('run_ended', [])

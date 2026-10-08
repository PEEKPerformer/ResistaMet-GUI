"""Following a run without spinning: ``wait_for`` and the event digest.

An agent that polls ``get_status`` in a loop spends its context on
identical replies. ``wait_for`` polls ``GET /session`` here instead, at a
modest interval, and returns once: when the condition it was given holds,
when a new prompt stops the run (only a person can move it on, and the
agent should tell the user), when the run is over (nothing more will
happen), or at the timeout, whichever comes first. A prompt the agent
has already been shown does not end it: waiting for the person to answer
it is what the wait is for.

"Already shown" is remembered by the MCP server process, and a client that
starts a new server for every call, or reconnects, forgets it: there every
``wait_for('run_ended')`` at a prompt came back at once with that same
prompt. Two forms need no memory. ``prompt_answered`` waits for whatever
prompt is pending when the call begins to stop pending; ``ignore_prompt_id``
names the prompt a wait should go through.

``get_run_events`` reads the same history the backend keeps for polling
clients (``GET /session/events``). Samples are left out unless asked for,
and then thinned to at most 200 spread over the run: the data file holds
every row, and ``get_run_summary`` reads it.

The backend is reached through ``get(path, params)``, so this module knows
nothing of HTTP and is tested with a script of replies.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

import anyio

#: No single wait is longer: an MCP client gives a tool call only so long,
#: and an agent should look up now and then rather than block for an hour.
MAX_WAIT_S = 120.0
POLL_S = 0.5
#: How often a wait that will stop the run looks. A stop that comes half a
#: second late is five readings too many at 10 Hz; at 0.1 s it is about one.
POLL_S_THEN_STOP = 0.1
#: Samples returned by one get_run_events, at most.
MAX_SAMPLES = 200
#: Other events returned by one get_run_events, at most; the rest follow
#: from the returned ``last_seq``.
MAX_EVENTS = 300
#: Events asked for per request, and in all, when reading the history.
PAGE = 5000
MAX_READ = 20000

STATES = ('idle', 'identifying', 'running', 'paused', 'awaiting_prompt', 'stopping')

Get = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class Until:
    """A condition ``wait_for`` was asked to wait for."""

    text: str
    kind: str
    samples: int = 0
    state: str = ''


def parse_until(text: str) -> Until:
    """``run_ended``, ``prompt``, ``prompt_answered``, ``samples:N`` or ``state:<state>``.

    Raises ValueError.
    """
    text = (text or '').strip()
    if text in ('run_ended', 'prompt', 'prompt_answered'):
        return Until(text, text)
    kind, _, value = text.partition(':')
    if kind == 'samples':
        try:
            count = int(value)
        except ValueError:
            count = -1
        if count < 1:
            raise ValueError(f"'{text}': samples:N needs a whole number N of at least 1")
        return Until(text, 'samples', samples=count)
    if kind == 'state':
        if value not in STATES:
            raise ValueError(f"'{text}': the states are {', '.join(STATES)}")
        return Until(text, 'state', state=value)
    raise ValueError(f"'{text}': wait for run_ended, prompt, prompt_answered, samples:N "
                     "or state:<state>")


@dataclass
class SampleCount:
    """A run's samples, counted from the event history a page at a time."""

    run_id: str
    count: int = 0
    #: The history no longer reached back to the run's start: the count is short.
    gap: bool = False
    _cursor: Optional[int] = field(default=None, repr=False)

    async def update(self, get: Get) -> None:
        events, gap, self._cursor = await read_history(get, self.run_id, 0, self._cursor)
        self.gap = self.gap or gap
        self.count += sum(1 for event in events if event.get('type') == 'sample')


async def read_history(get: Get, run_id: str, since_seq: int,
                       cursor: Optional[int] = None):
    """The run's events after ``since_seq`` (or after ``cursor``), oldest first.

    Returns ``(events, gap, cursor)``. Pages through the history by its
    cursor, which counts across runs, and keeps only this run's events: a
    reply for one run also carries every run after it.
    """
    events: List[Dict[str, Any]] = []
    gap = False
    read = 0
    while read < MAX_READ:
        params: Dict[str, Any] = {'run_id': run_id, 'limit': PAGE}
        if cursor is None:
            params['since_seq'] = since_seq
        else:
            params['since_cursor'] = cursor
        reply = await get('/session/events', params=params)
        page = reply.get('events', [])
        if cursor is None:
            gap = bool(reply.get('gap'))
        cursor = reply.get('cursor', cursor)
        events.extend(event for event in page if event.get('run_id') == run_id)
        read += len(page)
        if len(page) < PAGE:
            break
    return events, gap, cursor


def _ended(status: Dict[str, Any], run_id: Optional[str]) -> bool:
    return status.get('state') == 'idle' or status.get('run_id') != run_id


def _prompt_id(status: Dict[str, Any]) -> Optional[str]:
    prompt = status.get('pending_prompt')
    return prompt.get('prompt_id') if isinstance(prompt, dict) else None


def _met(until: Until, status: Dict[str, Any], run_id: Optional[str],
         samples: Optional[SampleCount], waited_through: Optional[str]) -> bool:
    if until.kind == 'run_ended':
        return _ended(status, run_id)
    if until.kind == 'prompt':
        return _prompt_id(status) not in (None, waited_through)
    if until.kind == 'prompt_answered':
        return (waited_through is None or _ended(status, run_id)
                or _prompt_id(status) != waited_through)
    if until.kind == 'state':
        return status.get('state') == until.state
    return samples is not None and samples.count >= until.samples


async def wait_for(get: Get, until: Until, timeout_s: float, *,
                   stop: Optional[Callable[[], Awaitable[Any]]] = None,
                   known_prompt: Optional[str] = None,
                   ignore_prompt: Optional[str] = None,
                   poll_s: Optional[float] = None,
                   clock: Callable[[], float] = time.monotonic,
                   sleep: Callable[[float], Awaitable[None]] = anyio.sleep) -> Dict[str, Any]:
    """Wait for ``until`` on the current run; say what ended the wait.

    ``fired`` is the condition asked for when it held; otherwise ``prompt``
    (a prompt the agent has not been shown is pending, and the run cannot
    go on until a person answers it), ``run_ended`` (no run is in progress, so nothing
    more will happen) or ``timeout``. Waiting when no run is in progress
    returns at once.

    ``known_prompt`` is the id of the prompt the agent has been shown
    pending. If that prompt is pending when the wait begins, it ends the
    wait only when ``until`` is ``prompt``. Otherwise an agent told "a
    person must answer this; tell the user, then wait" got the same prompt
    back at once, every time it waited, and could not wait for the person
    at all. Waiting through it is how an agent waits for the person: the
    run moving on shows as the condition (``state:running``), the next
    prompt, or the run's end, and ``prompt_at_start`` says whether that
    prompt is still pending. Any other prompt ends the wait at once,
    pending at the start or not: the agent has not heard of it.

    ``ignore_prompt`` does the same as ``known_prompt``, said by the agent
    instead of remembered for it, so it holds across server processes;
    with ``until`` ``prompt`` it makes the wait one for the next prompt.

    ``prompt_answered`` needs neither. It holds once the prompt pending
    when the wait began is pending no longer: answered, released by a stop,
    or gone with the run. The status then says what came next (the state,
    and the next prompt if one is already up). With no prompt pending at
    the start it holds at once. Given ``ignore_prompt`` too, it is the
    prompt the agent expects to be waiting on: a different one pending at
    the start ends the wait at once as ``prompt``, because the person
    answered the first before the wait began and nobody has been told of
    the second.

    With ``stop``, the run is stopped as soon as the condition holds, in
    this same call, and the wait goes on, within the same timeout, until
    the run has ended; ``stopped`` says so. Two calls (wait, then stop)
    leave a whole round trip of the agent's between them, in which a run
    asked for 20 samples took 57. Here the gap is one poll, made short for
    the purpose (``POLL_S_THEN_STOP``), and the reading in flight when the
    stop arrives. Only the condition asked for stops the run: a prompt, a
    run that ended or a timeout returns as without ``stop``.
    """
    timeout_s = max(0.0, min(float(timeout_s), MAX_WAIT_S))
    if poll_s is None:
        poll_s = POLL_S if stop is None else POLL_S_THEN_STOP
    started = clock()
    status = await get('/session')
    run_id = status.get('run_id')
    pending_at_start = _prompt_id(status)
    # The prompt this wait goes through. A remembered one does not stop
    # wait_for('prompt') from returning at once: that is how an agent asks
    # what is pending.
    waived = (ignore_prompt,) if until.kind == 'prompt' else (known_prompt, ignore_prompt)
    if until.kind == 'prompt_answered':
        prompt_at_start = pending_at_start
    elif pending_at_start is not None and pending_at_start in waived:
        prompt_at_start = pending_at_start
    else:
        prompt_at_start = None
    samples = SampleCount(run_id) if until.kind == 'samples' and run_id else None
    stopped = False
    # A prompt the agent did not expect, pending before it began to wait.
    unexpected = (until.kind == 'prompt_answered' and ignore_prompt is not None
                  and pending_at_start not in (None, ignore_prompt))
    if unexpected:
        fired = 'prompt'
        prompt_at_start = None
    while not unexpected:
        if stopped:
            if _ended(status, run_id):
                break
        else:
            if samples is not None:
                await samples.update(get)
            if _met(until, status, run_id, samples, prompt_at_start):
                fired = until.text
                if stop is None or _ended(status, run_id):
                    break
                await stop()
                stopped = True
                status = await get('/session')
                continue
            pending = _prompt_id(status)
            if pending is not None and pending != prompt_at_start:
                fired = 'prompt'
                break
            if _ended(status, run_id):
                fired = 'run_ended'
                break
        left = timeout_s - (clock() - started)
        if left <= 0:
            if not stopped:
                fired = 'timeout'
            break
        await sleep(min(poll_s, left))
        status = await get('/session')

    waited: Dict[str, Any] = {
        'fired': fired,
        'until': until.text,
        'waited_s': round(clock() - started, 2),
        'status': status,
    }
    if prompt_at_start is not None:
        waited['prompt_at_start'] = {
            'prompt_id': prompt_at_start,
            'still_pending': _prompt_id(status) == prompt_at_start,
        }
    if samples is not None:
        waited['samples'] = samples.count
        if samples.gap:
            waited['samples_note'] = ("the backend's history no longer reaches the run's "
                                      "start, so this count is short")
    if stopped:
        waited['stopped'] = True
        if not _ended(status, run_id):
            waited['stop_note'] = ("the run was told to stop and had not ended within the "
                                   "timeout; wait_for run_ended to see it end")
    if run_id and _ended(status, run_id):
        waited['run_ended'] = await run_ended_payload(get, run_id, status)
    return waited


async def run_ended_payload(get: Get, run_id: str,
                            status: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """How the run ended (reason, ok, samples, path), from its last event."""
    since = 0
    if status.get('run_id') == run_id:
        # run_ended is the run's last event, and last_seq is that run's.
        since = max(0, int(status.get('last_seq') or 0) - 1)
    events, _, _ = await read_history(get, run_id, since)
    for event in reversed(events):
        if event.get('type') == 'run_ended':
            return event.get('payload')
    return None


def thin(items: Sequence[Any], most: int) -> List[Any]:
    """At most ``most`` of ``items``, evenly spread, the first and last kept."""
    count = len(items)
    if count <= most:
        return list(items)
    if most == 1:
        return [items[-1]]
    step = (count - 1) / (most - 1)
    return [items[round(index * step)] for index in range(most)]


def _compact(event: Dict[str, Any]) -> Dict[str, Any]:
    payload = event.get('payload') or {}
    if event.get('type') == 'sample':
        payload = {key: value for key, value in payload.items()
                   if key != 't_unix' and value not in (None, '')}
    return {'seq': event.get('seq'), 'type': event.get('type'), 't': event.get('t'),
            'payload': payload}


def digest(events: Sequence[Dict[str, Any]], *, types: Optional[Sequence[str]] = None,
           include_samples: bool = False, max_samples: int = MAX_SAMPLES,
           max_events: int = MAX_EVENTS) -> Dict[str, Any]:
    """The events an agent should see, from one run's events in order.

    Without ``types``: everything but samples and the twice-a-second progress
    logs. Samples join when asked for (or named in ``types``), thinned to
    ``max_samples``; ``samples_total`` counts them either way. Past
    ``max_events`` other events the digest stops, and ``last_seq`` says where
    to continue from.
    """
    wanted = set(types) if types else None
    with_samples = include_samples or (wanted is not None and 'sample' in wanted)
    max_samples = max(1, min(int(max_samples), MAX_SAMPLES))
    kept: List[Dict[str, Any]] = []
    sample_events: List[Dict[str, Any]] = []
    others = 0
    last_seq = events[-1].get('seq', 0) if events else None
    more = False
    for event in events:
        kind = event.get('type')
        if kind == 'sample':
            sample_events.append(event)
            continue
        if wanted is not None and kind not in wanted:
            continue
        if wanted is None and kind == 'log' and \
                (event.get('payload') or {}).get('code') == 'progress':
            continue
        if others == max_events:
            more = True
            last_seq = kept[-1].get('seq')
            break
        kept.append(event)
        others += 1
    if more:
        sample_events = [event for event in sample_events if event.get('seq', 0) <= last_seq]
    chosen = thin(sample_events, max_samples) if with_samples else []
    merged = sorted(kept + chosen, key=lambda event: event.get('seq', 0))
    return {
        'events': [_compact(event) for event in merged],
        'last_seq': last_seq,
        'more': more,
        'samples_total': len(sample_events),
        'samples_returned': len(chosen),
    }

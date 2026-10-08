"""The MCP tools against a scripted backend, through the SDK's in-memory client.

What is checked here is the translation: arguments into requests, replies
into compact results, refusals into tool errors that keep the backend's
detail. The backend's own rules are tested against the real API in
``test_mcp_e2e.py``.
"""
import asyncio
import json
import os

import pytest

pytest.importorskip("mcp")
httpx2 = pytest.importorskip("httpx2")

from mcp import Client  # noqa: E402
from mcp.types import Implementation  # noqa: E402

from resistamet_gui.mcp.audit import AuditLog  # noqa: E402
from resistamet_gui.mcp.client import NOT_RUNNING, Backend  # noqa: E402
from resistamet_gui.mcp.server import build_server  # noqa: E402
from resistamet_gui.mcp.tools import VDP_PROMPTS  # noqa: E402

PENDING = {'prompt_id': 'run-2:vdp_geometry-1', 'kind': 'vdp_geometry',
           'options': ['proceed', 'abort'], 'requires_human': True, 'detail': {'index': 1}}

SCHEMA = {'modes': {'resistance': {
    'model': 'ResistanceSettings',
    'fields': ['res_test_current', 'res_voltage_compliance'],
    'override_keys': ['res_test_current', 'res_voltage_compliance', 'sampling_rate'],
    'keys': {
        'res_test_current': {'type': 'number', 'minimum': 1e-7, 'maximum': 3.0,
                             'default': 0.001, 'unit': 'A'},
        'res_voltage_compliance': {'type': 'number', 'minimum': 0.1, 'maximum': 200.0,
                                   'default': 5.0, 'unit': 'V'},
        'sampling_rate': {'type': 'number', 'minimum': 0.1, 'maximum': 100.0,
                          'default': 10.0, 'unit': 'Hz'}},
    'fixed': {}}}}


class ScriptedBackend:
    """Replies by method and path; records every request."""

    def __init__(self):
        self.replies = {
            ('GET', '/health'): (200, {'status': 'ok'}),
            ('GET', '/session'): (200, {'state': 'idle', 'run_id': None,
                                        'pending_prompt': None}),
            ('GET', '/schema/settings'): (200, SCHEMA),
            ('GET', '/users'): (200, {'users': ['alice'], 'last_user': 'alice'}),
        }
        self.requests = []

    def __call__(self, request):
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, dict(request.url.params), body,
                              request.url.raw_path.decode()))
        status, reply = self.replies.get((request.method, request.url.path),
                                         (404, {'detail': 'Not Found'}))
        if callable(reply):
            reply = reply(body)
        if isinstance(reply, str):
            return httpx2.Response(status, text=reply)
        return httpx2.Response(status, json=reply)


@pytest.fixture
def scripted():
    return ScriptedBackend()


@pytest.fixture
def connection_file(tmp_path):
    path = tmp_path / 'connection.json'
    path.write_text(json.dumps({'url': 'http://127.0.0.1:5000', 'agent_token': 'tok',
                                'pid': os.getpid()}), encoding='utf-8')
    return path


@pytest.fixture
def calls(scripted, connection_file, tmp_path):
    """Call tools in turn through one fresh in-memory MCP session.

    Takes ``(name, arguments)`` pairs; returns ``(is_error, payload)`` for each.
    """
    def run(*steps):
        async def go():
            backend = Backend(str(connection_file), transport=httpx2.MockTransport(scripted))
            server = build_server(backend, AuditLog(str(tmp_path / 'audit')))
            replies = []
            try:
                async with Client(server, mode='legacy',
                                  client_info=Implementation(name='test', version='1')) as client:
                    for name, arguments in steps:
                        reply = await client.call_tool(name, arguments or {})
                        text = reply.content[0].text
                        replies.append((reply.is_error,
                                        text if reply.is_error else json.loads(text)))
            finally:
                await backend.aclose()
            return replies
        return asyncio.run(go())
    return run


@pytest.fixture
def call(calls):
    """Call one tool through a fresh in-memory MCP session; (is_error, payload)."""
    def run(name, arguments=None):
        return calls((name, arguments))[0]
    return run


def _list_tools(connection_file):
    async def go():
        backend = Backend(str(connection_file))
        server = build_server(backend, AuditLog(os.devnull))
        try:
            async with Client(server, mode='legacy') as client:
                return (await client.list_tools()).tools
        finally:
            await backend.aclose()
    return {tool.name: tool for tool in asyncio.run(go())}


class TestTheToolList:
    def test_the_read_tools_are_read_only(self, connection_file):
        tools = _list_tools(connection_file)
        for name in ('get_status', 'list_instruments', 'identify_instrument', 'list_users',
                     'get_profile', 'describe_mode', 'check_settings'):
            assert tools[name].annotations.read_only_hint is True, name

    def test_nothing_answers_a_prompt_or_talks_scpi_or_shuts_down(self, connection_file):
        names = set(_list_tools(connection_file))
        assert not {name for name in names
                    if 'prompt' in name or 'scpi' in name or 'shutdown' in name
                    or 'write' in name or 'query' in name}

    def test_check_settings_says_it_comes_first_and_what_limits_are(self, connection_file):
        description = _list_tools(connection_file)['check_settings'].description
        assert 'before every start_run' in description
        assert 'only a person can change' in description


class TestReads:
    def test_get_status_says_who_answers_a_prompt(self, call, scripted):
        scripted.replies[('GET', '/session')] = (200, {'state': 'awaiting_prompt',
                                                       'run_id': 'run-2',
                                                       'pending_prompt': PENDING})
        failed, status = call('get_status')
        assert not failed
        assert status['backend'] == 'ok'
        assert status['pending_prompt']['kind'] == 'vdp_geometry'
        assert 'person must answer' in status['pending_prompt']['who_answers']

    def test_get_profile_leaves_out_the_machine_s_agent_switch(self, call, scripted):
        """Stored false while --allow-agents let this agent in: it read as a refusal."""
        scripted.replies[('GET', '/profiles/alice')] = (200, {
            'measurement': {'gpib_address': 'GPIB0::24::INSTR', 'allow_agents': False,
                            'res_test_current': 0.001},
            'agent_limits': {'max_voltage_v': 30.0}})
        failed, profile = call('get_profile', {'user': 'alice'})
        assert not failed
        assert profile == {'measurement': {'gpib_address': 'GPIB0::24::INSTR',
                                           'res_test_current': 0.001},
                           'agent_limits': {'max_voltage_v': 30.0}}

    def test_list_instruments_says_where_a_run_s_address_comes_from(self, connection_file):
        description = _list_tools(connection_file)['list_instruments'].description
        assert "profile's gpib_address" in description
        assert "overrides cannot name an address" in description

    def test_a_user_name_is_one_path_segment(self, call, scripted):
        call('get_profile', {'user': 'a/b c'})
        assert scripted.requests[-1][4] == '/profiles/a%2Fb%20c'

    def test_identify_sends_the_address(self, call, scripted):
        scripted.replies[('POST', '/instruments/identify')] = (200, {'model': '2420'})
        failed, reply = call('identify_instrument', {'address': 'GPIB0::24::INSTR'})
        assert (failed, reply) == (False, {'model': '2420'})
        assert scripted.requests[-1][3] == {'address': 'GPIB0::24::INSTR'}

    def test_describe_mode_gives_a_line_per_key_with_the_user_s_values(self, call, scripted):
        scripted.replies[('POST', '/settings/resolve')] = (200, {
            'ok': True, 'issues': [],
            'settings': {'measurement': {'res_test_current': 0.002,
                                         'res_voltage_compliance': 5.0,
                                         'sampling_rate': 10.0, 'vsource_voltage': 1.0}}})
        failed, described = call('describe_mode', {'mode': 'resistance'})
        assert not failed
        assert described['mode_keys'] == {
            'res_test_current': '0.002 A, from the profile; default 0.001; 1e-07 <= x <= 3',
            'res_voltage_compliance': '5.0 V, from the profile (the default); '
                                      '0.1 <= x <= 200',
        }
        assert described['shared_keys'] == {
            'sampling_rate': '10.0 Hz, from the profile (the default); 0.1 <= x <= 100'}
        assert (described['user'], described['issues']) == ('alice', [])
        assert 'fixed by the mode' in described['how_to_read']
        # Nothing is fixed in this mode, so the stored profile is not asked for.
        assert not [r for r in scripted.requests if r[1].startswith('/profiles/')]

    def test_describe_mode_says_which_values_the_mode_fixes(self, call, scripted):
        scripted.replies[('GET', '/schema/settings')] = (200, {'modes': {'vdp': {
            'model': 'VdpSettings', 'fields': ['vdp_current'],
            'override_keys': ['auto_zero', 'vdp_current'], 'fixed': {'auto_zero': 'on'},
            'keys': {'auto_zero': {'type': 'string', 'enum': ['on', 'once', 'off'],
                                   'default': 'once'},
                     'vdp_current': {'type': 'number', 'default': 0.001, 'unit': 'A'}}}}})
        scripted.replies[('POST', '/settings/resolve')] = (200, {
            'ok': True, 'issues': [],
            'settings': {'measurement': {'auto_zero': 'on', 'vdp_current': 0.001}}})
        scripted.replies[('GET', '/profiles/alice')] = (200, {
            'measurement': {'auto_zero': 'once', 'vdp_current': 0.001}})
        failed, described = call('describe_mode', {'mode': 'vdp'})
        assert not failed
        assert described['shared_keys'] == {
            'auto_zero': '"on", fixed by the mode (the profile\'s "once" is not used)'}
        assert described['prompts'] == VDP_PROMPTS
        # An older backend's schema has no wiring: nothing is made up.
        assert 'wiring' not in described and 'prompt_timeout_s' not in described

    def test_describe_mode_passes_on_the_van_der_pauw_wiring(self, call, scripted):
        wiring = [{'index': 0, 'name': 'Geometry 1 of 4', 'force_hi': 'C2',
                   'force_lo': 'C1', 'sense_hi': 'C3', 'sense_lo': 'C4',
                   'message': 'Geometry 1 of 4: connect ...'}]
        timeout = {'default': 900.0, 'maximum': 86400.0}
        scripted.replies[('GET', '/schema/settings')] = (200, {'modes': {'vdp': {
            'model': 'VdpSettings', 'fields': [], 'override_keys': [], 'fixed': {},
            'keys': {}, 'wiring': wiring}}, 'prompt_timeout_s': timeout})
        scripted.replies[('POST', '/settings/resolve')] = (200, {
            'ok': True, 'issues': [], 'settings': {'measurement': {}}})
        failed, described = call('describe_mode', {'mode': 'vdp'})
        assert not failed
        assert (described['wiring'], described['prompt_timeout_s']) == (wiring, timeout)

    def test_the_vdp_prompts_are_the_run_s(self, connection_file):
        """Four rewiring prompts, as many as the F76 geometries the run walks."""
        from resistamet_gui.calculations_vdp import f76_geometries
        assert len(f76_geometries()) == 4
        assert VDP_PROMPTS.startswith('A van der Pauw run stops at four prompts')
        assert 'the output is off while it waits' in VDP_PROMPTS
        assert 'touch-safety prompt (safety_voltage_ack) comes first' in VDP_PROMPTS
        assert 'A person must be at the bench for the whole run' in VDP_PROMPTS
        description = _list_tools(connection_file)['start_run'].description
        assert 'four more, one before each of its four wirings' in description
        assert 'someone must be at the bench for the whole run' in description

    def test_an_unknown_mode_names_the_modes(self, call):
        failed, text = call('describe_mode', {'mode': 'hall'})
        assert failed
        assert "unknown mode 'hall'" in text and 'resistance' in text


class TestCheckSettings:
    def _resolve(self, scripted, **reply):
        base = {'ok': True, 'issues': [], 'derived': {'max_rate_hz': 50.0}, 'hazard': None,
                'agent_limits': {'ok': True, 'violations': []},
                'settings': {'measurement': {'res_test_current': 0.002,
                                             'res_voltage_compliance': 5.0,
                                             'sampling_rate': 10.0, 'nplc': 1.0}}}
        base.update(reply)
        scripted.replies[('POST', '/settings/resolve')] = (200, base)

    def test_within_limits_the_agent_may_start(self, call, scripted):
        self._resolve(scripted)
        failed, checked = call('check_settings', {'user': 'alice', 'mode': 'resistance',
                                                  'overrides': {'res_test_current': 0.002}})
        assert not failed
        assert checked['can_start'] is True
        assert next(iter(checked)) == 'can_start'
        assert checked['settings'] == {'res_test_current': 0.002,
                                       'res_voltage_compliance': 5.0, 'sampling_rate': 10.0}
        assert scripted.requests[-1][3] == {'mode': 'resistance', 'username': 'alice',
                                            'overrides': {'res_test_current': 0.002}}

    def test_over_a_limit_the_violation_reaches_the_agent(self, call, scripted):
        violation = {'limit': 'max_voltage_v', 'source': 'agent_limits', 'model': None,
                     'keys': ['vsource_voltage'], 'value': 40.0, 'allowed': 30.0,
                     'message': 'voltage 40 V (vsource_voltage) is above the agent limit'}
        self._resolve(scripted, agent_limits={'ok': False, 'violations': [violation]},
                      hazard={'hazardous': True, 'voltage_v': 40.0, 'threshold_v': 30.0,
                              'reason': 'Source V'})
        failed, checked = call('check_settings', {'user': 'alice', 'mode': 'resistance'})
        assert not failed
        # Valid settings an agent may not start: ok alone would read as leave.
        assert (checked['can_start'], checked['ok']) == (False, True)
        assert checked['agent_limits']['violations'] == [violation]
        assert 'person at the' in checked['note']

    def test_what_the_run_will_warn_about_reaches_the_agent(self, call, scripted):
        warning = {'keys': ['sampling_rate', 'nplc'],
                   'message': '10 Hz is more than these timing settings can deliver '
                              '(about 4.8 Hz); the run will sample as fast as it can.'}
        self._resolve(scripted, warnings=[warning])
        failed, checked = call('check_settings', {'user': 'alice', 'mode': 'resistance'})
        assert not failed
        assert checked['warnings'] == [warning]
        assert checked['can_start'] is True

    def test_settings_with_errors_cannot_be_started(self, call, scripted):
        self._resolve(scripted, ok=False, agent_limits=None,
                      issues=[{'key': 'res_test_current', 'message': 'too large',
                               'severity': 'error'}])
        failed, checked = call('check_settings', {'user': 'alice', 'mode': 'resistance'})
        assert checked['can_start'] is False
        assert checked['issues'][0]['key'] == 'res_test_current'


class TestRefusals:
    def test_without_a_backend_the_agent_is_told_how_to_get_one(self, call, connection_file):
        connection_file.unlink()
        failed, text = call('get_status')
        assert failed
        assert NOT_RUNNING in text

    def test_a_403_says_a_person_must_do_it(self, call, scripted):
        scripted.replies[('GET', '/users')] = (403, {'detail': 'this needs a human'})
        failed, text = call('list_users')
        assert failed
        assert 'Only a person at the ResistaMet window' in text
        assert 'this needs a human' in text

    def test_a_409_is_busy_with_the_backend_s_words(self, call, scripted):
        scripted.replies[('GET', '/instruments/resources')] = (409, {'detail': 'session is running'})
        failed, text = call('list_instruments')
        assert failed and 'busy' in text and 'session is running' in text


class TestRunTools:
    def test_the_run_tools_act_and_say_so(self, connection_file):
        tools = _list_tools(connection_file)
        for name in ('start_run', 'stop_run', 'abort_run', 'pause_run', 'resume_run',
                     'mark_event'):
            annotations = tools[name].annotations
            assert annotations.read_only_hint is False, name
            assert annotations.destructive_hint is False, name
        assert tools['get_run_events'].annotations.read_only_hint is True
        # wait_for may stop the run (then_stop), so it does not claim to only read.
        assert tools['wait_for'].annotations.read_only_hint is False
        assert tools['wait_for'].annotations.destructive_hint is False
        assert 'then_stop' in tools['wait_for'].description

    def test_start_sends_the_request_and_names_this_server_as_the_client(
            self, call, scripted, tmp_path):
        scripted.replies[('POST', '/session/start')] = (202, {'run_id': 'run-4'})
        scripted.replies[('GET', '/session')] = (200, {'state': 'running', 'run_id': 'run-4',
                                                       'pending_prompt': None})
        failed, started = call('start_run', {'user': 'alice', 'mode': 'resistance',
                                             'sample_name': 'wafer 1',
                                             'overrides': {'res_test_current': 0.002}})
        assert not failed
        assert started == {'run_id': 'run-4', 'status': {'state': 'running',
                                                         'run_id': 'run-4',
                                                         'pending_prompt': None}}
        body = next(r[3] for r in scripted.requests if r[1] == '/session/start')
        assert body['mode'] == 'resistance' and body['username'] == 'alice'
        assert body['sample_name'] == 'wafer 1'
        assert body['overrides'] == {'res_test_current': 0.002}
        assert body['client']['name'] == 'resistamet-mcp'
        assert 'spot' not in body and 'prompt_timeout_s' not in body
        [line] = [json.loads(text) for path in (tmp_path / 'audit').iterdir()
                  for text in path.read_text(encoding='utf-8').splitlines()]
        assert (line['run_id'], line['http_status']) == ('run-4', 202)

    def test_a_start_beyond_the_limits_carries_each_violation(self, call, scripted):
        detail = {'message': 'beyond the agent limits: voltage 40 V (vsource_voltage) is '
                             'above the agent limit max_voltage_v = 30 V',
                  'violations': [{'limit': 'max_voltage_v', 'source': 'agent_limits',
                                  'model': None, 'keys': ['vsource_voltage'], 'value': 40.0,
                                  'allowed': 30.0, 'message': 'voltage 40 V ...'}]}
        scripted.replies[('POST', '/session/start')] = (422, {'detail': detail})
        failed, text = call('start_run', {'user': 'alice', 'mode': 'source_v',
                                          'sample_name': 's'})
        assert failed
        assert json.dumps(detail, separators=(',', ':')) in text
        assert 'only a person can change' in text

    def test_stop_returns_the_status(self, call, scripted):
        scripted.replies[('POST', '/session/stop')] = (200, {'state': 'stopping',
                                                             'run_id': 'run-4',
                                                             'pending_prompt': None})
        assert call('stop_run') == (False, {'state': 'stopping', 'run_id': 'run-4',
                                            'pending_prompt': None})

    def test_a_mark_sends_its_label(self, call, scripted):
        scripted.replies[('POST', '/session/mark')] = (200, {'state': 'running'})
        call('mark_event', {'label': 'lamp on'})
        assert scripted.requests[-1][3] == {'label': 'lamp on'}

    def test_wait_for_refuses_a_condition_it_does_not_know(self, call):
        failed, text = call('wait_for', {'until': 'done'})
        assert failed and 'run_ended, prompt, prompt_answered, samples:N' in text

    def test_wait_for_reports_a_prompt_and_who_answers_it(self, call, scripted):
        scripted.replies[('GET', '/session')] = (200, {'state': 'awaiting_prompt',
                                                       'run_id': 'run-2', 'last_seq': 3,
                                                       'pending_prompt': PENDING})
        scripted.replies[('GET', '/session/events')] = (200, {'events': [], 'gap': False,
                                                              'last_seq': 0, 'cursor': 0})
        failed, waited = call('wait_for', {'until': 'samples:5', 'timeout_s': 1})
        assert not failed
        assert waited['fired'] == 'prompt'
        assert 'person must answer' in waited['status']['pending_prompt']['who_answers']

    def test_wait_for_waits_through_a_prompt_the_agent_was_shown(self, calls, scripted):
        prompted = {'state': 'awaiting_prompt', 'run_id': 'run-2', 'last_seq': 3,
                    'pending_prompt': PENDING}
        scripted.replies[('GET', '/session')] = (200, prompted)
        (_, status), (failed, waited) = calls(
            ('get_status', None), ('wait_for', {'until': 'run_ended', 'timeout_s': 0.6}))
        assert status['pending_prompt']['prompt_id'] == PENDING['prompt_id']
        assert not failed
        assert waited['fired'] == 'timeout'
        assert waited['prompt_at_start'] == {'prompt_id': PENDING['prompt_id'],
                                             'still_pending': True}

    def test_who_answers_says_how_to_wait_for_the_person(self, call, scripted):
        scripted.replies[('GET', '/session')] = (200, {'state': 'awaiting_prompt',
                                                       'run_id': 'run-2',
                                                       'pending_prompt': PENDING})
        who = call('get_status')[1]['pending_prompt']['who_answers']
        # The stateless form, with this prompt's id filled in: it works from
        # a server process that never showed the prompt.
        assert ("wait_for('prompt_answered', ignore_prompt_id='run-2:vdp_geometry-1')"
                in who)

    def test_wait_for_passes_ignore_prompt_id_through(self, call, scripted):
        # A fresh server remembers nothing; the id it is given is enough.
        prompted = {'state': 'awaiting_prompt', 'run_id': 'run-2', 'last_seq': 3,
                    'pending_prompt': PENDING}
        scripted.replies[('GET', '/session')] = (200, prompted)
        failed, waited = call('wait_for', {'until': 'run_ended', 'timeout_s': 0.6,
                                           'ignore_prompt_id': PENDING['prompt_id']})
        assert not failed
        assert waited['fired'] == 'timeout'
        assert waited['prompt_at_start'] == {'prompt_id': PENDING['prompt_id'],
                                             'still_pending': True}

    def test_wait_for_prompt_answered_returns_what_came_next(self, call, scripted):
        prompted = {'state': 'awaiting_prompt', 'run_id': 'run-2', 'last_seq': 3,
                    'pending_prompt': PENDING}
        running = {'state': 'running', 'run_id': 'run-2', 'last_seq': 4,
                   'pending_prompt': None}
        statuses = [prompted, prompted, running]
        scripted.replies[('GET', '/session')] = (
            200, lambda body: statuses.pop(0) if len(statuses) > 1 else statuses[0])
        failed, waited = call('wait_for', {'until': 'prompt_answered', 'timeout_s': 5})
        assert not failed
        assert waited['fired'] == 'prompt_answered'
        assert waited['status']['state'] == 'running'
        assert waited['prompt_at_start'] == {'prompt_id': PENDING['prompt_id'],
                                             'still_pending': False}

    def test_wait_for_then_stop_stops_the_run_and_follows_it_to_its_end(self, call, scripted):
        statuses = [{'state': 'running', 'run_id': 'run-4', 'last_seq': 3,
                     'pending_prompt': None},
                    {'state': 'idle', 'run_id': 'run-4', 'last_seq': 4,
                     'pending_prompt': None}]
        stops = []
        scripted.replies[('GET', '/session')] = (
            200, lambda body: statuses[0] if not stops else statuses[1])
        scripted.replies[('POST', '/session/stop')] = (
            200, lambda body: stops.append(1) or {**statuses[0], 'state': 'stopping'})
        ended = {'reason': 'user_stop', 'ok': True, 'samples': 7, 'path': 'alice/r.csv'}
        scripted.replies[('GET', '/session/events')] = (200, {
            'gap': False, 'cursor': 4, 'last_seq': 4, 'events': [
                {'type': 'run_ended', 'run_id': 'run-4', 'seq': 4, 'cursor': 4,
                 'payload': ended}]})
        failed, waited = call('wait_for', {'until': 'state:running', 'then_stop': True,
                                           'timeout_s': 5})
        assert not failed
        assert stops == [1]
        assert (waited['fired'], waited['stopped']) == ('state:running', True)
        assert waited['run_ended'] == ended
        assert waited['status']['state'] == 'idle'

    def test_get_run_summary_of_the_first_rows(self, call, scripted):
        scripted.replies[('GET', '/results')] = (200, LISTING)
        scripted.replies[('GET', '/results/file')] = (200, RUN_FILE)
        failed, summarised = call('get_run_summary', {'path': 'alice/1_s1_R.csv',
                                                      'first_rows': 1})
        assert not failed
        assert summarised['columns']['R_ohm']['count'] == 1
        assert (summarised['rows'], summarised['rows_total']) == (1, 2)

    def test_events_with_no_run_yet_are_none(self, call):
        assert call('get_run_events') == (False, {'run_id': None, 'events': [],
                                                  'last_seq': 0})


RUN_FILE = """\
# mode: resistance
# units: s,Ω,,
elapsed_s,R_ohm,compliance,event
0.1,100,OK,
0.2,102,OK,
# --- run completed ---
# total_samples: 2
"""

LISTING = {'root': '/data', 'files': [
    {'path': 'alice/2_s2_R.csv', 'name': '2_s2_R.csv', 'user': 'alice', 'size': 10,
     'modified': 2.0},
    {'path': 'alice/1_s1_R.csv', 'name': '1_s1_R.csv', 'user': 'alice', 'size': 10,
     'modified': 1.0},
]}


class TestResults:
    @pytest.fixture(autouse=True)
    def _files(self, scripted):
        scripted.replies[('GET', '/results')] = (200, LISTING)
        scripted.replies[('GET', '/results/file')] = (200, RUN_FILE)

    def test_the_current_run_is_summarised_from_its_listed_file(self, call, scripted):
        scripted.replies[('GET', '/session')] = (200, {
            'state': 'idle', 'run_id': 'run-2', 'pending_prompt': None,
            'path': 'measurement_data/alice/1_s1_R.csv'})
        failed, summarised = call('get_run_summary')
        assert not failed
        assert (summarised['run_id'], summarised['path']) == ('run-2', 'alice/1_s1_R.csv')
        assert summarised['columns']['R_ohm']['mean'] == 101.0
        assert summarised['end'] == {'total_samples': 2}
        assert scripted.requests[-1][2] == {'path': 'alice/1_s1_R.csv'}

    def test_an_earlier_run_is_found_through_its_events(self, call, scripted):
        scripted.replies[('GET', '/session')] = (200, {'state': 'idle', 'run_id': 'run-3',
                                                       'pending_prompt': None,
                                                       'path': 'x/alice/3.csv'})
        scripted.replies[('GET', '/session/events')] = (200, {'gap': False, 'cursor': 2,
                                                              'last_seq': 2, 'events': [
            {'type': 'file_opened', 'run_id': 'run-1', 'seq': 1, 'cursor': 1,
             'payload': {'path': 'measurement_data/alice/2_s2_R.csv'}},
            {'type': 'run_started', 'run_id': 'run-3', 'seq': 1, 'cursor': 2,
             'payload': {}}]})
        failed, summarised = call('get_run_summary', {'run_id': 'run-1'})
        assert (failed, summarised['path']) == (False, 'alice/2_s2_R.csv')

    def test_a_run_that_wrote_no_file_says_so(self, call, scripted):
        scripted.replies[('GET', '/session')] = (200, {'state': 'idle', 'run_id': 'run-2',
                                                       'pending_prompt': None, 'path': None})
        failed, text = call('get_run_summary')
        assert failed and 'run-2 wrote no data file' in text

    def test_a_compressed_file_is_explained(self, call, scripted):
        scripted.replies[('GET', '/results/file')] = (415, {'detail': 'only .csv files can '
                                                                      'be previewed'})
        failed, text = call('get_run_summary', {'path': 'alice/1.csv.gz'})
        assert failed and 'not a plain .csv' in text

    def test_read_result_gives_a_slice(self, call):
        failed, sliced = call('read_result', {'path': 'alice/1_s1_R.csv', 'offset': -1})
        assert not failed
        assert sliced['rows'] == [[0.2, 102, 'OK', '']]
        assert (sliced['total_rows'], sliced['offset']) == (2, 1)

    def test_read_result_holds_the_bound(self, call):
        failed, text = call('read_result', {'path': 'a.csv', 'rows': 100_000})
        assert failed and 'rows' in text

    def test_list_results_by_sample(self, call, scripted):
        failed, listed = call('list_results', {'sample': 'S2'})
        assert [f['path'] for f in listed['files']] == ['alice/2_s2_R.csv']
        assert scripted.requests[-1][2] == {'limit': '5000'}

    def test_the_result_tools_are_read_only(self, connection_file):
        tools = _list_tools(connection_file)
        for name in ('get_run_summary', 'list_results', 'read_result', 'list_maps', 'get_map'):
            assert tools[name].annotations.read_only_hint is True, name

    def test_get_map_names_the_user(self, call, scripted):
        scripted.replies[('GET', '/maps/m-1')] = (200, {'map_id': 'm-1', 'spots': []})
        assert call('get_map', {'map_id': 'm-1', 'user': 'alice'}) == (
            False, {'map_id': 'm-1', 'spots': []})
        assert scripted.requests[-1][2] == {'user': 'alice'}

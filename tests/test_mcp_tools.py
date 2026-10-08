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

PENDING = {'prompt_id': 'run-2:vdp_geometry-1', 'kind': 'vdp_geometry',
           'options': ['proceed', 'abort'], 'requires_human': True, 'detail': {'index': 1}}

SCHEMA = {'modes': {'resistance': {
    'model': 'ResistanceSettings',
    'fields': ['res_test_current', 'res_voltage_compliance'],
    'override_keys': ['res_test_current', 'res_voltage_compliance', 'sampling_rate']}}}


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
def call(scripted, connection_file, tmp_path):
    """Call one tool through a fresh in-memory MCP session; (is_error, payload)."""
    def run(name, arguments=None):
        async def go():
            backend = Backend(str(connection_file), transport=httpx2.MockTransport(scripted))
            server = build_server(backend, AuditLog(str(tmp_path / 'audit')))
            try:
                async with Client(server, mode='legacy',
                                  client_info=Implementation(name='test', version='1')) as client:
                    reply = await client.call_tool(name, arguments or {})
            finally:
                await backend.aclose()
            text = reply.content[0].text
            return reply.is_error, (text if reply.is_error else json.loads(text))
        return asyncio.run(go())
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

    def test_a_user_name_is_one_path_segment(self, call, scripted):
        call('get_profile', {'user': 'a/b c'})
        assert scripted.requests[-1][4] == '/profiles/a%2Fb%20c'

    def test_identify_sends_the_address(self, call, scripted):
        scripted.replies[('POST', '/instruments/identify')] = (200, {'model': '2420'})
        failed, reply = call('identify_instrument', {'address': 'GPIB0::24::INSTR'})
        assert (failed, reply) == (False, {'model': '2420'})
        assert scripted.requests[-1][3] == {'address': 'GPIB0::24::INSTR'}

    def test_describe_mode_gives_the_keys_and_the_user_s_values(self, call, scripted):
        scripted.replies[('POST', '/settings/resolve')] = (200, {
            'ok': True, 'issues': [],
            'settings': {'measurement': {'res_test_current': 0.001,
                                         'res_voltage_compliance': 5.0,
                                         'sampling_rate': 10.0, 'vsource_voltage': 1.0}}})
        failed, described = call('describe_mode', {'mode': 'resistance'})
        assert not failed
        assert described == {
            'mode': 'resistance',
            'mode_keys': ['res_test_current', 'res_voltage_compliance'],
            'override_keys': ['res_test_current', 'res_voltage_compliance', 'sampling_rate'],
            'user': 'alice',
            'values': {'res_test_current': 0.001, 'res_voltage_compliance': 5.0,
                       'sampling_rate': 10.0},
            'issues': [],
        }

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
        assert checked['agent_may_start'] is True
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
        assert checked['agent_may_start'] is False
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
        assert checked['agent_may_start'] is True

    def test_settings_with_errors_cannot_be_started(self, call, scripted):
        self._resolve(scripted, ok=False, agent_limits=None,
                      issues=[{'key': 'res_test_current', 'message': 'too large',
                               'severity': 'error'}])
        failed, checked = call('check_settings', {'user': 'alice', 'mode': 'resistance'})
        assert checked['agent_may_start'] is False
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
        for name in ('wait_for', 'get_run_events'):
            assert tools[name].annotations.read_only_hint is True, name

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
        assert failed and 'run_ended, prompt, samples:N or state:<state>' in text

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

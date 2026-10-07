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

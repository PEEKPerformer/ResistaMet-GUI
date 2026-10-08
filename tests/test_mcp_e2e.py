"""The MCP server against a real backend on the simulator, end to end.

The backend is the sidecar process, started with ``--simulate
--allow-agents`` under a home directory of its own, as a user would start
it. The test plays the person at the window with the handshake's ``ui``
token: it creates the operator and, for one test, raises an agent limit.
The agent is the MCP server, built in this process and driven through the
SDK's in-memory client; it finds the backend through the connection file
like any other MCP server would. No model is involved.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
httpx2 = pytest.importorskip("httpx2")

from mcp import Client  # noqa: E402
from mcp.types import Implementation  # noqa: E402

from resistamet_gui.mcp.audit import AuditLog  # noqa: E402
from resistamet_gui.mcp.client import Backend  # noqa: E402
from resistamet_gui.mcp.server import build_server  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]

#: Every tool call the tests make, so the audit log can be held to it.
CALLS = []


class Bench:
    """The sidecar, its tokens, and where it keeps things."""

    def __init__(self, root: Path):
        self.root = root
        home = root / 'home'
        home.mkdir()
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
                   RESISTAMET_DISABLE_NI_USB='1')
        env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(_REPO), env.get('PYTHONPATH')]))
        self.process = subprocess.Popen(
            [sys.executable, '-m', 'resistamet_gui.api', '--port', '0', '--simulate',
             '--allow-agents', '--no-watchdog', '--config', str(root / 'config.json')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, cwd=str(root), env=env)
        line = self.process.stdout.readline()
        assert line, "the backend printed no handshake"
        handshake = json.loads(line)
        self.url, self.ui_token = handshake['url'], handshake['token']
        self.connection_file = home / '.resistamet' / 'api' / 'connection.json'
        deadline = time.time() + 15
        while not self.connection_file.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert self.connection_file.exists(), "agent access did not come on"
        self.agent_token = json.loads(self.connection_file.read_text())['agent_token']
        self.audit_dir = root / 'audit'

    def ui(self, method, path, **kwargs):
        """A request as the person at the window."""
        with httpx2.Client(base_url=self.url, trust_env=False, timeout=10,
                           headers={'Authorization': f'Bearer {self.ui_token}'}) as client:
            response = client.request(method, path, **kwargs)
        assert response.status_code < 400, response.text
        return response.json()

    def as_agent(self, method, path, **kwargs):
        """A request with the agent token, past the MCP server."""
        with httpx2.Client(base_url=self.url, trust_env=False, timeout=10,
                           headers={'Authorization': f'Bearer {self.agent_token}'}) as client:
            return client.request(method, path, **kwargs)

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=45)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)


@pytest.fixture(scope='module')
def bench(tmp_path_factory):
    made = Bench(tmp_path_factory.mktemp('mcp-bench'))
    try:
        made.ui('POST', '/users', json={'username': 'alice'})
        made.ui('PATCH', '/profiles/alice', json={
            'file': {'data_directory': str(made.root / 'data')},
            'measurement': {'settling_time': 0.0}})
        yield made
    finally:
        made.close()


def agent(bench, steps):
    """Run ``steps(call)`` in one MCP session; ``call(name, args)`` -> (is_error, payload)."""
    async def go():
        backend = Backend(str(bench.connection_file))
        server = build_server(backend, AuditLog(str(bench.audit_dir), redact=backend.redact))
        try:
            async with Client(server, mode='legacy',
                              client_info=Implementation(name='e2e', version='1')) as client:
                async def call(name, arguments=None):
                    CALLS.append(name)
                    reply = await client.call_tool(name, arguments or {})
                    text = reply.content[0].text
                    return reply.is_error, (text if reply.is_error else json.loads(text))
                return await steps(call)
        finally:
            await backend.aclose()
    return asyncio.run(go())


async def _end_any_run(call):
    await call('stop_run')
    failed, waited = await call('wait_for', {'until': 'run_ended', 'timeout_s': 60})
    assert not failed and waited['fired'] == 'run_ended', waited
    return waited


def test_the_tools_are_listed(bench):
    async def listed():
        backend = Backend(str(bench.connection_file))
        server = build_server(backend, AuditLog(str(bench.audit_dir)))
        try:
            async with Client(server, mode='legacy') as client:
                return {tool.name for tool in (await client.list_tools()).tools}
        finally:
            await backend.aclose()

    assert asyncio.run(listed()) == {
        'get_status', 'list_instruments', 'identify_instrument', 'list_users', 'get_profile',
        'describe_mode', 'check_settings', 'start_run', 'stop_run', 'abort_run', 'pause_run',
        'resume_run', 'mark_event', 'wait_for', 'get_run_events', 'get_run_summary',
        'list_results', 'read_result', 'list_maps', 'get_map'}

    async def steps(call):
        failed, status = await call('get_status')
        assert not failed
        assert status['state'] == 'idle'
        assert status['backend'] == 'ok'

    agent(bench, steps)


def test_check_settings_within_and_beyond_the_agent_limit(bench):
    async def steps(call):
        failed, within = await call('check_settings', {'user': 'alice', 'mode': 'resistance'})
        assert not failed and within['ok'] and within['agent_may_start'], within
        failed, beyond = await call('check_settings', {
            'user': 'alice', 'mode': 'source_v', 'overrides': {'vsource_voltage': 40.0}})
        assert not failed
        assert beyond['agent_may_start'] is False
        [violation] = beyond['agent_limits']['violations']
        assert (violation['limit'], violation['value'], violation['allowed']) == (
            'max_voltage_v', 40.0, 30.0)
        assert beyond['hazard']['hazardous'] is True

    agent(bench, steps)


def test_describe_mode_says_what_each_key_accepts_and_where_it_comes_from(bench):
    async def steps(call):
        failed, described = await call('describe_mode', {'mode': 'four_point',
                                                         'user': 'alice'})
        assert not failed, described
        assert described['mode_keys']['fpp_model'].startswith(
            '"thin_film", from the profile (the default); '
            'one of thin_film|semi_infinite|finite_thin|finite_alpha; ')
        assert described['mode_keys']['fpp_temperature_c'].startswith('null, from the profile')
        # The profile stores the defaults "once" and 5; a four-point run uses its own.
        assert described['shared_keys']['auto_zero'].startswith(
            '"on", fixed by the mode (the profile\'s "once" is not used)')
        assert described['shared_keys']['filter_count'] == \
            '10, fixed by the mode (the profile\'s 5 is not used)'

    agent(bench, steps)


def test_a_resistance_run_from_start_to_summary(bench):
    async def steps(call):
        failed, started = await call('start_run', {'user': 'alice', 'mode': 'resistance',
                                                   'sample_name': 'mcp-e2e'})
        assert not failed, started
        run_id = started['run_id']

        failed, waited = await call('wait_for', {'until': 'samples:5', 'timeout_s': 60})
        assert not failed and waited['fired'] == 'samples:5', waited
        assert waited['samples'] >= 5

        failed, running = await call('get_run_summary')
        assert not failed, running
        assert running['run_id'] == run_id
        assert running['finalized'] is False
        resistance = running['columns']['R_ohm']
        assert resistance['count'] >= 5
        assert resistance['mean'] == pytest.approx(100.0, rel=1e-3)  # --sim-resistance 100
        assert resistance['unit'] == 'Ω'
        assert running['metadata']['started_by'] == 'agent'
        assert running['metadata']['client.name'] == 'resistamet-mcp'

        failed, mark = await call('mark_event', {'label': 'checked by agent'})
        assert not failed, mark
        # The next sample carries the mark. Count from where the run is now,
        # not from a fixed number it may already have passed.
        failed, now = await call('wait_for', {'until': 'samples:1', 'timeout_s': 10})
        assert not failed, now
        target = now['samples'] + 2
        failed, waited = await call('wait_for', {'until': f'samples:{target}', 'timeout_s': 60})
        assert not failed and waited['fired'] == f'samples:{target}', waited

        ended = await _end_any_run(call)
        assert ended['run_ended']['reason'] == 'user_stop'

        failed, final = await call('get_run_summary', {'run_id': run_id})
        assert not failed and final['finalized'] is True
        assert final['end']['total_samples'] == final['rows']
        assert final['marks'][0]['label'] == 'checked by agent'

        failed, events = await call('get_run_events', {'run_id': run_id})
        assert not failed
        types = [event['type'] for event in events['events']]
        assert types[0] == 'run_started' and types[-1] == 'run_ended'
        assert 'sample' not in types and events['samples_total'] == final['rows']

        failed, sliced = await call('read_result', {'path': final['path'], 'offset': -2})
        assert not failed and len(sliced['rows']) == 2

    agent(bench, steps)


def test_a_start_over_the_voltage_limit_carries_the_violation(bench):
    async def steps(call):
        failed, text = await call('start_run', {'user': 'alice', 'mode': 'source_v',
                                                'sample_name': 'too-high',
                                                'overrides': {'vsource_voltage': 40.0}})
        assert failed
        assert '"limit":"max_voltage_v"' in text and '"allowed":30.0' in text
        failed, status = await call('get_status')
        assert status['state'] == 'idle'

    agent(bench, steps)


def test_a_van_der_pauw_run_waits_for_a_person(bench):
    async def steps(call):
        failed, started = await call('start_run', {
            'user': 'alice', 'mode': 'vdp', 'sample_name': 'vdp-e2e',
            'overrides': {'vdp_thickness_cm': 0.01}})
        assert not failed, started
        failed, waited = await call('wait_for', {'until': 'prompt', 'timeout_s': 60})
        assert not failed and waited['fired'] == 'prompt', waited
        prompt = waited['status']['pending_prompt']
        assert prompt['kind'] == 'vdp_geometry' and prompt['requires_human'] is True
        assert 'person must answer' in prompt['who_answers']

        # Nothing in the tools answers it, and the agent token cannot either.
        refused = bench.as_agent('POST', '/session/prompt', json={
            'prompt_id': prompt['prompt_id'], 'choice': 'proceed'})
        assert refused.status_code == 403
        failed, status = await call('get_status')
        assert status['state'] == 'awaiting_prompt'

        await _end_any_run(call)

    agent(bench, steps)


def test_a_van_der_pauw_run_without_a_thickness_with_a_person_at_the_bench(bench):
    """Four rewiring prompts, as describe_mode says; R_s, and no resistivity."""
    async def steps(call):
        failed, described = await call('describe_mode', {'mode': 'vdp', 'user': 'alice'})
        assert not failed and 'four prompts' in described['prompts']

        failed, started = await call('start_run', {
            'user': 'alice', 'mode': 'vdp', 'sample_name': 'vdp-no-thickness',
            'overrides': {'vdp_thickness_cm': 0.0}})
        assert not failed, started
        answered = []
        while True:
            failed, waited = await call('wait_for', {'until': 'prompt', 'timeout_s': 60})
            assert not failed, waited
            if waited['fired'] != 'prompt':
                break
            prompt = waited['status']['pending_prompt']
            answered.append((prompt['kind'], prompt['detail']['index']))
            # The person at the window, with the ui token, has rewired the leads.
            bench.ui('POST', '/session/prompt', json={'prompt_id': prompt['prompt_id'],
                                                      'choice': 'proceed'})
        assert answered == [('vdp_geometry', 0), ('vdp_geometry', 1),
                            ('vdp_geometry', 2), ('vdp_geometry', 3)]
        assert waited['run_ended']['reason'] == 'completed', waited

        failed, summarised = await call('get_run_summary', {'run_id': started['run_id']})
        assert not failed, summarised
        end = summarised['end']
        assert end['vdp_result.sheet_resistance'] > 0
        assert end['vdp_result.thickness_cm'] == 0.0
        assert [end[f'vdp_result.{key}'] for key in ('rho_avg', 'rho_a', 'rho_b')] == \
            [None, None, None]

    agent(bench, steps)


def test_a_hazardous_run_within_a_raised_limit_waits_for_a_person(bench):
    bench.ui('PATCH', '/profiles/alice', json={'agent_limits': {'max_voltage_v': 50.0}})
    try:
        async def steps(call):
            failed, started = await call('start_run', {
                'user': 'alice', 'mode': 'source_v', 'sample_name': 'hazard-e2e',
                'overrides': {'vsource_voltage': 31.0}})
            assert not failed, started
            failed, waited = await call('wait_for', {'until': 'samples:1', 'timeout_s': 60})
            assert not failed and waited['fired'] == 'prompt', waited
            prompt = waited['status']['pending_prompt']
            assert prompt['kind'] == 'safety_voltage_ack' and prompt['requires_human']
            ended = await _end_any_run(call)
            assert ended['run_ended']['samples'] == 0

        agent(bench, steps)
    finally:
        bench.ui('PATCH', '/profiles/alice', json={'agent_limits': {'max_voltage_v': 30.0}})


def test_a_fixed_number_of_readings_in_a_mode_without_a_count(bench):
    async def steps(call):
        failed, started = await call('start_run', {'user': 'alice', 'mode': 'resistance',
                                                   'sample_name': 'ten-readings'})
        assert not failed, started
        failed, waited = await call('wait_for', {'until': 'samples:10', 'then_stop': True,
                                                 'timeout_s': 60})
        assert not failed, waited
        assert (waited['fired'], waited['stopped']) == ('samples:10', True), waited
        assert waited['status']['state'] == 'idle'
        assert waited['run_ended']['reason'] == 'user_stop'
        rows = waited['run_ended']['samples']
        # At least the ten asked for. At most a few more: the simulator reads
        # at the profile's 10 Hz, so in the 0.1 s between two looks at the
        # count one more can arrive, and one more is in flight when the stop
        # lands; two more allow for a slow test machine. Two calls, wait then
        # stop, gave 57 for 20 in the usability trial.
        assert 10 <= rows <= 10 + 4, rows

        failed, first = await call('get_run_summary', {'run_id': started['run_id'],
                                                       'first_rows': 5})
        assert not failed, first
        assert first['finalized'] is True
        assert (first['rows'], first['rows_total']) == (5, rows)
        assert first['columns']['R_ohm']['count'] == 5
        assert first['columns']['elapsed_s']['count'] == 5

    agent(bench, steps)


def test_the_stdio_server_answers(bench):
    """``python -m resistamet_gui.mcp`` as an MCP client starts it: stdout is the protocol."""
    from mcp import StdioServerParameters

    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        filter(None, [str(_REPO), os.environ.get('PYTHONPATH')])))
    parameters = StdioServerParameters(
        command=sys.executable,
        args=['-m', 'resistamet_gui.mcp', '--connection-file', str(bench.connection_file),
              '--audit-dir', str(bench.root / 'stdio-audit')],
        env=env, cwd=str(bench.root))

    async def go():
        async with Client(parameters, mode='legacy') as client:
            reply = await client.call_tool('get_status', {})
            return reply.is_error, json.loads(reply.content[0].text)

    failed, status = asyncio.run(go())
    assert not failed and status['state'] == 'idle'
    [line] = [json.loads(text) for path in (bench.root / 'stdio-audit').glob('*.jsonl')
              for text in path.read_text(encoding='utf-8').splitlines()]
    assert line['tool'] == 'get_status' and line['http_status'] == 200


def test_the_audit_log_has_a_line_per_call_and_no_token(bench):
    files = sorted(bench.audit_dir.glob('*.jsonl'))
    assert files
    text = ''.join(path.read_text(encoding='utf-8') for path in files)
    lines = [json.loads(line) for line in text.splitlines()]
    assert [line['tool'] for line in lines] == CALLS
    assert all(line['client'] == {'name': 'e2e', 'version': '1'} for line in lines)
    assert bench.agent_token not in text and bench.ui_token not in text
    starts = [line for line in lines if line['tool'] == 'start_run']
    assert [line['outcome'] for line in starts] == ['ok', 'error', 'ok', 'ok', 'ok', 'ok']
    assert starts[1]['http_status'] == 422
    assert starts[0]['run_id'] and starts[0]['http_status'] == 202

"""The MCP audit log: one JSONL line per tool call (``docs/design/mcp_layer.md`` M7).

Pure: no SDK and no network. The middleware is driven with a stand-in for
the SDK's request context, which it only reads by attribute.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from resistamet_gui.mcp import audit
from resistamet_gui.mcp.audit import AuditLog, AuditMiddleware, capped

#: 2026-10-07 23:59:59.5 UTC and half a second later, across midnight.
LATE = 1791417599.5
NEXT_DAY = LATE + 1.0


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


class TestCapped:
    def test_a_small_value_is_kept_as_it_is(self):
        assert capped({'a': [1, 2]}, cap=20) == {'a': [1, 2]}

    def test_a_large_value_is_cut_and_says_so(self):
        # {"a":"xxxxxxxxxx"} is 18 characters of JSON.
        cut = capped({'a': 'x' * 10}, cap=8)
        assert cut == {'truncated': True, 'chars': 18, 'head': '{"a":"xx'}

    def test_exactly_at_the_cap_is_not_cut(self):
        assert capped('abcdef', cap=8) == 'abcdef'  # '"abcdef"' is 8


class TestAuditLog:
    def test_one_line_per_record_with_every_field(self, tmp_path):
        log = AuditLog(str(tmp_path), clock=lambda: LATE)
        log.record(tool='start_run', arguments={'mode': 'resistance'}, result={'run_id': 'run-1'},
                   outcome='ok', http_status=202, run_id='run-1',
                   client={'name': 'claude-code', 'version': '2.1'})
        [line] = _lines(tmp_path / '2026-10-07.jsonl')
        assert line == {
            'time': '2026-10-07T23:59:59.500000+00:00',
            'client': {'name': 'claude-code', 'version': '2.1'},
            'tool': 'start_run',
            'arguments': {'mode': 'resistance'},
            'outcome': 'ok',
            'http_status': 202,
            'run_id': 'run-1',
            'result': {'run_id': 'run-1'},
        }

    def test_the_file_is_named_for_the_utc_day(self, tmp_path):
        times = iter([LATE, NEXT_DAY])
        log = AuditLog(str(tmp_path), clock=lambda: next(times))
        log.record(tool='a', arguments={}, result=None, outcome='ok')
        log.record(tool='b', arguments={}, result=None, outcome='ok')
        assert [line['tool'] for line in _lines(tmp_path / '2026-10-07.jsonl')] == ['a']
        assert [line['tool'] for line in _lines(tmp_path / '2026-10-08.jsonl')] == ['b']

    def test_lines_are_appended_not_replaced(self, tmp_path):
        log = AuditLog(str(tmp_path), clock=lambda: LATE)
        for name in ('a', 'b', 'c'):
            log.record(tool=name, arguments={}, result=None, outcome='ok')
        assert [line['tool'] for line in _lines(tmp_path / '2026-10-07.jsonl')] == ['a', 'b', 'c']

    def test_a_large_result_is_truncated_and_marked(self, tmp_path):
        log = AuditLog(str(tmp_path), clock=lambda: LATE)
        log.record(tool='get_run_events', arguments={}, result={'events': ['e'] * 5000},
                   outcome='ok')
        [line] = _lines(tmp_path / '2026-10-07.jsonl')
        assert line['result']['truncated'] is True
        assert line['result']['chars'] == len('{"events":[' + ','.join(['"e"'] * 5000) + ']}')
        assert len(line['result']['head']) == audit.CAP_CHARS

    def test_redact_is_applied_to_the_whole_line(self, tmp_path):
        log = AuditLog(str(tmp_path), clock=lambda: LATE,
                       redact=lambda text: text.replace('s3cret', '***'))
        log.record(tool='t', arguments={'x': 's3cret'}, result='s3cret!', outcome='ok')
        text = (tmp_path / '2026-10-07.jsonl').read_text(encoding='utf-8')
        assert 's3cret' not in text
        assert _lines(tmp_path / '2026-10-07.jsonl')[0]['result'] == '***!'

    def test_a_log_that_cannot_be_written_does_not_fail_the_call(self, tmp_path):
        blocker = tmp_path / 'not-a-directory'
        blocker.write_text('')
        log = AuditLog(str(blocker), clock=lambda: LATE)
        log.record(tool='stop_run', arguments={}, result=None, outcome='ok')


def _ctx(method='tools/call', params=None, client=('claude-code', '2.1')):
    info = None if client is None else SimpleNamespace(name=client[0], version=client[1])
    return SimpleNamespace(
        method=method, params=params,
        session=SimpleNamespace(client_params=None if info is None
                                else SimpleNamespace(client_info=info)))


def _wire(structured=None, text='', is_error=False):
    wire = {'content': [{'type': 'text', 'text': text}], 'isError': is_error}
    if structured is not None:
        wire['structuredContent'] = structured
    return wire


class TestMiddleware:
    @pytest.fixture
    def log(self, tmp_path):
        return AuditLog(str(tmp_path), clock=lambda: LATE)

    @pytest.fixture
    def lines(self, tmp_path):
        return lambda: _lines(tmp_path / '2026-10-07.jsonl')

    def _call(self, middleware, ctx, handler):
        return asyncio.run(middleware(ctx, handler))

    def test_a_tool_call_is_written_with_what_the_tool_noted(self, log, lines):
        async def handler(ctx):
            audit.note_http_status(202)
            audit.note_run_id('run-3')
            return _wire({'run_id': 'run-3'}, '{"run_id":"run-3"}')

        params = {'name': 'start_run', 'arguments': {'mode': 'resistance'}}
        returned = self._call(AuditMiddleware(log), _ctx(params=params), handler)
        assert returned == _wire({'run_id': 'run-3'}, '{"run_id":"run-3"}')
        [line] = lines()
        assert line['tool'] == 'start_run'
        assert line['arguments'] == {'mode': 'resistance'}
        assert line['outcome'] == 'ok'
        assert line['http_status'] == 202
        assert line['run_id'] == 'run-3'
        assert line['client'] == {'name': 'claude-code', 'version': '2.1'}
        assert line['result'] == {'run_id': 'run-3'}

    def test_an_error_result_is_an_error_with_its_text(self, log, lines):
        async def handler(ctx):
            audit.note_http_status(422)
            return _wire(text='Error executing tool start_run: refused', is_error=True)

        self._call(AuditMiddleware(log), _ctx(params={'name': 'start_run', 'arguments': {}}),
                   handler)
        [line] = lines()
        assert (line['outcome'], line['http_status']) == ('error', 422)
        assert line['result'] == 'Error executing tool start_run: refused'

    def test_the_run_id_is_found_in_the_status_a_tool_returned(self, log, lines):
        async def handler(ctx):
            return _wire({'fired': 'run_ended', 'status': {'run_id': 'run-7'}})

        self._call(AuditMiddleware(log), _ctx(params={'name': 'wait_for', 'arguments': {}}),
                   handler)
        assert lines()[0]['run_id'] == 'run-7'

    def test_a_raised_failure_is_written_and_raised_again(self, log, lines):
        async def handler(ctx):
            raise RuntimeError('boom')

        with pytest.raises(RuntimeError):
            self._call(AuditMiddleware(log), _ctx(params={'name': 'x', 'arguments': {}}),
                       handler)
        [line] = lines()
        assert line['outcome'] == 'error'
        assert line['result'] == 'RuntimeError: boom'

    def test_other_methods_pass_through_unwritten(self, log, tmp_path):
        async def handler(ctx):
            return {'tools': []}

        assert self._call(AuditMiddleware(log), _ctx(method='tools/list'), handler) == \
            {'tools': []}
        assert list(tmp_path.iterdir()) == []

    def test_a_client_that_did_not_say_who_it_is_is_null(self, log, lines):
        async def handler(ctx):
            return _wire({})

        self._call(AuditMiddleware(log),
                   _ctx(params={'name': 'get_status', 'arguments': {}}, client=None), handler)
        assert lines()[0]['client'] is None

    def test_notes_outside_a_tool_call_are_ignored(self):
        audit.note_http_status(500)
        audit.note_run_id('run-1')

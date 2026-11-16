"""How the MCP server finds the backend and keeps finding it.

The connection file is the only way in (``docs/design/mcp_layer.md`` M2).
A missing file, a file whose process is gone, and a backend that refuses
the token all mean the same thing to an agent, said in words it can pass
on. A backend that restarted, or minted a new token, is found again by
reading the file once more. The backend is a ``MockTransport``: these
tests are about the client, not the network.
"""
import asyncio
import json
import os
import subprocess
import sys

import pytest

httpx2 = pytest.importorskip("httpx2")

from resistamet_gui.mcp.client import (NOT_RUNNING, Backend, BackendError,  # noqa: E402
                                       BackendUnavailable, Connection,
                                       default_connection_file, read_connection_file)


def _write(path, url='http://127.0.0.1:5000', token='tok-1', pid=None):
    path.write_text(json.dumps({'url': url, 'agent_token': token,
                                'pid': os.getpid() if pid is None else pid,
                                'started': 1.0}), encoding='utf-8')


def _dead_pid():
    process = subprocess.Popen([sys.executable, '-c', 'pass'])
    process.wait()
    return process.pid


class FakeBackend:
    """Answers like the API: 401 unless the token is one it holds."""

    def __init__(self, tokens=('tok-1',), url='http://127.0.0.1:5000'):
        self.tokens = set(tokens)
        self.url = url
        self.seen = []

    def __call__(self, request):
        self.seen.append((str(request.url), request.headers.get('authorization')))
        if not str(request.url).startswith(self.url):
            raise httpx2.ConnectError('connection refused', request=request)
        if request.headers.get('authorization') not in {f'Bearer {t}' for t in self.tokens}:
            return httpx2.Response(401, json={'detail': 'invalid token'})
        if request.url.path == '/session/start':
            return httpx2.Response(422, json={'detail': {
                'message': 'beyond the agent limits',
                'violations': [{'limit': 'max_voltage_v', 'value': 40.0, 'allowed': 30.0}]}})
        if request.url.path == '/results/file':
            return httpx2.Response(200, text='x' * 100)
        return httpx2.Response(200, json={'state': 'idle'})


def _run(coroutine):
    return asyncio.run(coroutine)


async def _call(backend, *args, **kwargs):
    try:
        return await backend.call(*args, **kwargs)
    finally:
        await backend.aclose()


def test_the_default_file_is_the_one_the_backend_writes():
    expected = os.path.join(os.path.expanduser('~'), '.resistamet', 'api', 'connection.json')
    assert os.path.normpath(default_connection_file()) == os.path.normpath(expected)


class TestReadingTheFile:
    def test_a_file_names_the_backend(self, tmp_path):
        path = tmp_path / 'connection.json'
        _write(path, url='http://127.0.0.1:5000/', token='tok-1')
        connection = read_connection_file(str(path))
        assert connection == Connection('http://127.0.0.1:5000', 'tok-1', os.getpid())

    def test_the_token_is_not_in_the_repr(self, tmp_path):
        path = tmp_path / 'connection.json'
        _write(path, token='tok-secret')
        assert 'tok-secret' not in repr(read_connection_file(str(path)))

    def test_no_file_means_no_backend(self, tmp_path):
        with pytest.raises(BackendUnavailable) as caught:
            read_connection_file(str(tmp_path / 'connection.json'))
        assert str(caught.value).startswith(NOT_RUNNING)
        assert 'no connection file' in str(caught.value)

    @pytest.mark.skipif(sys.platform == 'win32', reason="no cheap process probe on Windows")
    def test_a_file_whose_process_is_gone_is_stale(self, tmp_path):
        path = tmp_path / 'connection.json'
        _write(path, pid=_dead_pid())
        with pytest.raises(BackendUnavailable) as caught:
            read_connection_file(str(path))
        assert 'no longer running' in str(caught.value)

    @pytest.mark.parametrize('content', ['', 'not json', '[]', '{"url": "http://x"}',
                                         '{"agent_token": "t"}'])
    def test_a_file_that_names_nothing_is_no_backend(self, tmp_path, content):
        path = tmp_path / 'connection.json'
        path.write_text(content, encoding='utf-8')
        with pytest.raises(BackendUnavailable):
            read_connection_file(str(path))


class TestTalkingToIt:
    def test_a_request_carries_the_agent_token(self, tmp_path):
        path = tmp_path / 'connection.json'
        _write(path)
        fake = FakeBackend()
        backend = Backend(str(path), transport=httpx2.MockTransport(fake))
        assert _run(_call(backend, 'GET', '/session')) == {'state': 'idle'}
        assert fake.seen == [('http://127.0.0.1:5000/session', 'Bearer tok-1')]

    def test_no_file_is_no_backend(self, tmp_path):
        backend = Backend(str(tmp_path / 'connection.json'),
                          transport=httpx2.MockTransport(FakeBackend()))
        with pytest.raises(BackendUnavailable):
            _run(_call(backend, 'GET', '/session'))

    def test_a_refused_token_is_no_backend(self, tmp_path):
        path = tmp_path / 'connection.json'
        _write(path, token='withdrawn')
        fake = FakeBackend()
        backend = Backend(str(path), transport=httpx2.MockTransport(fake))
        with pytest.raises(BackendUnavailable) as caught:
            _run(_call(backend, 'GET', '/session'))
        assert 'agent access was turned off' in str(caught.value)
        assert 'withdrawn' not in str(caught.value)
        assert len(fake.seen) == 2  # read again and tried once more, then gave up

    def test_nothing_listening_is_no_backend(self, tmp_path):
        path = tmp_path / 'connection.json'
        _write(path, url='http://127.0.0.1:6000')
        backend = Backend(str(path), transport=httpx2.MockTransport(FakeBackend()))
        with pytest.raises(BackendUnavailable) as caught:
            _run(_call(backend, 'GET', '/session'))
        assert 'nothing answers at http://127.0.0.1:6000' in str(caught.value)

    def test_a_new_token_is_picked_up_after_a_401(self, tmp_path):
        """Access turned off and on again: the old token is refused, the file has the new one."""
        path = tmp_path / 'connection.json'
        _write(path, token='tok-1')
        fake = FakeBackend(tokens=('tok-1',))
        backend = Backend(str(path), transport=httpx2.MockTransport(fake))

        async def scenario():
            await backend.call('GET', '/session')
            fake.tokens = {'tok-2'}
            _write(path, token='tok-2')
            return await backend.call('GET', '/session')

        assert _run(_finally_close(backend, scenario())) == {'state': 'idle'}
        assert [auth for _, auth in fake.seen] == ['Bearer tok-1', 'Bearer tok-1', 'Bearer tok-2']

    def test_a_restarted_backend_is_found_at_its_new_url(self, tmp_path):
        path = tmp_path / 'connection.json'
        _write(path, url='http://127.0.0.1:5000')
        fake = FakeBackend(url='http://127.0.0.1:5000')
        backend = Backend(str(path), transport=httpx2.MockTransport(fake))

        async def scenario():
            await backend.call('GET', '/session')
            fake.url = 'http://127.0.0.1:5001'
            _write(path, url='http://127.0.0.1:5001')
            return await backend.call('GET', '/session')

        assert _run(_finally_close(backend, scenario())) == {'state': 'idle'}
        assert fake.seen[-1][0] == 'http://127.0.0.1:5001/session'

    def test_a_refusal_keeps_the_backend_s_detail_whole(self, tmp_path):
        path = tmp_path / 'connection.json'
        _write(path)
        backend = Backend(str(path), transport=httpx2.MockTransport(FakeBackend()))
        with pytest.raises(BackendError) as caught:
            _run(_call(backend, 'POST', '/session/start', json_body={}))
        assert caught.value.status == 422
        assert caught.value.detail['violations'] == [
            {'limit': 'max_voltage_v', 'value': 40.0, 'allowed': 30.0}]

    def test_a_text_reply_over_the_bound_is_refused(self, tmp_path):
        path = tmp_path / 'connection.json'
        _write(path)
        backend = Backend(str(path), transport=httpx2.MockTransport(FakeBackend()))

        async def read(limit):
            return await backend.get_text('/results/file', params={'path': 'a.csv'},
                                          max_bytes=limit)

        assert _run(read(100)) == 'x' * 100
        with pytest.raises(BackendError) as caught:
            _run(_finally_close(backend, read(99)))
        assert caught.value.status == 413

    def test_redact_hides_the_token_in_force(self, tmp_path):
        path = tmp_path / 'connection.json'
        _write(path, token='tok-1')
        backend = Backend(str(path), transport=httpx2.MockTransport(FakeBackend()))
        _run(_call(backend, 'GET', '/session'))
        assert backend.redact('Bearer tok-1 here') == 'Bearer *** here'


async def _finally_close(backend, coroutine):
    try:
        return await coroutine
    finally:
        await backend.aclose()

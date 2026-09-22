"""The sidecar going away in the middle of a run.

However the process is asked to leave -- a signal, or its parent closing
stdin -- the run has to be stopped first: output off, file finalized. These
spawn the real entry point, because signal handling is process behaviour.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
httpx = pytest.importorskip("httpx")


_REPO = Path(__file__).resolve().parents[1]


def _sidecar_env(tmp_path):
    """A home and a working directory of its own, so the sidecar's instrument
    locks, logs and data land under ``tmp_path``. PYTHONPATH keeps it
    importing this checkout from there."""
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    # RESISTAMET_DISABLE_NI_USB: nothing here may reach an adapter on the bench.
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
               RESISTAMET_DISABLE_NI_USB='1')
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(_REPO), env.get('PYTHONPATH')]))
    return env


@pytest.fixture
def running_sidecar(tmp_path):
    """A sidecar with a simulated run that has already written samples.

    Yields (process, path of the run's file).
    """
    process = subprocess.Popen(
        [sys.executable, '-m', 'resistamet_gui.api', '--port', '0', '--simulate',
         '--config', str(tmp_path / 'config.json')],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=str(tmp_path), env=_sidecar_env(tmp_path),
    )
    try:
        line = process.stdout.readline()
        assert line, f"no handshake: {process.stderr.read()[:400]}"
        handshake = json.loads(line)
        url = handshake['url']
        headers = {'Authorization': f"Bearer {handshake['token']}"}
        started = httpx.post(
            f"{url}/session/start", headers=headers, timeout=10.0,
            json={'mode': 'resistance', 'sample_name': 'shutdown', 'username': 'e2e',
                  'overrides': {'res_test_current': 1e-3, 'res_voltage_compliance': 5.0}},
        )
        assert started.status_code == 202, started.text

        deadline = time.time() + 15
        status = {}
        while time.time() < deadline:
            status = httpx.get(f"{url}/session", headers=headers, timeout=5.0).json()
            events = httpx.get(f"{url}/session/events?limit=500", headers=headers,
                               timeout=5.0).json()['events']
            if status.get('path') and sum(e['type'] == 'sample' for e in events) >= 3:
                break
            time.sleep(0.1)
        assert status.get('path'), "the run never opened a file"
        assert status['state'] == 'running'
        # The path is relative to the sidecar's working directory.
        yield process, tmp_path / status['path']
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def _assert_finalized(process, path):
    stderr = process.stderr.read()
    assert process.returncode == 0, stderr[-2000:]
    text = path.read_text()
    assert '# --- run completed ---' in text or 'ended_at' in text, text[-600:]
    assert 'Export finalized' in stderr or 'finalized' in stderr.lower()


@pytest.mark.skipif(sys.platform == 'win32',
                    reason="SIGTERM is TerminateProcess on Windows; nothing can handle it")
class TestSignals:
    @pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGINT])
    def test_a_signal_mid_run_finalizes_the_file(self, running_sidecar, signum):
        process, path = running_sidecar

        process.send_signal(signum)
        process.wait(timeout=45)

        _assert_finalized(process, path)


class TestParentGoesAway:
    def test_stdin_closing_mid_run_finalizes_the_file(self, running_sidecar):
        process, path = running_sidecar

        process.stdin.close()
        process.wait(timeout=45)

        _assert_finalized(process, path)


class TestCheckVisaIsReadOnly:
    def test_it_does_not_create_the_config_it_is_asked_about(self, tmp_path):
        config = tmp_path / 'config.json'

        result = subprocess.run(
            [sys.executable, '-m', 'resistamet_gui.api', '--check-visa',
             '--visa-library', '@py', '--config', str(config)],
            capture_output=True, text=True, timeout=120,
            cwd=str(tmp_path), env=_sidecar_env(tmp_path))

        assert result.returncode == 0, result.stderr[-600:]
        assert json.loads(result.stdout)['requested'] == '@py'
        assert not config.exists()
        assert sorted(p.name for p in tmp_path.iterdir()) == ['home']


class TestTheTokenStaysOutOfTheLog:
    def test_a_websocket_connect_does_not_log_the_token(self, tmp_path):
        import http.client
        from urllib.parse import urlsplit

        process = subprocess.Popen(
            [sys.executable, '-m', 'resistamet_gui.api', '--port', '0', '--simulate',
             '--config', str(tmp_path / 'config.json')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=str(tmp_path), env=_sidecar_env(tmp_path))
        try:
            handshake = json.loads(process.stdout.readline())
            parts = urlsplit(handshake['url'])
            connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
            connection.request('GET', f"/session/events/ws?token={handshake['token']}&since_seq=0",
                               headers={'Connection': 'Upgrade', 'Upgrade': 'websocket',
                                        'Sec-WebSocket-Version': '13',
                                        'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ=='})
            assert connection.getresponse().status == 101
            connection.close()
            process.stdin.close()
            process.wait(timeout=30)
            stderr = process.stderr.read()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)

        assert handshake['token'] not in stderr
        assert 'token=***' in stderr


class TestBindAddress:
    @pytest.mark.parametrize('host', ['0.0.0.0', '192.168.1.20', 'lab-pc.example'])
    def test_a_host_that_is_not_loopback_is_refused(self, host, capsys):
        from resistamet_gui.api.__main__ import _parse_args

        with pytest.raises(SystemExit) as refused:
            _parse_args(['--host', host])

        assert refused.value.code == 2
        assert '--allow-remote' in capsys.readouterr().err

    @pytest.mark.parametrize('argv', [[], ['--host', '127.0.0.1'], ['--host', 'localhost'],
                                      ['--host', '::1'],
                                      ['--host', '0.0.0.0', '--allow-remote']])
    def test_loopback_or_an_explicit_opt_in_is_accepted(self, argv):
        from resistamet_gui.api.__main__ import _parse_args

        assert _parse_args(argv).host == (argv[1] if argv else '127.0.0.1')

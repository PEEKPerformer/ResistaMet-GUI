"""The sidecar as a real process: handshake, watchdog, shutdown.

These spawn the entry point rather than importing it, because what is being
tested is process behaviour — the parent reads one line from stdout, talks
HTTP, and gets its child back when it closes stdin.
"""
import json
import os
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
    """The sidecar is a separate process: give it a home and a working
    directory of its own, so its instrument locks, its logs and the data it
    writes land under ``tmp_path`` and not in the developer's home or in the
    checkout. PYTHONPATH keeps it importing this checkout from there."""
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(_REPO), env.get('PYTHONPATH')]))
    return env


def _spawn(tmp_path, *extra):
    """Start the sidecar and return (process, handshake)."""
    process = subprocess.Popen(
        [sys.executable, '-m', 'resistamet_gui.api', '--port', '0', '--simulate',
         '--config', str(tmp_path / 'config.json'), *extra],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=str(tmp_path), env=_sidecar_env(tmp_path),
    )
    line = process.stdout.readline()
    assert line, f"no handshake: {process.stderr.read()[:400]}"
    return process, json.loads(line)


def _stop(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


class TestHandshake:
    def test_handshake_is_one_json_line_on_stdout(self, tmp_path):
        process, handshake = _spawn(tmp_path)
        try:
            assert handshake['url'].startswith('http://127.0.0.1:')
            assert handshake['token']
            assert handshake['pid'] == process.pid
        finally:
            _stop(process)

    def test_the_api_answers_on_the_advertised_url(self, tmp_path):
        process, handshake = _spawn(tmp_path)
        try:
            response = httpx.get(f"{handshake['url']}/health", timeout=5.0)
            assert response.status_code == 200
        finally:
            _stop(process)

    def test_the_advertised_token_works_and_others_do_not(self, tmp_path):
        process, handshake = _spawn(tmp_path)
        try:
            url = f"{handshake['url']}/session"
            good = httpx.get(url, headers={'Authorization': f"Bearer {handshake['token']}"},
                              timeout=5.0)
            bad = httpx.get(url, headers={'Authorization': 'Bearer nope'}, timeout=5.0)
            assert good.status_code == 200
            assert good.json()['state'] == 'idle'
            assert bad.status_code == 401
        finally:
            _stop(process)

    def test_logs_go_to_stderr_not_stdout(self, tmp_path):
        """The parent reads stdout as a protocol; a log line there breaks it."""
        process, handshake = _spawn(tmp_path)
        try:
            httpx.get(f"{handshake['url']}/health", timeout=5.0)
            process.stdin.close()
            process.wait(timeout=20)
            remainder = process.stdout.read()
            assert remainder.strip() == ''
        finally:
            _stop(process)


def _websocket_upgrade_status(url: str, token: str) -> int:
    """Send a bare WebSocket handshake and return the HTTP status uvicorn answers.

    No client library: what is being tested is the server process's ability
    to upgrade at all, which Starlette's in-process test client never asks
    of uvicorn.
    """
    import http.client
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        connection.request('GET', f"/session/events/ws?token={token}", headers={
            'Connection': 'Upgrade',
            'Upgrade': 'websocket',
            'Sec-WebSocket-Version': '13',
            'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ==',
        })
        return connection.getresponse().status
    finally:
        connection.close()


class TestEventsWebSocket:
    """The real server process must upgrade, not just serve HTTP.

    uvicorn has no WebSocket implementation of its own; without one it logs
    "No supported WebSocket library detected" and answers 404. The desktop
    UI then never receives an event and shows "Reconnecting" for ever, which
    is exactly what the first frozen build did on the lab PC.
    """

    def test_the_server_process_accepts_the_upgrade(self, tmp_path):
        process, handshake = _spawn(tmp_path)
        try:
            assert _websocket_upgrade_status(handshake['url'], handshake['token']) == 101
        finally:
            _stop(process)

    def test_a_bad_token_is_refused_not_ignored(self, tmp_path):
        process, handshake = _spawn(tmp_path)
        try:
            status = _websocket_upgrade_status(handshake['url'], 'wrong')
            # Accepted then closed with 4401, or refused outright: either is
            # the server speaking WebSocket. 404 would mean it cannot.
            assert status != 404
        finally:
            _stop(process)


class TestShutdown:
    def test_closing_stdin_stops_the_process(self, tmp_path):
        """A killed parent must not leave a process holding the instrument."""
        process, _ = _spawn(tmp_path)
        try:
            process.stdin.close()
            process.wait(timeout=20)
            assert process.returncode is not None
        finally:
            _stop(process)

    def test_shutdown_route_stops_the_process(self, tmp_path):
        process, handshake = _spawn(tmp_path)
        try:
            httpx.post(f"{handshake['url']}/session/shutdown",
                        headers={'Authorization': f"Bearer {handshake['token']}"},
                        timeout=5.0)
            process.wait(timeout=20)
            assert process.returncode is not None
        finally:
            _stop(process)

    def test_a_run_is_finalized_before_exit(self, tmp_path):
        """Shutdown stops the run, so the file is closed and the output off."""
        process, handshake = _spawn(tmp_path)
        headers = {'Authorization': f"Bearer {handshake['token']}"}
        try:
            httpx.post(f"{handshake['url']}/users", headers=headers, timeout=5.0)
            started = httpx.post(
                f"{handshake['url']}/session/start", headers=headers, timeout=10.0,
                json={'mode': 'resistance', 'sample_name': 'sidecar', 'username': 'e2e',
                       'overrides': {'res_test_current': 1e-3,
                                      'res_voltage_compliance': 5.0}},
            )
            assert started.status_code == 202

            # Wait for real rows, not just an open file: the point is that
            # shutdown finalizes a run that was actually measuring.
            deadline = time.time() + 15
            while time.time() < deadline:
                status = httpx.get(f"{handshake['url']}/session", headers=headers,
                                    timeout=5.0).json()
                events = httpx.get(f"{handshake['url']}/session/events?limit=500",
                                    headers=headers, timeout=5.0).json()['events']
                if status['path'] and any(e['type'] == 'sample' for e in events):
                    break
                time.sleep(0.1)
            assert status['path'], "run never opened a file"

            httpx.post(f"{handshake['url']}/session/shutdown", headers=headers, timeout=5.0)
            process.wait(timeout=30)

            # The path is relative to the sidecar's working directory.
            with open(tmp_path / status['path']) as handle:
                text = handle.read()
            assert '# --- run completed ---' in text or 'ended_at' in text
        finally:
            _stop(process)


class TestCheckVisa:
    """``--check-visa`` serves nothing; it reports and exits."""

    def _check(self, tmp_path, *extra):
        result = subprocess.run(
            [sys.executable, '-m', 'resistamet_gui.api', '--check-visa',
             '--config', str(tmp_path / 'config.json'), *extra],
            capture_output=True, text=True, timeout=120,
            cwd=str(tmp_path), env=_sidecar_env(tmp_path),
        )
        return result, json.loads(result.stdout)

    def test_it_reports_the_backend_and_exits_zero(self, tmp_path):
        result, report = self._check(tmp_path)
        assert result.returncode == 0
        assert report['ok'] is True
        assert report['backend']['kind'] in ('ivi', 'py')
        # No bus traffic unless asked, so no resource list.
        assert 'resources' not in report

    def test_a_named_backend_is_used_and_named_back(self, tmp_path):
        result, report = self._check(tmp_path, '--visa-library', '@py')
        assert result.returncode == 0
        assert report['requested'] == '@py'
        assert report['backend']['kind'] == 'py'
        assert report['backend']['library'] == 'pyvisa-py'

    def test_the_ni_usb_driver_reports_itself(self, tmp_path):
        _, report = self._check(tmp_path)
        ni_usb = report['ni_usb']
        assert isinstance(ni_usb['available'], bool)
        assert isinstance(ni_usb['adapters'], list)
        # A source checkout uses a system libusb; only a frozen app bundles one.
        assert ni_usb.get('libusb') is None

    def test_a_missing_visa_library_is_reported_not_raised(self, tmp_path):
        result, report = self._check(tmp_path, '--visa-library',
                                     str(tmp_path / 'no-such-libvisa.so'))
        assert result.returncode == 1
        assert report['ok'] is False
        assert report['error']
        # The NI USB view is independent of the vendor library and still there.
        assert 'ni_usb' in report

    def test_bus_mode_enumerates(self, tmp_path):
        result = subprocess.run(
            [sys.executable, '-m', 'resistamet_gui.api', '--check-visa', 'bus',
             '--visa-library', '@py', '--config', str(tmp_path / 'config.json')],
            capture_output=True, text=True, timeout=120,
            cwd=str(tmp_path), env=_sidecar_env(tmp_path),
        )
        report = json.loads(result.stdout)
        assert result.returncode == 0
        assert isinstance(report.get('resources', report.get('resources_error')), (list, str))

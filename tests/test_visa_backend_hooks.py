"""A pyvisa-py extension hook that fails must not block the bus."""
import logging

import pyvisa
import pytest

from resistamet_gui import visa_backend


@pytest.fixture
def recording_rm(monkeypatch):
    """Replace pyvisa.ResourceManager with a factory that records its args."""
    calls = []

    class _RM:
        def list_resources(self):
            return ()

    def _factory(*args, **kwargs):
        calls.append(args)
        return _RM()

    monkeypatch.setattr(pyvisa, 'ResourceManager', _factory)
    return calls


def _broken_hook():
    raise AttributeError("type object 'Session' has no attribute '_session_classes'")


@pytest.mark.parametrize('library, expected', [('', ()), ('@py', ('@py',))])
def test_a_raising_hook_is_skipped_with_a_warning(
        recording_rm, monkeypatch, caplog, library, expected):
    monkeypatch.setattr(visa_backend, '_PY_EXTENSIONS', [])
    visa_backend.register_py_extension(_broken_hook)

    with caplog.at_level(logging.WARNING, logger=visa_backend.logger.name):
        rm = visa_backend.resource_manager(library)

    assert rm is not None
    assert recording_rm == [expected]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert '_broken_hook' in warnings[0].getMessage()
    assert '_session_classes' in warnings[0].getMessage()


def test_the_hooks_after_a_raising_one_still_run(recording_rm, monkeypatch):
    ran = []
    monkeypatch.setattr(visa_backend, '_PY_EXTENSIONS', [])
    visa_backend.register_py_extension(_broken_hook)
    visa_backend.register_py_extension(lambda: ran.append('later hook'))

    visa_backend.resource_manager('')

    assert ran == ['later hook']

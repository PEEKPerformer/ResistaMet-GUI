"""The PySide6 entry point turns file logging on.

``logging_config.setup_logging`` existed for the whole of v1 and nothing
called it, so the log file the documentation pointed at was never written.
"""
from __future__ import annotations

import logging

import pytest

from resistamet_gui import __main__ as entry
from resistamet_gui.logging_config import get_logger


@pytest.fixture
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    yield tmp_path
    logging.getLogger("resistamet_gui").handlers.clear()


def _log_files(home):
    return sorted((home / ".resistamet" / "logs").glob("resistamet_*.log"))


def test_a_log_file_is_written_under_the_home_directory(private_home):
    entry._start_logging()
    get_logger("test").info("one line from the window")

    files = _log_files(private_home)
    assert len(files) == 1
    text = files[0].read_text()
    assert "Logging initialized" in text
    assert "one line from the window" in text


def test_a_home_that_cannot_take_the_file_still_logs_to_the_terminal(private_home, capsys):
    (private_home / ".resistamet").write_text("not a directory")

    entry._start_logging()
    get_logger("test").info("still here")

    out = capsys.readouterr().out
    assert "Not writing a log file" in out
    assert "still here" in out
    assert not (private_home / ".resistamet" / "logs").exists()


def test_the_self_test_does_not_start_logging(private_home, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["resistamet-gui", "--self-test"])
    entry.main()
    assert "self-test passed" in capsys.readouterr().out
    assert not (private_home / ".resistamet").exists()

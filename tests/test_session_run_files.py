"""open_exporter does not leave a file open behind a failure of its own."""
import time

import pytest

from resistamet_gui.session import run_files


class _Exporter:
    def __init__(self):
        self.finalized = 0

    @property
    def output_paths(self):
        raise RuntimeError("paths unavailable")

    def finalize(self, end_metadata=None):
        self.finalized += 1


def _open(tmp_path):
    settings = {'measurement': {}, 'file': {}, 'output': {}}
    return run_files.open_exporter(
        base_path=tmp_path / 'run', mode='resistance', settings=settings,
        measurement_settings=settings['measurement'], username='alice', sample_name='s',
        instrument_idn='', start_time=time.time(), aux_columns=[], aux_units=[])


def test_a_failure_after_the_exporter_exists_finalizes_it(tmp_path, monkeypatch):
    made = _Exporter()
    monkeypatch.setattr(run_files, 'make_exporter', lambda **kwargs: made)
    with pytest.raises(RuntimeError, match="paths unavailable"):
        _open(tmp_path)
    assert made.finalized == 1


def test_a_finalize_that_also_fails_does_not_hide_the_first_error(tmp_path, monkeypatch):
    made = _Exporter()

    def refuse(end_metadata=None):
        raise OSError("disk gone")
    made.finalize = refuse
    monkeypatch.setattr(run_files, 'make_exporter', lambda **kwargs: made)
    with pytest.raises(RuntimeError, match="paths unavailable"):
        _open(tmp_path)

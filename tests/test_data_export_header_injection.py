"""No metadata value may start a line of its own in the file header.

The header is ``# key: value`` lines, and values such as the sample name come
from a client. A line break inside one would let it write header lines -- or
a data row -- of its own.
"""
import csv

import pytest

from resistamet_gui.data_export import CsvExporter, parse_metadata

PAYLOAD = "wafer\n# total_samples: 999\n0.0,1,1,1,1,OK,"


def _write(tmp_path, metadata, end_metadata=None):
    exporter = CsvExporter(tmp_path / 'run', metadata, ['elapsed_s', 'V'])
    exporter.write_row([0.1, 1.0])
    exporter.finalize(end_metadata or {'total_samples': 1})
    return tmp_path / 'run.csv'


def test_a_sample_name_cannot_plant_header_lines_or_rows(tmp_path):
    path = _write(tmp_path, {'user': 'alice', 'sample': PAYLOAD})

    lines = path.read_text(encoding='utf-8').splitlines()
    head = lines[:lines.index('elapsed_s,V')]
    assert all(line.startswith('# ') for line in head)
    assert sum(line.startswith('# total_samples') for line in lines) == 1
    assert [line for line in lines if line.startswith('# sample:')] == [
        r'# sample: wafer\n# total_samples: 999\n0.0,1,1,1,1,OK,']

    body = [line for line in lines if not line.startswith('#')]
    assert list(csv.reader(body)) == [['elapsed_s', 'V'], ['0.1', '1']]


def test_the_escaped_form_is_what_reads_back(tmp_path):
    path = _write(tmp_path, {'sample': PAYLOAD})

    meta = parse_metadata(path, text_keys=('sample',))

    assert meta['sample'] == r'wafer\n# total_samples: 999\n0.0,1,1,1,1,OK,'
    assert meta['total_samples'] == 1


@pytest.mark.parametrize('breaker', ['\r', '\r\n', '\x0b', '\x0c', '\x1c', '\x85',
                                     '\u2028', '\u2029'])
def test_every_character_a_reader_splits_lines_on_is_covered(tmp_path, breaker):
    path = _write(tmp_path, {'sample': f'a{breaker}# mode: fake'})

    meta = parse_metadata(path, text_keys=('sample',))

    assert 'mode' not in meta
    assert meta['sample'] == r'a\n# mode: fake'


def test_keys_and_end_metadata_are_covered_too(tmp_path):
    path = _write(tmp_path, {'aux': {'t\n# mode: fake': 1}},
                  end_metadata={'stop_reason': 'done\n# user: mallory', 'total_samples': 1})

    meta = parse_metadata(path, text_keys=('stop_reason',))

    assert 'mode' not in meta and 'user' not in meta
    assert meta['stop_reason'] == r'done\n# user: mallory'


def test_ordinary_values_are_written_as_before(tmp_path):
    path = _write(tmp_path, {'sample': r'C:\new\wafer #3', 'note': 'ratio 1:2'})

    text = path.read_text(encoding='utf-8')

    assert '# sample: C:\\new\\wafer #3\n' in text
    assert '# note: ratio 1:2\n' in text

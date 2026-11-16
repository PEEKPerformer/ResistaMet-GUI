"""A four-point map, end to end: three spots and a repeat, on the simulator.

What the design's verification section asks for. Each spot is a run through
MeasurementSession; the DUT changes between placements so the spots differ,
and the simulator adds a little noise so a spot has a spread worth checking.
No Qt is imported here.
"""
import copy
import csv
import json
import math
import time
from pathlib import Path

import pytest

from resistamet_gui.constants import DEFAULT_SETTINGS
from resistamet_gui.data_export import parse_metadata
from resistamet_gui.session.emitter import ListSink
from resistamet_gui.session.manager import MeasurementSession
from resistamet_gui.session.spot_map import assemble_map

MAP_ID = 'wafer7-map1'
SAMPLES = 5
CURRENT_A = 1e-3
#: (spot index, label, DUT ohms). Spot 2 is measured twice: a bad first
#: contact, then the repeat that should stand for it.
PLACEMENTS = [(1, 'centre', 100.0), (2, 'north', 150.0), (3, 'east', 120.0),
              (2, 'north again', 110.0)]


@pytest.fixture(autouse=True)
def _no_sleep_inhibitor(monkeypatch):
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit", lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit", lambda self: True)


@pytest.fixture
def profile(tmp_path):
    settings = copy.deepcopy({
        'measurement': DEFAULT_SETTINGS['measurement'],
        'display': DEFAULT_SETTINGS['display'],
        'file': DEFAULT_SETTINGS['file'],
        'output': DEFAULT_SETTINGS['output'],
    })
    settings['file']['data_directory'] = str(tmp_path / 'measurement_data')
    settings['measurement'].update({
        'sampling_rate': 50.0, 'settling_time': 0.0, 'nplc': 0.1, 'filter_enabled': False,
        'fpp_current': CURRENT_A, 'fpp_voltage_compliance': 5.0, 'fpp_samples': SAMPLES,
        'fpp_thickness_um': 100.0, 'fpp_power_warn_w': 1.0, 'fpp_power_stop_w': 2.0,
    })
    return settings


def _wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _next_second():
    """Run files are stamped to the second; two spots must not share one."""
    now = int(time.time())
    while int(time.time()) == now:
        time.sleep(0.02)


def _rows(path):
    with open(path) as handle:
        rows = [r for r in csv.reader(handle) if r and not r[0].startswith('#')]
    header, body = rows[0], rows[1:]
    return {name: [float(row[i]) for row in body]
            for i, name in enumerate(header) if name in ('Rs_ohm_sq', 'rho_ohm_cm', 'sigma_S_cm')}


def _mean_sd(values):
    mean = sum(values) / len(values)
    return mean, math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


@pytest.fixture
def measured_map(profile):
    """Run the four placements; return (sink, run paths in order)."""
    from resistamet_gui.simulator import enable_simulation

    sink = ListSink()
    session = MeasurementSession(sink)
    paths = []
    try:
        for index, label, dut_ohms in PLACEMENTS:
            enable_simulation(dut_resistance_ohms=dut_ohms, model="2420", noise_rsd=1e-3)
            _next_second()
            session.start(profile, 'four_point', 'wafer7', 'alice',
                          spot={'map_id': MAP_ID, 'index': index, 'label': label})
            assert _wait_for(lambda: session.state == 'idle')
            ended = sink.of_type('run_ended')[-1].payload
            assert ended['reason'] == 'target_samples'
            paths.append(ended['path'])
    finally:
        session.close(timeout=5.0)
    assert len(set(paths)) == 4
    return sink, paths


def test_three_spots_and_a_repeat_make_a_map(measured_map):
    sink, paths = measured_map
    completes = [e.payload for e in sink.of_type('spot_complete')]
    assert len(completes) == 4

    # Every file says which spot of which map it is ...
    for (index, label, _), path in zip(PLACEMENTS, paths):
        header = parse_metadata(path, text_keys=('spot.map_id', 'spot.label'))
        assert header['spot.map_id'] == MAP_ID
        assert header['spot.index'] == index
        assert header['spot.label'] == label
        assert header['spot.sample.shape'] == 'unbounded'

    # ... and ends with the statistics of its own rows, which are also what
    # the spot_complete event carried.
    footer_key = {'Rs_ohm_sq': 'rs', 'rho_ohm_cm': 'rho', 'sigma_S_cm': 'sigma'}
    for path, complete in zip(paths, completes):
        footer = parse_metadata(path)
        assert complete['path'] == path
        assert footer['spot_stats.n'] == SAMPLES == complete['stats']['n']
        for column, values in _rows(path).items():
            name = footer_key[column]
            mean, sd = _mean_sd(values)
            assert footer[f'spot_stats.{name}.n'] == SAMPLES
            # Rows are written to six significant figures; the footer is not.
            assert footer[f'spot_stats.{name}.mean'] == pytest.approx(mean, rel=1e-5)
            assert footer[f'spot_stats.{name}.sd'] == pytest.approx(sd, rel=2e-2)
            assert footer[f'spot_stats.{name}.u_stat'] == pytest.approx(
                sd / math.sqrt(SAMPLES), rel=2e-2)
            for field in ('mean', 'sd', 'rsd_pct', 'u_stat', 'u_inst', 'u_total'):
                assert complete['stats'][name][field] == footer[f'spot_stats.{name}.{field}']
        assert footer['spot_stats.rs.sd'] > 0          # the simulator's noise
        assert footer['spot_stats.rs.u_inst'] > 0

    # The map: three spots, and the repeat stands for spot 2.
    directory = Path(paths[0]).parent
    found = assemble_map(directory, MAP_ID)
    assert [spot.index for spot in found.spots] == [1, 2, 3]
    spot_two = found.spots[1]
    assert spot_two.label == 'north again'
    assert spot_two.file == Path(paths[3]).name
    assert spot_two.superseded == [Path(paths[1]).name]
    assert found.skipped == []

    # Inter-spot spread, by hand, from the three means that stand.
    means = [completes[0]['stats']['rs']['mean'], completes[3]['stats']['rs']['mean'],
             completes[2]['stats']['rs']['mean']]
    mean = sum(means) / 3
    sd = math.sqrt(sum((m - mean) ** 2 for m in means) / 2)
    assert found.rs.n == 3
    assert found.rs.mean == pytest.approx(mean, rel=1e-12)
    assert found.rs.sd == pytest.approx(sd, rel=1e-12)
    assert found.rs.rsd_pct == pytest.approx(sd / mean * 100.0, rel=1e-12)
    # And against the DUTs: 100, 110 and 120 ohm have an RSD of 100/11 %.
    assert found.rs.mean == pytest.approx(4.532 * 110.0, rel=5e-3)
    assert found.rs.rsd_pct == pytest.approx(100.0 / 11.0, rel=2e-2)


def test_the_map_summary_is_written_beside_the_runs(measured_map):
    sink, paths = measured_map
    directory = Path(paths[0]).parent
    summary_path = directory / f'{MAP_ID}_map.json'
    assert summary_path.exists()

    summary = json.loads(summary_path.read_text())
    assert summary['map_id'] == MAP_ID
    assert [spot['index'] for spot in summary['spots']] == [1, 2, 3]
    assert summary['spots'][1]['file'] == Path(paths[3]).name
    assert summary == json.loads(assemble_map(directory, MAP_ID).model_dump_json())
    # One log line per completed spot, and nothing left behind by the writes.
    written = [e for e in sink.of_type('log') if e.payload['code'] == 'map_summary']
    assert len(written) == 4
    assert [p.name for p in directory.iterdir() if p.name.endswith('.tmp')] == []
    assert all(Path(path).exists() for path in paths)


def test_a_run_without_a_spot_writes_no_summary(profile):
    from resistamet_gui.simulator import enable_simulation

    enable_simulation(dut_resistance_ohms=100.0, model="2420")
    sink = ListSink()
    session = MeasurementSession(sink)
    try:
        session.start(profile, 'four_point', 'wafer7', 'alice')
        assert _wait_for(lambda: session.state == 'idle')
    finally:
        session.close(timeout=5.0)
    directory = Path(sink.of_type('run_ended')[0].payload['path']).parent
    assert [p.name for p in directory.iterdir() if p.name.endswith('.json')] == []

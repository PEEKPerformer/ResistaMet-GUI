"""Properties of a spot's statistics and of a map assembled from run files.

``test_session_spot_stats.py`` and ``test_session_spot_map.py`` work through
cases by hand. Here the series and the directories are drawn:

* the statistics of a spot equal numpy's over the samples that count --
  taken outside compliance, finite -- whatever else was recorded beside
  them, and do not depend on the order of the samples;
* a map does not depend on the order its files were written in or on what
  the sample was called; the newest run of an index stands for the spot, by
  the stamp in its name and then by the ``-N`` the exporter adds, and asking
  twice gives the same answer;
* a data directory also holds other things. Truncated files, binary noise,
  directories named like runs and headers somebody edited must cost the map
  at most those runs, never an exception.

Every file is written under ``tmp_path``. The run is derandomised and keeps
no example database.
"""

import gzip
import itertools
import math
import random

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from resistamet_gui.data_export import CsvExporter
from resistamet_gui.session.run_files import MODE_FILE_TAGS
from resistamet_gui.session.spot_map import SpotMap, assemble_map, inter_spot_statistics, list_map_ids
from resistamet_gui.session.spot_stats import QUANTITIES, SpotSamples, spot_statistics

# No deadline: the map examples write files, and file-system latency on a
# shared CI runner is not the property. tmp_path is function-scoped, which
# hypothesis flags; each example takes a directory of its own inside it.
PROPERTY = settings(max_examples=60, deadline=None, database=None, derandomize=True,
                    suppress_health_check=[HealthCheck.function_scoped_fixture])

TAG = MODE_FILE_TAGS["four_point"]
_counter = itertools.count()


def _fresh_dir(tmp_path):
    directory = tmp_path / f"d{next(_counter)}"
    directory.mkdir()
    return directory


def _magnitude(low_exp, high_exp):
    return st.floats(low_exp, high_exp).map(lambda e: 10.0 ** e)


not_numbers = st.sampled_from([None, "", "abc", float("nan"), float("inf"), -float("inf"), [], {}])
derived_value = st.one_of(_magnitude(-6.0, 9.0), not_numbers)
sample_rows = st.lists(st.tuples(
    _magnitude(-6.0, 1.0), _magnitude(-7.0, -1.0), derived_value, derived_value, derived_value,
    st.sampled_from(["OK", "OK", "OK", "Voltage", "Current"])), max_size=25)


def _finite(value) -> bool:
    return isinstance(value, float) and math.isfinite(value)


def _close(actual, expected) -> bool:
    if math.isnan(expected):
        return math.isnan(actual)
    return actual == pytest.approx(expected, rel=1e-9, abs=1e-300)


class TestSpotStatistics:
    @PROPERTY
    @given(sample_rows, st.randoms(use_true_random=False))
    def test_equal_numpy_over_the_samples_that_count(self, rows, rnd):
        samples = SpotSamples()
        for v, i, rs, rho, sigma, compliance in rows:
            samples.add(v, i, {"rs": rs, "rho": rho, "sigma": sigma}, compliance)
        stats = spot_statistics(samples)
        kept = [row for row in rows if row[5] == "OK"]
        assert stats["n"] == len(kept) == len(samples)
        assert stats["n_excluded"] == len(rows) - len(kept)
        assert len({len(getattr(samples, column)) for column in ("voltage", "current") + QUANTITIES}) == 1

        for position, name in enumerate(QUANTITIES, start=2):
            values = np.array([row[position] for row in kept if _finite(row[position])], dtype=float)
            got = stats[name]
            assert got["n"] == len(values)
            if len(values) == 0:
                assert all(math.isnan(got[key]) for key in ("mean", "sd", "rsd_pct", "u_stat", "u_inst", "u_total"))
                continue
            mean = float(np.mean(values))
            sd = float(np.std(values, ddof=1)) if len(values) > 1 else float("nan")
            assert _close(got["mean"], mean)
            assert _close(got["sd"], sd)
            assert _close(got["rsd_pct"], sd / abs(mean) * 100.0)
            assert _close(got["u_stat"], sd / math.sqrt(len(values)) if len(values) > 1 else 0.0)
            assert got["u_inst"] > 0
            assert _close(got["u_total"], math.hypot(got["u_stat"], got["u_inst"]))

        # The order the samples arrived in is not information.
        shuffled = SpotSamples()
        order = list(rows)
        rnd.shuffle(order)
        for v, i, rs, rho, sigma, compliance in order:
            shuffled.add(v, i, {"rs": rs, "rho": rho, "sigma": sigma}, compliance)
        again = spot_statistics(shuffled)
        for name in QUANTITIES:
            for key, value in stats[name].items():
                assert _close(float(again[name][key]), float(value))

    @PROPERTY
    @given(st.lists(st.tuples(not_numbers, not_numbers,
                              st.one_of(not_numbers, st.dictionaries(st.sampled_from(QUANTITIES), not_numbers)),
                              st.sampled_from(["OK", "Voltage", "", None])), max_size=8))
    def test_anything_can_be_recorded_and_nothing_raises(self, rows):
        samples = SpotSamples()
        for v, i, derived, compliance in rows:
            samples.add(v, i, derived, compliance)
        stats = spot_statistics(samples)
        assert stats["n"] + stats["n_excluded"] == len(rows)
        assert all(stats[name]["n"] == 0 for name in QUANTITIES)


class TestInterSpotStatistics:
    @PROPERTY
    @given(st.lists(st.one_of(_magnitude(-6.0, 9.0), st.none(), st.just(float("nan")), st.just(float("inf"))),
                    max_size=12), st.randoms(use_true_random=False))
    def test_equals_numpy_over_the_finite_means(self, means, rnd):
        values = np.array([m for m in means if m is not None and math.isfinite(m)], dtype=float)
        got = inter_spot_statistics(means)
        assert got.n == len(values)
        if len(values) == 0:
            assert got.mean is None and got.sd is None and got.rsd_pct is None
            return
        assert got.mean == pytest.approx(float(np.mean(values)), rel=1e-12)
        if len(values) < 2:
            assert got.sd is None and got.rsd_pct is None
        else:
            sd = float(np.std(values, ddof=1))
            assert got.sd == pytest.approx(sd, rel=1e-9, abs=1e-300)
            assert got.rsd_pct == pytest.approx(sd / abs(got.mean) * 100.0, rel=1e-9, abs=1e-300)
        shuffled = list(means)
        rnd.shuffle(shuffled)
        again = inter_spot_statistics(shuffled)
        assert again.n == got.n and again.mean == pytest.approx(got.mean, rel=1e-12)


def _quantity(mean, n):
    return {"n": n, "mean": mean, "sd": mean * 0.01, "rsd_pct": 1.0,
            "u_stat": mean * 0.004, "u_inst": mean * 0.001, "u_total": mean * 0.0042}


def _write_run(directory, stamp, sample, index, rs_mean, *, map_id="m1", kind="good", gz=False):
    """One run file as ContinuousRun leaves it. ``kind``: 'good', 'unfinished'
    (no footer) or 'empty' (a footer with no valid sample)."""
    meta = {"user": "op", "sample": sample, "mode": "four_point",
            "started_at": "2026-09-19T12:00:00",
            "spot": {"map_id": map_id, "index": index, "label": f"spot {index}"}}
    exporter = CsvExporter(directory / f"{stamp}_{sample}_{TAG}_1mA", meta, ["elapsed_s", "Rs"],
                           compression="always" if gz else "never")
    exporter.write_row([0.0, rs_mean])
    if kind == "unfinished":
        exporter.flush()
        exporter._csv_file.close()
        return exporter.csv_path.name
    n = 0 if kind == "empty" else 5
    exporter.finalize({"total_samples": n, "spot_stats": {
        "n": n, "n_excluded": 0,
        "rs": _quantity(rs_mean, n) if n else {"n": 0, "mean": float("nan")},
        "rho": {"n": 0, "mean": float("nan")}, "sigma": {"n": 0, "mean": float("nan")}}})
    return exporter.output_paths[0].name


runs = st.lists(st.tuples(
    st.integers(0, 3),                                   # spot index
    st.sampled_from(["wafer", "a", "zz_top", "B-2"]),    # sample: changes the name's sort order
    _magnitude(-3.0, 6.0),                               # Rs mean
    st.sampled_from(["good", "good", "good", "unfinished", "empty"]),
    st.booleans(),                                       # gzipped
), min_size=1, max_size=8)


class TestMapAssembly:
    @PROPERTY
    @given(runs, st.randoms(use_true_random=False))
    def test_the_newest_good_run_of_each_index_whatever_the_writing_order(self, tmp_path, drawn, rnd):
        # Distinct stamps, dealt at random so that age is unrelated to the
        # index, to the sample's name and to the order of writing.
        stamps = list(range(1000, 1000 + len(drawn)))
        rnd.shuffle(stamps)
        planned = [(stamp,) + run for stamp, run in zip(stamps, drawn)]

        def write_all(directory, order):
            names = {}
            for stamp, index, sample, rs, kind, gz in order:
                names[stamp] = _write_run(directory, stamp, sample, index, rs, kind=kind, gz=gz)
            return names

        first, second = _fresh_dir(tmp_path), _fresh_dir(tmp_path)
        names = write_all(first, planned)
        reordered = list(planned)
        rnd.shuffle(reordered)
        write_all(second, reordered)

        found = assemble_map(first, "m1")
        assert found.model_dump_json() == assemble_map(second, "m1").model_dump_json()
        assert found.model_dump_json() == assemble_map(first, "m1").model_dump_json()

        good = {}
        for stamp, index, sample, rs, kind, gz in planned:
            if kind == "good":
                good.setdefault(index, []).append((stamp, rs))
        assert [spot.index for spot in found.spots] == sorted(good)
        for spot in found.spots:
            newest_first = sorted(good[spot.index], reverse=True)
            assert spot.file == names[newest_first[0][0]]
            assert spot.stats.rs.mean == pytest.approx(newest_first[0][1], rel=1e-5)   # .6g in the file
            assert spot.superseded == [names[stamp] for stamp, _ in newest_first[1:]]
        assert sorted(run.file for run in found.skipped) == sorted(
            names[stamp] for stamp, _, _, _, kind, _ in planned if kind != "good")
        # Every file is somewhere, once.
        everywhere = ([s.file for s in found.spots] + [f for s in found.spots for f in s.superseded]
                      + [run.file for run in found.skipped])
        assert sorted(everywhere) == sorted(names.values())
        means = [spot.stats.rs.mean for spot in found.spots]
        assert found.rs.n == len(means)
        if means:
            assert found.rs.mean == pytest.approx(float(np.mean(means)), rel=1e-12)
        assert list_map_ids(first) == (["m1"] if planned else [])

    @PROPERTY
    @given(st.integers(2, 12), st.integers(0, 3))
    def test_a_tie_goes_to_the_run_written_last(self, tmp_path, count, index):
        """Same second, same start time: the exporter names the later files
        ``-2``, ``-3`` ... and those numbers decide, ``-10`` after ``-9``."""
        directory = _fresh_dir(tmp_path)
        names = [_write_run(directory, 1000, "wafer", index, float(k + 1)) for k in range(count)]
        found = assemble_map(directory, "m1")
        assert [spot.file for spot in found.spots] == [names[-1]]
        assert found.spots[0].stats.rs.mean == float(count)
        assert found.spots[0].superseded == names[-2::-1]
        assert assemble_map(directory, "m1").model_dump_json() == found.model_dump_json()


_VALUES = ["abc", "", "nan", "Infinity", "-Infinity", "1e400", "-1", "0", "2", "2.5", "{[]}", "[1]", "None",
           "true", "'q'", "9" * 40, "1_0", "0x10", "1j", "(1, 2)", "../x", "m 1", "0001-01-01T00:00:00",
           "9999-12-31T23:59:59+14:00", "2026-13-45"]
_HEAD_KEYS = ["mode", "spot.map_id", "spot.index", "spot.label", "spot.x_mm", "spot.y_mm", "spot.angle_deg",
              "spot.relative_error", "started_at", "sample"]
_FOOT_KEYS = ["spot_stats.n", "spot_stats.n_excluded", "spot_stats.end_reason", "spot_stats.rs.n",
              "spot_stats.rs.mean", "spot_stats.rs.sd", "spot_stats.rho.n", "spot_stats.rho.mean",
              "spot_stats.sigma.n", "spot_stats.sigma.bogus"]


def _edited_header(rng: random.Random) -> str:
    """A header and footer somebody has been at: mostly plausible, not quite."""
    lines = ["# resistamet_format_version: 2.0"]
    for key in _HEAD_KEYS:
        if rng.random() < 0.85:
            likely = {"mode": "four_point", "spot.map_id": "m1", "spot.index": str(rng.randrange(4))}.get(key)
            lines.append(f"# {key}: {likely if likely and rng.random() < 0.75 else rng.choice(_VALUES)}")
    lines += ["elapsed_s,Rs", "0.0,1.0"]
    if rng.random() < 0.8:
        lines.append("# --- run completed ---")
        for key in _FOOT_KEYS:
            if rng.random() < 0.8:
                lines.append(f"# {key}: {rng.choice(['5', '1', '3.5']) if rng.random() < 0.6 else rng.choice(_VALUES)}")
    return "\n".join(lines) + "\n"


class TestJunkInTheDataDirectory:
    @PROPERTY
    @given(st.integers(0, 2 ** 32 - 1), st.integers(0, 3))
    def test_never_raises_and_never_loses_a_good_run(self, tmp_path, seed, good_runs):
        rng = random.Random(seed)
        directory = _fresh_dir(tmp_path)
        good = [_write_run(directory, 5000 + k, "wafer", 10 + k, 100.0 + k) for k in range(good_runs)]
        for _ in range(rng.randrange(1, 7)):
            stamp = rng.choice(["1000", "1001", "", "9" * 30, "x"])
            name = (f"{stamp}_{rng.choice(['s', 'a_b', ''])}_{TAG}_1mA"
                    f"{rng.choice(['', '-2', '-10', '-x'])}{rng.choice(['.csv', '.csv.gz', '.h5'])}")
            path = directory / name
            if path.exists():
                continue
            kind = rng.random()
            if kind < 0.1:
                path.mkdir()
            elif kind < 0.3:
                path.write_bytes(bytes(rng.randrange(256) for _ in range(rng.randrange(0, 200))))
            elif kind < 0.4:
                path.write_bytes(_edited_header(rng).encode("utf-8")[:rng.randrange(0, 120)])
            elif name.endswith(".gz") and rng.random() < 0.7:
                with gzip.open(path, "wt", encoding="utf-8") as handle:
                    handle.write(_edited_header(rng))
            else:
                path.write_text(_edited_header(rng), encoding="utf-8")

        found = assemble_map(directory, "m1")
        assert isinstance(found, SpotMap)
        assert isinstance(list_map_ids(directory), list)
        by_index = {spot.index: spot for spot in found.spots}
        for k, name in enumerate(good):
            # Indices 10.. belong to the good runs alone, and their stamps
            # are the newest in the directory bar the absurd one.
            assert name in [by_index[10 + k].file] + by_index[10 + k].superseded
        files = [s.file for s in found.spots] + [f for s in found.spots for f in s.superseded]
        assert len(files) == len(set(files))
        assert all(math.isfinite(s.index) for s in found.spots)

    @pytest.mark.xfail(strict=True, raises=OverflowError, reason=(
        "assemble_map validates each run inside a guard so that an edited header "
        "costs the map that run only, but the spread between spots is computed "
        "outside it: inter_spot_statistics squares (mean - grand mean) with `**`, "
        "and one footer edited to `spot_stats.rs.mean: 1e200` raises OverflowError "
        "for the whole map (spot_map.py, inter_spot_statistics)."))
    def test_one_absurd_mean_does_not_cost_the_whole_map(self, tmp_path):
        directory = _fresh_dir(tmp_path)
        _write_run(directory, 1000, "wafer", 0, 100.0)
        _write_run(directory, 1001, "wafer", 1, 1e200)
        assert isinstance(assemble_map(directory, "m1"), SpotMap)

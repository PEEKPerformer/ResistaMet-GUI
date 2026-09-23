"""Round-trip and injection properties of the v2 CSV header.

``test_data_export_v2.py`` writes known metadata and reads it back. Here the
metadata is drawn: nested dicts whose values are the awkward ones -- text with
colons, hashes, Unicode and outer spaces, text that looks like a number,
NaN, infinities, integers of a thousand digits -- written by ``CsvExporter``
and read by ``parse_metadata``. The header is ``# key: value`` lines, so the
second half asks the question that format invites: can anything a person
types (a sample name, a spot label, a mark) come back as a header key nobody
wrote?

Every file is written under ``tmp_path``. The run is derandomised and keeps
no example database.
"""

import csv
import itertools
import math
import string
from datetime import datetime

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from resistamet_gui.data_export import (
    CsvExporter, _format_scalar, _parse_scalar, build_metadata, parse_metadata,
)
from resistamet_gui.schema.settings_modes import RunRequest
from resistamet_gui.schema.spots import SpotRequest

# No deadline: each example creates a file, and file-system latency on a
# shared CI runner is not what is being tested. tmp_path is function-scoped,
# which hypothesis flags; every example takes its own file name instead.
PROPERTY = settings(max_examples=120, deadline=None, database=None, derandomize=True,
                    suppress_health_check=[HealthCheck.function_scoped_fixture])

_counter = itertools.count()


def _fresh(tmp_path):
    return tmp_path / f"run_{next(_counter)}"


def _same(a, b) -> bool:
    """Equal in value *and* type, with NaN equal to NaN."""
    if type(a) is not type(b):
        return False
    if isinstance(a, float):
        return (math.isnan(a) and math.isnan(b)) or (a == b and math.copysign(1, a) == math.copysign(1, b))
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


numbers = st.one_of(
    st.booleans(),
    st.none(),
    st.integers(-2 ** 4000, 2 ** 4000),
    st.floats(allow_nan=True, allow_infinity=True),
)
#: Text that stays on its line under every reader: no line boundary of any
#: kind (``str.splitlines`` knows more of them than ``open`` does).
_LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85  "
one_line_text = st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters=_LINE_BREAKS), max_size=40)
#: The strings most likely to be mistaken for something else.
tricky_text = st.sampled_from([
    "12_3", "007", "1e5", "0x10", "1j", "true", "false", "NaN", "Infinity", "-Infinity", "None", "",
    "[1, 2]", "(1,)", "'quoted'", "a: b", "key: value: more", "# not a comment", "#", ":", " padded ",
    "--- dashes", "units", "µΩ·cm", "Ω/□", "样品 7", "naïve café", "tab\there", "{'a': 1}",
])
texts = st.one_of(one_line_text, tricky_text)
keys = st.text(string.ascii_lowercase + string.digits + "_", min_size=1, max_size=12).filter(
    lambda k: k not in ("units", "resistamet_format_version"))


class TestScalars:
    @PROPERTY
    @given(numbers)
    def test_numbers_booleans_and_none_survive_exactly(self, value):
        assert _same(_parse_scalar(_format_scalar(value)), value)

    @PROPERTY
    @given(st.lists(st.one_of(st.booleans(), st.none(), st.integers(), st.floats(allow_nan=False, allow_infinity=False),
                              st.text(string.ascii_letters + " _-", max_size=8)), max_size=6))
    def test_lists_of_plain_values_survive(self, value):
        assert _same(_parse_scalar(_format_scalar(value)), value)

    @PROPERTY
    @given(st.text(max_size=200))
    def test_any_text_parses_to_something(self, text):
        """A header is read long after it was written, possibly by another
        version: a value that is not a literal must come back as text, not as
        an exception. (Set and dict displays are the exception; see below.)"""
        if "{" in text:
            return
        _parse_scalar(text)
        assert _format_scalar(text) == text

    @pytest.mark.xfail(strict=True, raises=TypeError, reason=(
        "_parse_scalar catches ValueError and SyntaxError from ast.literal_eval, "
        "but a set or dict display with an unhashable member -- '{[]}', '{{1}}' -- "
        "raises TypeError, and parse_metadata with it. Any header value read "
        "without text_keys can do this (data_export.py, _parse_scalar)."))
    @pytest.mark.parametrize("text", ["{[]}", "{{1}}", "{[1]: 2}", "{{}}"])
    def test_a_display_with_an_unhashable_member_comes_back_as_text(self, text):
        assert _parse_scalar(text) == text


def _nested(leaves):
    return st.dictionaries(keys, st.one_of(leaves, st.dictionaries(keys, st.one_of(
        leaves, st.dictionaries(keys, leaves, max_size=3)), max_size=3)), max_size=6)


def _flat(meta, prefix=""):
    for key, value in meta.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            yield from _flat(value, name)
        else:
            yield name, value


cells = st.one_of(st.floats(allow_nan=True, allow_infinity=True), st.integers(-10 ** 9, 10 ** 9),
                  st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters=_LINE_BREAKS + "\x00"),
                          max_size=12))


class TestFileRoundTrip:
    @PROPERTY
    @given(_nested(st.one_of(numbers, texts)), _nested(st.one_of(numbers, texts)),
           st.lists(st.lists(cells, min_size=3, max_size=3), max_size=5), st.sampled_from(["never", "always"]))
    def test_metadata_written_is_metadata_read(self, tmp_path, head, tail, rows, compression):
        # Footer keys that repeat a header key lose to it (first one wins), so
        # give the footer its own namespace, as the real footer has.
        tail = {"end": tail} if tail else {}
        exporter = CsvExporter(_fresh(tmp_path), head, ["elapsed_s", "value", "event"], ["s", "V", ""],
                               compression=compression)
        for row in rows:
            # A first cell that starts with '#' would be a comment line to any
            # reader; the first column of a real row is a number.
            exporter.write_row([0.5] + row[1:])
        exporter.finalize(tail)
        (path,) = exporter.output_paths
        assert path.name.endswith(".csv.gz" if compression == "always" else ".csv")

        written = dict(_flat(head))
        written.update(_flat(tail))
        text_keys = [k for k, v in written.items() if isinstance(v, str)]
        read = parse_metadata(path, text_keys=text_keys)

        assert read.pop("resistamet_format_version") == 2.0
        assert read.pop("units") == ["s", "V", ""]
        assert set(read) == set(written)
        for key, value in written.items():
            if isinstance(value, str):
                # The reader strips a value; nothing else may change.
                assert read[key] == value.strip(), key
            else:
                assert _same(read[key], value), key

        # Without the escape hatch a string is coerced exactly as
        # _parse_scalar coerces it, and never raises on the way.
        if not any("{" in v for v in written.values() if isinstance(v, str)):
            coerced = parse_metadata(path)
            for key in text_keys:
                assert _same(coerced[key], _parse_scalar(written[key].strip())), key

    @PROPERTY
    @given(st.lists(st.lists(cells, min_size=2, max_size=2), min_size=1, max_size=6))
    def test_rows_written_are_rows_read(self, tmp_path, rows):
        exporter = CsvExporter(_fresh(tmp_path), {"sample": "s"}, ["elapsed_s", "a", "b"])
        for index, row in enumerate(rows):
            exporter.write_row([float(index)] + row)
        exporter.finalize({"total_samples": len(rows)})
        assert exporter.row_count == len(rows)
        with open(exporter.output_paths[0], newline="", encoding="utf-8") as handle:
            lines = [line for line in csv.reader(handle) if not (line and line[0].startswith("#"))]
        assert lines[0] == ["elapsed_s", "a", "b"]
        assert len(lines) == len(rows) + 1
        for line, (index, row) in zip(lines[1:], enumerate(rows)):
            assert float(line[0]) == float(index)
            for cell, value in zip(line[1:], row):
                if isinstance(value, float):
                    # Six significant figures: what the file promises.
                    expected = float(f"{value:.6g}")
                    assert (math.isnan(expected) and math.isnan(float(cell))) or float(cell) == expected
                else:
                    assert cell == str(value)
        assert parse_metadata(exporter.output_paths[0])["total_samples"] == len(rows)


def _accepted(model, **fields):
    try:
        return model(**fields)
    except ValidationError:
        return None


_SETTINGS = {"measurement": {"gpib_address": "GPIB0::24::INSTR", "fpp_current": 1e-3}}
_SPOT = {"map_id": "wafer7", "index": 3, "label": "centre", "x_mm": 1.0, "y_mm": 2.0}
_FORGERIES = ["# spot.map_id: forged", "# spot.index: 99", "# forged_key: 1", "# ended_at: never"]


def _header_of(tmp_path, sample, spot):
    meta = build_metadata("operator", sample, "four_point", _SETTINGS, "KEITHLEY,MODEL 2400",
                          datetime(2026, 9, 19, 12, 0, 0), spot=spot)
    exporter = CsvExporter(_fresh(tmp_path), meta, ["elapsed_s", "V", "event"])
    exporter.write_row([0.0, 1.0, ""])
    exporter.finalize({"total_samples": 1})
    expected = {"resistamet_format_version", *dict(_flat(meta)), "total_samples"}
    return parse_metadata(exporter.output_paths[0], text_keys=("spot.map_id", "spot.label", "sample")), expected


adversarial_text = st.builds(
    lambda before, breaker, forged, after: before + breaker + forged + after,
    st.text(max_size=10), st.sampled_from(["\n", "\r", "\r\n", " ", "\x85", "\x0c", " ", ": ", "\\n"]),
    st.sampled_from(_FORGERIES), st.text(max_size=5))


class TestHeaderInjection:
    """What a client can put in a request must not become a header key."""

    @PROPERTY
    @given(st.one_of(adversarial_text, st.text(min_size=1, max_size=80)))
    def test_a_spot_label_the_schema_accepts_cannot_forge_a_key(self, tmp_path, label):
        spot = _accepted(SpotRequest, **{**_SPOT, "label": label})
        if spot is None:
            return                      # refused at the door, which is an answer too
        read, expected = _header_of(tmp_path, "sample", spot.model_dump())
        assert set(read) == expected
        assert read["spot.map_id"] == "wafer7" and read["spot.index"] == 3
        assert read["spot.label"] == spot.label

    @PROPERTY
    @given(st.one_of(adversarial_text, st.text(min_size=1, max_size=60)))
    def test_a_one_line_sample_name_cannot_forge_a_key(self, tmp_path, sample):
        request = _accepted(RunRequest, mode="four_point", username="operator", sample_name=sample)
        if request is None or "\n" in sample or "\r" in sample:
            return                      # line breaks: the test below
        read, expected = _header_of(tmp_path, request.sample_name, dict(_SPOT))
        assert set(read) == expected
        assert read["spot.map_id"] == "wafer7" and read["spot.index"] == 3
        assert read["sample"] == sample.strip()

    @pytest.mark.parametrize("breaker", ["\n", "\r", "\r\n"])
    @pytest.mark.parametrize("forged", _FORGERIES[:3])
    def test_a_sample_name_with_a_line_break_cannot_forge_a_key(self, tmp_path, breaker, forged):
        # RunRequest refuses this name (test_settings_models); the PySide6
        # path hands the typed name to build_metadata without it.
        sample = "x" + breaker + forged
        read, expected = _header_of(tmp_path, sample, dict(_SPOT))
        assert set(read) == expected
        assert read["spot.map_id"] == "wafer7" and read["spot.index"] == 3
        assert read["sample"] == "x\\n" + forged

    @pytest.mark.xfail(strict=True, reason=(
        "A mark label is any string (api.routes_session.MarkRequest) and lands in "
        "the event cell. csv quotes a cell with a line break but still writes the "
        "break, so the cell's second line starts with '#'; on the last row it sits "
        "directly above the footer and parse_metadata's tail pass absorbs it as a "
        "key. A run with no spot can be given spot.map_id and spot.index this way."))
    def test_a_mark_with_a_line_break_cannot_forge_a_footer_key(self, tmp_path):
        exporter = CsvExporter(_fresh(tmp_path), {"sample": "s"}, ["elapsed_s", "V", "event"])
        exporter.write_row([0.0, 1.0, ""])
        exporter.write_row([0.1, 1.0, "MARK\n# spot.map_id: forged"])
        exporter.finalize({"total_samples": 2})
        read = parse_metadata(exporter.output_paths[0])
        assert "spot.map_id" not in read

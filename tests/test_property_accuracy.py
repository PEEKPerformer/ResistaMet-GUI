"""Properties of the Keithley accuracy tables and the range inference.

``test_accuracy.py`` reproduces the datasheet's and the manual's worked
examples. This module checks the structure around them, for every model and
every table at once: the tables are well formed, the 105 % overrange rule
picks the range it says it picks, an uncertainty is positive, affine inside a
range and never improves when the instrument ranges up, and nothing but a
number or NaN comes back whatever is passed in.

Where a table breaks one of these, the case is a strict xfail that names the
row. None of the expected numbers here come from the datasheet: they are
consistency conditions between the tables and the code's own docstrings.

The run is derandomised and keeps no example database.
"""

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from resistamet_gui import accuracy as acc
from resistamet_gui import instrument

# No deadline: nothing here is about speed, and shared CI runners make the
# per-example clock flaky. max_examples bounds the run.
PROPERTY = settings(max_examples=200, deadline=None, database=None, derandomize=True)

NAN = float("nan")
INF = float("inf")

#: table name -> (lookup, public function, kind for the NPLC modifier or None)
TABLES = {
    "v_measure": (acc._V_MEASURE, acc.voltage_uncertainty, "voltage"),
    "i_measure": (acc._I_MEASURE, acc.current_uncertainty, "current"),
    "v_source": (acc._V_SOURCE, acc.voltage_source_uncertainty, None),
    "i_source": (acc._I_SOURCE, acc.current_source_uncertainty, None),
}
MODELS = acc.known_models()
NPLCS = (0.01, 0.1, 1.0, 10.0)


def _cases(xfail=None):
    """Every (table, model), with the known-bad ones marked."""
    xfail = xfail or {}
    out = []
    for table in TABLES:
        for model in MODELS:
            reason = xfail.get((table, model))
            marks = [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
            out.append(pytest.param(table, model, marks=marks, id=f"{table}-{model}"))
    return out


def _magnitude(low_exp: float, high_exp: float):
    return st.floats(low_exp, high_exp).map(lambda e: 10.0 ** e)


def _signed(low_exp: float, high_exp: float):
    return st.builds(lambda m, neg: -m if neg else m, _magnitude(low_exp, high_exp), st.booleans())


any_reading = st.one_of(
    st.sampled_from([0.0, -0.0, NAN, INF, -INF, 9.9e37, -9.9e37]),
    _signed(-15.0, 38.0),
)
table_names = st.sampled_from(sorted(TABLES))
models = st.sampled_from(MODELS + ("2450", "bogus", ""))


_DUPLICATE_1A = (
    "_I_SRC_2440 is `_I_SRC_2400[1:] + (1 A row, 5 A row)`, and the slice "
    "already ends with the 2400's 1 A row, so the table has two 1 A ranges. "
    "_pick_range returns the first: the 2440's own 0.067 % + 900 uA row is "
    "unreachable and a 2440 sourcing 1 A is given the 2400's 0.27 %.")


class TestTablesAreWellFormed:
    def test_every_table_names_the_same_models(self):
        for lookup in (acc._V_MEASURE, acc._I_MEASURE, acc._V_SOURCE, acc._I_SOURCE, acc._R_ENHANCED):
            assert tuple(sorted(lookup)) == MODELS
        assert acc._DEFAULT_MODEL in MODELS

    @pytest.mark.parametrize("table, model", _cases({("i_source", "2440"): _DUPLICATE_1A}))
    def test_ranges_ascend_without_repeats(self, table, model):
        """``_pick_range`` requires ascending order; a repeated full scale
        makes the later row dead."""
        specs = TABLES[table][0][model]
        maxima = [spec.range_max for spec in specs]
        assert all(low < high for low, high in zip(maxima, maxima[1:]))

    @pytest.mark.parametrize("table, model", _cases())
    def test_rows_are_positive_and_small(self, table, model):
        for spec in TABLES[table][0][model]:
            assert spec.range_max > 0
            assert 0 < spec.pct_reading < 0.01        # a fraction, not a percentage
            assert 0 < spec.offset < 0.01 * spec.range_max

    @pytest.mark.parametrize("model", [
        pytest.param(m, marks=[pytest.mark.xfail(strict=True, reason=_DUPLICATE_1A)] if m == "2440" else [])
        for m in MODELS])
    def test_measure_and_source_tables_have_the_same_ranges(self, model):
        for measure, source in ((acc._V_MEASURE, acc._V_SOURCE), (acc._I_MEASURE, acc._I_SOURCE)):
            assert [s.range_max for s in measure[model]] == [s.range_max for s in source[model]]

    def test_enhanced_resistance_ranges_ascend(self):
        maxima = [spec.range_max for spec in acc._R_ENH_2400]
        assert all(low < high for low, high in zip(maxima, maxima[1:]))


class TestAgainstTheInstrumentTable:
    """``accuracy`` says it mirrors ``instrument._MODELS``."""

    @pytest.mark.parametrize("model", [
        pytest.param(m, marks=[pytest.mark.xfail(strict=True, reason=(
            "instrument._MODELS knows the 2450 and detect_model returns it, but no "
            "accuracy table does: every *_uncertainty call falls back silently to the "
            "2400's rows, and the result is written to the file as the instrument's."))]
            if m == "2450" else [])
        for m in instrument.known_models()])
    def test_every_detectable_model_has_tables(self, model):
        assert model in MODELS

    @pytest.mark.parametrize("model", [
        pytest.param(m, marks=[pytest.mark.xfail(strict=True, reason=(
            "ModelSpec gives the 2425 and 2430 a 100 V maximum, but their voltage tables "
            "are the 2420's and stop at 60 V, so anything above 63 V is clamped onto the "
            "60 V row. One of instrument._MODELS and accuracy._V_MEASURE/_V_SOURCE is wrong."))]
            if m in ("2425", "2430") else [])
        for m in MODELS])
    def test_the_top_range_reaches_the_models_maximum(self, model):
        """A Keithley range sources at most 110 % of its full scale (the
        2410's 1000 V range reaches 1100 V)."""
        spec = instrument._MODELS[model]
        for lookup in (acc._V_MEASURE, acc._V_SOURCE):
            assert lookup[model][-1].range_max * 1.1 >= spec.max_source_v
        for lookup in (acc._I_MEASURE, acc._I_SOURCE):
            assert lookup[model][-1].range_max * 1.1 >= spec.max_source_i


def _expected_range(value: float, specs) -> float:
    """The 105 % rule, restated: the smallest range whose full scale, plus
    5 %, holds the reading; the top range when none does."""
    for spec in specs:
        if abs(value) <= spec.range_max * 1.05 * (1 + 1e-12):
            return spec.range_max
    return specs[-1].range_max


class TestRangeSelection:
    @PROPERTY
    @given(table_names, st.sampled_from(MODELS), _signed(-12.0, 4.0), _signed(-12.0, 4.0))
    def test_monotone_in_the_magnitude_and_blind_to_the_sign(self, table, model, a, b):
        specs = TABLES[table][0][model]
        small, large = sorted((abs(a), abs(b)))
        assert acc._pick_range(small, specs).range_max <= acc._pick_range(large, specs).range_max
        assert acc._pick_range(a, specs) is acc._pick_range(-a, specs)
        assert acc._pick_range(a, specs).range_max == pytest.approx(_expected_range(a, specs))

    @pytest.mark.parametrize("table, model", _cases())
    def test_the_105_percent_boundary(self, table, model):
        specs = TABLES[table][0][model]
        for spec, above in zip(specs, specs[1:]):
            edge = spec.range_max * 1.05
            assert acc._pick_range(edge, specs).range_max == spec.range_max
            assert acc._pick_range(edge * (1 - 1e-9), specs).range_max == spec.range_max
            assert acc._pick_range(-edge, specs).range_max == spec.range_max
            if above.range_max > spec.range_max:
                assert acc._pick_range(edge * (1 + 1e-9), specs).range_max == above.range_max
        # Far over the top the last range is used, not an exception.
        assert acc._pick_range(1e30, specs) is specs[-1]
        assert acc._pick_range(0.0, specs) is specs[0]


class TestUncertainty:
    @PROPERTY
    @given(table_names, models, _signed(-12.0, 4.0), st.sampled_from(NPLCS))
    def test_is_the_active_rows_formula(self, table, model, reading, nplc):
        lookup, function, kind = TABLES[table]
        specs = lookup.get(model, lookup["2400"])
        spec = acc._pick_range(reading, specs)
        value = function(reading, model, nplc)
        assert value >= spec.offset > 0
        assert value == function(-reading, model, nplc)
        modifier = acc._nplc_modifier(nplc, spec, kind) if kind else 0.0
        assert value == pytest.approx(spec.pct_reading * abs(reading) + spec.offset + modifier, rel=1e-12)
        # Relative accuracy is never better than the row's percentage.
        assert value / abs(reading) > spec.pct_reading

    @PROPERTY
    @given(table_names, st.sampled_from(MODELS), st.sampled_from(NPLCS), st.data())
    def test_small_change_in_the_reading_small_change_inside_a_range(self, table, model, nplc, data):
        lookup, function, _ = TABLES[table]
        specs = lookup[model]
        index = data.draw(st.integers(0, len(specs) - 1))
        low = specs[index - 1].range_max * 1.05 if index else 0.0
        high = specs[index].range_max * 1.05
        if not low < high:
            return      # the repeated 1 A row: an empty interval
        a = data.draw(st.floats(low, high, exclude_min=True))
        b = data.draw(st.floats(low, high, exclude_min=True))
        assert abs(function(a, model, nplc) - function(b, model, nplc)) <= specs[index].pct_reading * abs(a - b) * (1 + 1e-9) + 1e-18

    _INHERITED_1A = (
        "The 2420/2425/2430/2440 current tables reuse the 2400's 1 A row "
        "(0.22 % measure, 0.27 % source). Ranging up from it to the model's own "
        "3 A or 5 A row makes the uncertainty at 1.05 A *fall* (2.88 mA -> 2.26 mA "
        "measured on a 2420), which a coarser range cannot do. Together with the "
        "dead 0.067 % row in _I_SRC_2440 this points at the 1 A row being "
        "model-specific in the datasheet; check it against 1KW-2798-3.")

    @pytest.mark.parametrize("table, model", _cases({
        ("i_measure", "2420"): _INHERITED_1A, ("i_measure", "2425"): _INHERITED_1A,
        ("i_measure", "2430"): _INHERITED_1A, ("i_measure", "2440"): _INHERITED_1A,
        ("i_source", "2420"): _INHERITED_1A, ("i_source", "2425"): _INHERITED_1A,
        ("i_source", "2430"): _INHERITED_1A,
    }))
    def test_ranging_up_never_improves_the_uncertainty(self, table, model):
        lookup, function, _ = TABLES[table]
        specs = lookup[model]
        for nplc in NPLCS:
            for spec in specs[:-1]:
                edge = spec.range_max * 1.05
                assert function(edge * (1 + 1e-9), model, nplc) >= function(edge * (1 - 1e-9), model, nplc)

    @PROPERTY
    @given(st.sampled_from(["v_measure", "i_measure"]), st.sampled_from(MODELS), _signed(-12.0, 4.0),
           st.floats(1e-3, 20.0), st.floats(1e-3, 20.0))
    def test_a_longer_integration_is_never_worse(self, table, model, reading, a, b):
        _, function, _ = TABLES[table]
        fast, slow = sorted((a, b))
        assert function(reading, model, slow) <= function(reading, model, fast)
        # The modifier is a step function of three buckets.
        bucket = 1.0 if slow >= 0.5 else 0.1 if slow >= 0.05 else 0.01
        assert function(reading, model, slow) == function(reading, model, bucket)

    @PROPERTY
    @given(table_names, models, any_reading, st.one_of(st.sampled_from(NPLCS), any_reading))
    def test_a_number_or_nan_and_never_an_exception(self, table, model, reading, nplc):
        value = TABLES[table][1](reading, model, nplc)
        if math.isfinite(reading):
            assert value > 0 and math.isfinite(value)
        else:
            assert math.isnan(value)

    @PROPERTY
    @given(table_names, st.sampled_from(["2450", "bogus", "", "24"]), _signed(-12.0, 4.0), st.sampled_from(NPLCS))
    def test_an_unknown_model_gets_the_2400_rows(self, table, model, reading, nplc):
        function = TABLES[table][1]
        assert function(reading, model, nplc) == function(reading, "2400", nplc)


volts = _signed(-7.0, 2.0)
amps = _signed(-9.0, 0.0)


class TestResistanceUncertainty:
    @PROPERTY
    @given(volts, amps, st.sampled_from(MODELS), st.sampled_from(NPLCS))
    def test_rss_of_the_two_relative_uncertainties(self, v, i, model, nplc):
        sigma = acc.resistance_uncertainty(v, i, model, nplc)
        rel_v = acc.voltage_uncertainty(v, model, nplc) / abs(v)
        rel_i = acc.current_uncertainty(i, model, nplc) / abs(i)
        r = abs(v / i)
        assert sigma == pytest.approx(r * math.hypot(rel_v, rel_i), rel=1e-12)
        # Between the larger term alone and the manual's linear sum.
        assert r * max(rel_v, rel_i) * (1 - 1e-12) <= sigma <= r * (rel_v + rel_i) * (1 + 1e-12)
        for sv, si in ((-1, 1), (1, -1), (-1, -1)):
            assert acc.resistance_uncertainty(sv * v, si * i, model, nplc) == sigma

    @PROPERTY
    @given(_magnitude(-3.0, 10.0), _magnitude(-7.0, -1.0), st.sampled_from(MODELS), st.sampled_from(NPLCS))
    def test_enhanced_uses_its_table_inside_it_and_falls_back_outside(self, r, i, model, nplc):
        v = r * i
        actual_r = v / i
        enhanced = acc.resistance_uncertainty(v, i, model, nplc, enhanced=True)
        table = acc._R_ENHANCED[model]
        if 2.0 < actual_r <= table[-1].range_max * 1.05:
            spec = acc._pick_range(actual_r, table)
            assert enhanced == pytest.approx(spec.pct_reading * actual_r + spec.offset, rel=1e-12)
        else:
            assert enhanced == acc.resistance_uncertainty(v, i, model, nplc)
        assert enhanced > 0

    @PROPERTY
    @given(any_reading, any_reading, models, st.sampled_from(NPLCS), st.booleans())
    def test_a_number_or_nan_and_never_an_exception(self, v, i, model, nplc, enhanced):
        sigma = acc.resistance_uncertainty(v, i, model, nplc, enhanced)
        if not (math.isfinite(v) and math.isfinite(i)) or i == 0:
            assert math.isnan(sigma)
        elif v != 0:
            assert sigma > 0

    @pytest.mark.xfail(strict=True, raises=OverflowError, reason=(
        "resistance_uncertainty squares sigma_V / V with `**`. For 0 < |V| below "
        "about 3e-158 the ratio is finite but its square is not, and `**` raises "
        "OverflowError where math.hypot would return inf (accuracy.py, last line "
        "of resistance_uncertainty). four_point_combined_uncertainty calls it "
        "unguarded."))
    @pytest.mark.parametrize("v", [1e-160, 1e-200, 1e-300])
    def test_a_vanishing_voltage_does_not_raise(self, v):
        sigma = acc.resistance_uncertainty(v, 1e-3)
        assert math.isnan(sigma) or sigma > 0

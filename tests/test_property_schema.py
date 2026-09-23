"""Properties of the settings contract, read off the models themselves.

``test_settings_bounds.py`` compares the models' bounds with the widgets, and
``test_resolve.py`` walks the resolver through chosen requests. This module
does not name a single bound: it reads every ``ge``/``gt``/``le``/``lt`` from
the pydantic fields, so it follows the models when they change, and asserts

* a settings dict drawn inside every bound validates, for every model;
* the same dict with one value pushed past one bound fails, and the error
  names that field and no other;
* ``SpotRequest`` and ``SampleGeometry`` accept exactly what their docstrings
  say (token map ids, one-line labels, positions in pairs, the dimensions of
  the shape and no others);
* ``resolve_run_settings`` never raises, whatever a request holds, and a
  strict resolution that is ``ok`` holds only values its own models accept
  when asked again.

The run is derandomised and keeps no example database.
"""

import copy
import math
import re
import tempfile
import typing
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from resistamet_gui.constants import DEFAULT_SETTINGS, MODE_TIMING_OVERRIDES
from resistamet_gui.data_export import CsvExporter, parse_metadata
from resistamet_gui.schema import settings_common as common
from resistamet_gui.schema.resolve import allowed_override_keys, resolve_run_settings
from resistamet_gui.schema.settings_modes import MODE_MODELS
from resistamet_gui.schema.spots import MAP_ID_PATTERN, SampleGeometry, SpotRequest

# No deadline: pydantic builds validators lazily, so first examples are slow,
# and nothing here is about speed. max_examples bounds the run.
PROPERTY = settings(max_examples=100, deadline=None, database=None, derandomize=True)

MODELS = dict(MODE_MODELS)
MODELS.update({m.__name__: m for m in (common.InstrumentSettings, common.AuxSensorSettings,
                                       common.SafetySettings, common.FileSettings,
                                       common.OutputSettings, common.DisplaySettings)})


def _bounds(field):
    """(low, low_inclusive, high, high_inclusive) read from the field."""
    low = high = None
    low_inc = high_inc = True
    for item in field.metadata:
        if getattr(item, "ge", None) is not None:
            low, low_inc = item.ge, True
        if getattr(item, "gt", None) is not None:
            low, low_inc = item.gt, False
        if getattr(item, "le", None) is not None:
            high, high_inc = item.le, True
        if getattr(item, "lt", None) is not None:
            high, high_inc = item.lt, False
    return low, low_inc, high, high_inc


def _base_type(annotation):
    """float, int, bool, str, 'literal' or None for anything else."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        inner = [a for a in typing.get_args(annotation) if a is not type(None)]
        return _base_type(inner[0]) if len(inner) == 1 else None
    if origin is typing.Literal:
        return "literal"
    return annotation if annotation in (float, int, bool, str) else None


def _inside(field):
    kind = _base_type(field.annotation)
    low, low_inc, high, high_inc = _bounds(field)
    if kind is float:
        lo = -1e6 if low is None else low
        hi = lo + 1e6 if high is None else high
        return st.floats(lo, hi, exclude_min=not low_inc, exclude_max=not high_inc)
    if kind is int:
        lo = 0 if low is None else low + (0 if low_inc else 1)
        hi = lo + 10 ** 6 if high is None else high - (0 if high_inc else 1)
        return st.integers(lo, hi)
    if kind is bool:
        return st.booleans()
    if kind == "literal":
        annotation = field.annotation
        if typing.get_origin(annotation) is typing.Union:
            annotation = [a for a in typing.get_args(annotation) if a is not type(None)][0]
        return st.sampled_from(typing.get_args(annotation))
    return None       # strings and lists keep their defaults


@st.composite
def valid_settings(draw, model):
    values = {}
    for name, field in model.model_fields.items():
        strategy = _inside(field)
        if strategy is not None:
            values[name] = draw(strategy)
    if "sweep_source" in values:
        # Start, stop, step and compliance are in the unit of the source.
        from resistamet_gui.schema import settings_modes as modes
        if values["sweep_source"] == "current":
            cap = modes.SWEEP_MAX_SOURCE_CURRENT_A
            for key in ("sweep_start", "sweep_stop", "sweep_step"):
                values[key] = math.copysign(min(abs(values[key]), cap), values[key])
        else:
            values["sweep_compliance"] = min(values["sweep_compliance"], modes.SWEEP_MAX_CURRENT_COMPLIANCE_A)
    return values


def _bounded_fields(model):
    out = []
    for name, field in model.model_fields.items():
        low, _, high, _ = _bounds(field)
        if _base_type(field.annotation) in (float, int):
            out += [(name, "low")] if low is not None else []
            out += [(name, "high")] if high is not None else []
    return out


def _outside(field, side, how_far, kind_of_bad):
    low, low_inc, high, high_inc = _bounds(field)
    is_int = _base_type(field.annotation) is int
    edge, sign = (low, -1.0) if side == "low" else (high, 1.0)
    if kind_of_bad == "nan" and not is_int:
        return float("nan")
    if kind_of_bad == "inf" and not is_int:
        return sign * float("inf")
    if is_int:
        return int(edge + sign * max(1, round(how_far)))
    if kind_of_bad == "edge" and not (low_inc if side == "low" else high_inc):
        return float(edge)                  # an exclusive bound excludes itself
    return edge + sign * max(abs(edge), 1.0) * how_far


@pytest.mark.parametrize("name", sorted(MODELS))
class TestEveryModel:
    """The models against their own bounds: that pydantic enforces what each
    field declares, and that no bound on one field leaks onto another. The
    bounds read off the model cannot say whether a bound is *right*; that is
    ``test_settings_bounds.py``, which holds them against the widgets."""

    @PROPERTY
    @given(st.data())
    def test_inside_every_bound_validates(self, name, data):
        model = MODELS[name]
        values = data.draw(valid_settings(model))
        validated = model.model_validate(values)
        for key, value in values.items():
            assert getattr(validated, key) == value
        # Strict typing accepts them too: they are already the right types.
        model.model_validate(values, strict=True)

    @PROPERTY
    @given(st.data())
    def test_outside_one_bound_fails_and_names_the_field(self, name, data):
        model = MODELS[name]
        candidates = _bounded_fields(model)
        if not candidates:
            pytest.skip(f"{name} has no bounded numeric field")
        values = data.draw(valid_settings(model))
        key, side = data.draw(st.sampled_from(candidates))
        values[key] = _outside(model.model_fields[key], side,
                               data.draw(st.floats(1e-9, 1e3)),
                               data.draw(st.sampled_from(["past", "past", "edge", "nan", "inf"])))
        with pytest.raises(ValidationError) as caught:
            model.model_validate(values)
        assert {error["loc"][0] for error in caught.value.errors()} == {key}

    def test_the_defaults_validate(self, name):
        MODELS[name]()


class TestSweepUnits:
    @PROPERTY
    @given(st.data())
    def test_a_value_legal_in_volts_but_not_in_amperes_names_its_field(self, data):
        from resistamet_gui.schema import settings_modes as modes
        model = MODE_MODELS["sweep"]
        values = data.draw(valid_settings(model))
        values["sweep_source"] = "current"
        for key in ("sweep_start", "sweep_stop", "sweep_step"):
            values[key] = min(abs(values[key]), modes.SWEEP_MAX_SOURCE_CURRENT_A) or 1.0
        key = data.draw(st.sampled_from(["sweep_start", "sweep_stop", "sweep_step"]))
        values[key] = data.draw(st.floats(modes.SWEEP_MAX_SOURCE_CURRENT_A * 1.0001,
                                          modes.SWEEP_MAX_SOURCE_VOLTAGE_V))
        with pytest.raises(ValidationError) as caught:
            model.model_validate(values)
        assert {error["loc"][0] for error in caught.value.errors()} == {key}
        values["sweep_source"] = "voltage"
        values["sweep_compliance"] = min(values["sweep_compliance"], modes.SWEEP_MAX_CURRENT_COMPLIANCE_A)
        model.model_validate(values)


_SPOT = {"map_id": "wafer-7", "index": 0, "label": "centre"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
finite = st.floats(allow_nan=False, allow_infinity=False)


class TestSpotRequest:
    @PROPERTY
    @given(st.one_of(st.text(max_size=70), st.from_regex(r"[A-Za-z0-9_-]{1,64}", fullmatch=True),
                     st.from_regex(r"[A-Za-z0-9_-]{1,8}[\n/. \\]", fullmatch=True)))
    def test_map_id_is_a_token_and_nothing_else(self, map_id):
        is_token = re.fullmatch(r"[A-Za-z0-9_-]{1,64}", map_id) is not None
        assert is_token == (re.fullmatch(MAP_ID_PATTERN, map_id) is not None)
        if is_token:
            assert SpotRequest(**{**_SPOT, "map_id": map_id}).map_id == map_id
        else:
            with pytest.raises(ValidationError) as caught:
                SpotRequest(**{**_SPOT, "map_id": map_id})
            assert {error["loc"][0] for error in caught.value.errors()} == {"map_id"}

    @PROPERTY
    @given(st.one_of(st.text(max_size=100),
                     st.text(st.characters(blacklist_categories=("Cs", "Cc")), min_size=1, max_size=90),
                     st.sampled_from(["a: b", "# x", "007", "true", "NaN", "[1, 2]", " µΩ ",
                                      "x\u2028y", "x\x85y", "C:\\data\\n1"])))
    def test_an_accepted_label_reads_back_from_a_header_line_unchanged(self, label):
        try:
            spot = SpotRequest(**{**_SPOT, "label": label})
        except ValidationError as exc:
            assert {error["loc"][0] for error in exc.errors()} == {"label"}
            stripped = label.strip()
            assert not stripped or len(stripped) > 80 or _CONTROL.search(label)
            return
        assert spot.label == label.strip() == spot.label.strip()
        assert 1 <= len(spot.label) <= 80 and not _CONTROL.search(spot.label)
        with tempfile.TemporaryDirectory() as directory:
            exporter = CsvExporter(Path(directory) / "run", {"spot": spot.model_dump()},
                                   ["elapsed_s", "V"])
            exporter.write_row([0.0, 1.0])
            exporter.finalize({"total_samples": 1})
            read = parse_metadata(exporter.output_paths[0], text_keys=("spot.label",))
        assert read["spot.label"] == spot.label

    @PROPERTY
    @given(st.one_of(st.none(), finite), st.one_of(st.none(), finite),
           st.one_of(st.none(), st.floats(-720.0, 720.0)), st.integers(-10, 10010))
    def test_position_is_a_pair_and_the_numbers_are_bounded(self, x, y, angle, index):
        fields = {**_SPOT, "index": index, "x_mm": x, "y_mm": y, "angle_deg": angle}
        fine = ((x is None) == (y is None) and 0 <= index <= 9999
                and (angle is None or -360.0 <= angle <= 360.0))
        if fine:
            spot = SpotRequest(**fields)
            assert spot.has_position == (x is not None)
        else:
            with pytest.raises(ValidationError):
                SpotRequest(**fields)

    @pytest.mark.parametrize("field", ["x_mm", "y_mm", "angle_deg"])
    @pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
    def test_no_number_may_be_nan_or_infinite(self, field, value):
        with pytest.raises(ValidationError):
            SpotRequest(**{**_SPOT, "x_mm": 0.0, "y_mm": 0.0, field: value})


_NEEDED = {"unbounded": set(), "circle": {"diameter_mm"}, "rectangle": {"width_mm", "length_mm"}}
length = st.floats(1e-6, 1e6)


class TestSampleGeometry:
    @PROPERTY
    @given(st.sampled_from(sorted(_NEEDED)),
           st.fixed_dictionaries({}, optional={"diameter_mm": length, "width_mm": length, "length_mm": length}))
    def test_exactly_the_dimensions_of_the_shape(self, shape, dimensions):
        given_names = set(dimensions)
        if given_names == _NEEDED[shape]:
            outline = SampleGeometry(shape=shape, **dimensions)
            assert all(getattr(outline, key) == value for key, value in dimensions.items())
            return
        with pytest.raises(ValidationError) as caught:
            SampleGeometry(shape=shape, **dimensions)
        # The model's own words, without pydantic's echo of the input.
        message = " ".join(error["msg"] for error in caught.value.errors())
        # The first dimension at fault is named, and only one that is at
        # fault: missing from the shape, or not the shape's to have.
        wrong = _NEEDED[shape] ^ given_names
        right = {"diameter_mm", "width_mm", "length_mm"} - wrong
        assert any(name in message for name in wrong)
        assert not any(name in message for name in right)

    @PROPERTY
    @given(st.sampled_from(["diameter_mm", "width_mm", "length_mm"]),
           st.one_of(st.floats(max_value=0.0), st.just(float("nan")), st.just(float("inf"))))
    def test_a_dimension_is_positive_and_finite(self, name, value):
        shape = "circle" if name == "diameter_mm" else "rectangle"
        dimensions = {key: 10.0 for key in _NEEDED[shape]}
        dimensions[name] = value
        with pytest.raises(ValidationError) as caught:
            SampleGeometry(shape=shape, **dimensions)
        assert {error["loc"][0] for error in caught.value.errors()} == {name}


def _profile():
    return copy.deepcopy({key: DEFAULT_SETTINGS[key] for key in ("measurement", "display", "file", "output")})


junk = st.one_of(
    st.none(), st.booleans(), st.integers(-10 ** 12, 10 ** 12), st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=6), st.sampled_from(["on", "off", "true", "false", "1", "voltage", "circle"]),
    st.lists(st.integers(), max_size=2), st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
)
modes = st.sampled_from(sorted(MODE_MODELS))


@st.composite
def requests(draw):
    mode = draw(modes)
    allowed = sorted(allowed_override_keys(mode))
    keys = st.one_of(st.sampled_from(allowed),
                     st.sampled_from(["settling_time", "gpib_address", "safety_voltage_warn_v", "no_such_key"]))
    model = MODE_MODELS[mode]
    overrides = {}
    for key in draw(st.lists(keys, max_size=5)):
        field = model.model_fields.get(key)
        inside = _inside(field) if field is not None else None
        overrides[key] = draw(st.one_of(junk, inside) if inside is not None else junk)
    return mode, overrides


@st.composite
def valid_requests(draw):
    """Overrides drawn inside every bound, for keys the mode allows."""
    mode = draw(modes)
    values = draw(valid_settings(MODE_MODELS[mode]))
    keys = sorted(key for key in values if key in allowed_override_keys(mode))
    chosen = set(draw(st.lists(st.sampled_from(keys), min_size=1, max_size=5, unique=True)))
    if mode == "sweep" and any(key.startswith("sweep_") for key in chosen):
        # valid_settings fits the sweep values to the source it drew, so
        # they only hold together with that source.
        chosen |= {key for key in keys if key.startswith("sweep_")}
    return mode, {key: values[key] for key in sorted(chosen)}


#: The checks across fields a request inside every bound can still fail:
#: the ones the GUI makes at Start, a rectangle without its sides, and a
#: sweep step so fine that its points cannot be counted.
_CROSS_FIELD_KEYS = {"vdp_thickness_cm", "fpp_power_stop_w", "fpp_sample_shape", "sweep_step"}


class TestResolver:
    @PROPERTY
    @given(requests(), st.booleans())
    def test_never_raises_and_never_touches_the_profile(self, request, strict):
        mode, overrides = request
        profile = _profile()
        before = copy.deepcopy(profile)
        resolved = resolve_run_settings(profile, mode, overrides, strict=strict)
        assert repr(profile) == repr(before)          # repr: NaN is not equal to itself
        assert all(issue.severity in ("error", "warning") for issue in resolved.issues)
        assert resolved.ok == (not any(issue.severity == "error" for issue in resolved.issues))
        if strict:
            for key in overrides:
                if key not in allowed_override_keys(mode):
                    assert key in {issue.key for issue in resolved.issues}

    @PROPERTY
    @given(requests())
    def test_what_strict_resolution_accepts_it_accepts_again(self, request):
        mode, overrides = request
        resolved = resolve_run_settings(_profile(), mode, overrides, strict=True)
        if not resolved.ok:
            return
        self._accepted_again(mode, resolved)

    @PROPERTY
    @given(valid_requests())
    def test_a_request_inside_every_bound_is_applied_and_accepted_again(self, request):
        mode, overrides = request
        resolved = resolve_run_settings(_profile(), mode, overrides, strict=True)
        errors = {issue.key for issue in resolved.issues if issue.severity == "error"}
        assert errors <= _CROSS_FIELD_KEYS, resolved.issues
        if not resolved.ok:
            return
        forced = MODE_TIMING_OVERRIDES.get(mode, {})
        measurement = resolved.settings["measurement"]
        for key, value in overrides.items():
            assert measurement[key] == (forced[key] if key in forced else value), key
        self._accepted_again(mode, resolved)

    @staticmethod
    def _accepted_again(mode, resolved):
        measurement = resolved.settings["measurement"]
        for model in (MODE_MODELS[mode], common.InstrumentSettings, common.AuxSensorSettings,
                      common.SafetySettings):
            subset = {key: measurement[key] for key in model.model_fields if key in measurement}
            value = subset.get("fpp_temperature_c")
            if isinstance(value, float) and math.isnan(value):
                subset["fpp_temperature_c"] = None      # "not measured", as the resolver reads it
            model.model_validate(subset, strict=True)
        # Sent back as a request, the resolved values are accepted and stay put.
        echo = {key: value for key, value in measurement.items() if key in allowed_override_keys(mode)}
        again = resolve_run_settings(_profile(), mode, echo, strict=True)
        assert again.ok, [(issue.key, issue.message) for issue in again.issues]
        assert repr(again.settings) == repr(resolved.settings)

    @pytest.mark.xfail(strict=True, reason=(
        "ResistanceSettings.res_cable_null is `ge=0.0` with no upper bound and "
        "pydantic admits infinity by default, so a strict request of "
        "res_cable_null = inf resolves ok and every reading has infinity "
        "subtracted from it. SpotRequest and SampleGeometry say "
        "allow_inf_nan=False; the mode models' one-sided bounds do not."))
    def test_an_accepted_request_holds_only_finite_numbers(self):
        resolved = resolve_run_settings(_profile(), "resistance", {"res_cable_null": float("inf")}, strict=True)
        assert not resolved.ok

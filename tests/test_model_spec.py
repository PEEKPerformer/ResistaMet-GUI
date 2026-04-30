"""Tests for the ModelSpec table and detect_model() identification path."""
from __future__ import annotations

import pytest

from resistamet_gui.instrument import (
    Keithley2400,
    ModelSpec,
    known_models,
    parse_model_from_idn,
)


# Real IDN strings captured from the lab — anchors the parsing test to ground truth.
_BENCH_IDNS = {
    "2420": "KEITHLEY INSTRUMENTS INC.,MODEL 2420,1230523,C30   Mar 17 2006 09:29:29/A02  /H/L",
    "2400": "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1175680,C30   Mar 17 2006 09:29:29/A02  /K/J",
}


class TestParseModelFromIdn:
    def test_parses_2420_idn(self):
        spec = parse_model_from_idn(_BENCH_IDNS["2420"])
        assert spec is not None
        assert spec.model == "2420"
        assert spec.max_source_v == 60.0
        assert spec.max_source_i == 3.05
        assert spec.family == "2400"

    def test_parses_2400_idn(self):
        spec = parse_model_from_idn(_BENCH_IDNS["2400"])
        assert spec is not None
        assert spec.model == "2400"
        assert spec.max_source_v == 200.0
        assert spec.max_source_i == 1.05
        assert spec.family == "2400"

    def test_unknown_model_returns_none(self):
        spec = parse_model_from_idn(
            "KEITHLEY INSTRUMENTS INC.,MODEL 9999,12345,X99   Jan 1 2030"
        )
        assert spec is None

    def test_non_keithley_idn_returns_none(self):
        assert parse_model_from_idn("ROHDE&SCHWARZ,SMA100B,12345,1.0") is None

    def test_empty_idn_returns_none(self):
        assert parse_model_from_idn("") is None

    def test_malformed_idn_returns_none(self):
        assert parse_model_from_idn("not an idn string") is None

    def test_2450_recognized_as_different_family(self):
        idn = "KEITHLEY INSTRUMENTS INC.,MODEL 2450,12345,1.6.4"
        spec = parse_model_from_idn(idn)
        assert spec is not None
        assert spec.family == "2450"


class TestKnownModels:
    def test_includes_bench_models(self):
        models = known_models()
        assert "2400" in models
        assert "2420" in models

    def test_includes_high_voltage_2410(self):
        models = known_models()
        assert "2410" in models
        spec = parse_model_from_idn("KEITHLEY,MODEL 2410,1,1")
        assert spec is not None
        assert spec.max_source_v == 1100.0  # the high-V variant

    def test_each_entry_has_complete_spec(self):
        for model in known_models():
            spec = parse_model_from_idn(f"KEITHLEY,MODEL {model},x,y")
            assert isinstance(spec, ModelSpec)
            assert spec.model == model
            assert spec.max_source_v > 0
            assert spec.max_source_i > 0
            assert spec.max_power_w > 0
            assert spec.family in ("2400", "2450")


class TestDetectModelOnFake:
    """detect_model() goes through the real *IDN? path; verify against fake."""

    def test_detect_default_returns_2420(self, fake_instrument):
        # Default fake uses the bench 2420 IDN
        wrapper = Keithley2400("GPIB0::24::INSTR")
        wrapper.dev = fake_instrument
        spec = wrapper.detect_model()
        assert spec is not None
        assert spec.model == "2420"

    def test_detect_with_explicit_model(self, fake_rm):
        # Open a fresh resource manager configured with model="2410"
        from tests.fakes.fake_keithley import FakeKeithley
        fake = FakeKeithley(model="2410")
        wrapper = Keithley2400("GPIB0::24::INSTR")
        wrapper.dev = fake
        spec = wrapper.detect_model()
        assert spec is not None
        assert spec.model == "2410"
        assert spec.max_source_v == 1100.0

    def test_detect_unknown_returns_none(self, fake_rm):
        from tests.fakes.fake_keithley import FakeKeithley
        fake = FakeKeithley(idn="KEITHLEY INSTRUMENTS INC.,MODEL 9999,1,1")
        wrapper = Keithley2400("GPIB0::24::INSTR")
        wrapper.dev = fake
        assert wrapper.detect_model() is None

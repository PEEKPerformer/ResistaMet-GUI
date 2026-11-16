import assert from "node:assert/strict";
import { test } from "node:test";

import { fppCurrentDecision } from "./fppCurrentDecision.ts";

test("a marginal finding shows what was reached against the target", () => {
  const reason = "R≈0.0123 Ω: 2.7 significant figures at 100 mA (V≈1.23 mV, SNR≈492), not the 4.0 asked for; limited by the current ceiling.";
  assert.deepEqual(
    fppCurrentDecision({
      verdict: "marginal",
      message: reason,
      current_a: 0.1,
      requested_current_a: 0.001,
      resistance_ohm: 0.0123,
      bound: false,
      expected_voltage_v: 0.00123,
      noise_floor_v: 2.5e-6,
      snr: 492,
      sig_figs: 2.69,
      target_sig_figs: 4,
    }),
    {
      title: "Target significant figures not reached",
      reason,
      rows: [
        { label: "Measured R", value: "12.30 mΩ" },
        { label: "Probe current", value: "1.000 mA" },
        { label: "Suggested current", value: "100.0 mA" },
        { label: "Expected voltage", value: "1.230 mV" },
        { label: "Noise floor", value: "2.500 µV" },
        { label: "Significant figures", value: "2.7 of 4" },
      ],
    },
  );
});

test("a too-conductive finding is titled as one", () => {
  const decision = fppCurrentDecision({ verdict: "too_conductive", message: "under 1 significant figure", sig_figs: 0.4, target_sig_figs: 4 });
  assert.equal(decision.title, "Sample too conductive");
  assert.deepEqual(decision.rows, [{ label: "Significant figures", value: "0.4 of 4" }]);
});

test("in compliance at the smallest current, the resistance is a lower bound and nothing else was measured", () => {
  const decision = fppCurrentDecision({
    verdict: "too_resistive",
    message: "In compliance (5 V) at 1 µA: V/I > 5e+06 Ω.",
    current_a: 1e-6,
    requested_current_a: 1e-3,
    resistance_ohm: 5e6,
    bound: true,
    expected_voltage_v: 5,
    noise_floor_v: null,
    snr: null,
    sig_figs: null,
    target_sig_figs: 4,
  });
  assert.equal(decision.title, "Sample too resistive");
  assert.deepEqual(decision.rows, [
    { label: "R, lower bound", value: "> 5.000 MΩ" },
    { label: "Probe current", value: "1.000 mA" },
    { label: "Suggested current", value: "1.000 µA" },
    { label: "Expected voltage", value: "5.000 V" },
    { label: "Significant figures", value: "— of 4" },
  ]);
});

test("a negative requested current keeps its sign on the suggestion", () => {
  const rows = fppCurrentDecision({ verdict: "marginal", current_a: -0.01, requested_current_a: -0.001, target_sig_figs: 3 }).rows;
  assert.deepEqual(rows.slice(0, 2), [
    { label: "Probe current", value: "-1.000 mA" },
    { label: "Suggested current", value: "-10.00 mA" },
  ]);
});

test("a detail this build does not know still has a title and no invented rows", () => {
  assert.deepEqual(fppCurrentDecision({}), { title: "Auto-select current", reason: "", rows: [] });
});

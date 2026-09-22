import { test } from "node:test";
import assert from "node:assert/strict";
import { outOfBounds, parseEngineering } from "./engineeringParse.ts";

const AMPS = { unit: "A", prefixes: true };

test("plain numbers and exponents", () => {
  assert.equal(parseEngineering("12"), 12);
  assert.equal(parseEngineering("  -0.5 "), -0.5);
  assert.equal(parseEngineering("+.5"), 0.5);
  assert.equal(parseEngineering("5."), 5);
  assert.equal(parseEngineering("1e-4"), 1e-4);
  assert.equal(parseEngineering("1.5E3"), 1500);
  assert.equal(parseEngineering("0"), 0);
});

test("a prefix scales exactly", () => {
  assert.equal(parseEngineering("100u"), 1e-4);
  assert.equal(parseEngineering("100u"), Number("1e-4"));
  assert.equal(parseEngineering("0.1m"), 1e-4);
  assert.equal(parseEngineering("1.5k"), 1500);
  assert.equal(parseEngineering("2.2M"), 2.2e6);
  assert.equal(parseEngineering("10n"), 1e-8);
  assert.equal(parseEngineering("3p"), 3e-12);
  assert.equal(parseEngineering("7f"), 7e-15);
  assert.equal(parseEngineering("1e3m"), 1);
  assert.equal(parseEngineering("-20m"), -0.02);
});

test("prefixes are case-sensitive: m is milli, M is mega, and U is nothing", () => {
  assert.equal(parseEngineering("100m", { unit: "V", prefixes: true }), 0.1);
  assert.equal(parseEngineering("100M", { unit: "V", prefixes: true }), 1e8);
  for (const wrong of ["100U", "10N", "10P", "5t", "5g", "5K", "7F"]) {
    assert.equal(parseEngineering(wrong, AMPS), null, wrong);
  }
});

test("micro may be typed as u, the micro sign or a Greek mu", () => {
  assert.equal(parseEngineering("2u", AMPS), 2e-6);
  assert.equal(parseEngineering("2µ", AMPS), 2e-6);
  assert.equal(parseEngineering("2μ", AMPS), 2e-6);
  assert.equal(parseEngineering("2 μA", AMPS), 2e-6);
  assert.equal(parseEngineering("2 µA", AMPS), 2e-6);
  assert.equal(parseEngineering("2uA", AMPS), 2e-6);
});

test("the field's unit may follow, and no other", () => {
  assert.equal(parseEngineering("3 A", AMPS), 3);
  assert.equal(parseEngineering("3A", AMPS), 3);
  assert.equal(parseEngineering("100 mA", AMPS), 0.1);
  assert.equal(parseEngineering("100 mV", AMPS), null);
  assert.equal(parseEngineering("3 V", AMPS), null);
  assert.equal(parseEngineering("3 a", AMPS), null);
  assert.equal(parseEngineering("3 A"), null);
  assert.equal(parseEngineering("50 Hz", { unit: "Hz", prefixes: true }), 50);
  assert.equal(parseEngineering("1.5 kHz", { unit: "Hz", prefixes: true }), 1500);
});

test("a unit that takes no prefix refuses one", () => {
  const cm = { unit: "cm", prefixes: false };
  assert.equal(parseEngineering("0.5", cm), 0.5);
  assert.equal(parseEngineering("0.5 cm", cm), 0.5);
  assert.equal(parseEngineering("5m", cm), null);
  assert.equal(parseEngineering("5 mcm", cm), null);
  const um = { unit: "µm", prefixes: false };
  assert.equal(parseEngineering("500 µm", um), 500);
  assert.equal(parseEngineering("500 μm", um), 500);
  assert.equal(parseEngineering("500um", um), 500);
  assert.equal(parseEngineering("500u", um), null);
  assert.equal(parseEngineering("2 h", { unit: "h", prefixes: false }), 2);
});

test("a suffix that reads two ways is refused", () => {
  const metres = { unit: "m", prefixes: true };
  assert.equal(parseEngineering("5m", metres), null);
  assert.equal(parseEngineering("5 mm", metres), 5e-3);
  assert.equal(parseEngineering("5", metres), 5);
});

test("anything that is not wholly a number is refused", () => {
  for (const wrong of [
    "", " ", "abc", "12abc", "1,5", "1,000", "0x10", "1e", "e3", "1e3.5", "--1", "1 000", "1..2", ".",
    "1 e3", "1m A", "1mm", "−1", "Infinity", "NaN", "1e999", "1_000", "١٢",
  ]) {
    assert.equal(parseEngineering(wrong), null, JSON.stringify(wrong));
  }
});

test("bounds are reported, not applied", () => {
  assert.equal(outOfBounds(100, -3, 3), "above");
  assert.equal(outOfBounds(-100, -3, 3), "below");
  assert.equal(outOfBounds(3, -3, 3), null);
  assert.equal(outOfBounds(-3, -3, 3), null);
  assert.equal(outOfBounds(1e9, undefined, undefined), null);
  assert.equal(outOfBounds(0, Number.EPSILON, undefined), "below");
});

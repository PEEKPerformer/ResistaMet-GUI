import assert from "node:assert/strict";
import { test } from "node:test";

import { vdpVerdict } from "./vdpVerdict.ts";

test("a valid homogeneous result passes, with its asymmetry", () => {
  assert.deepEqual(vdpVerdict({ valid: true, invalid_reason: "", homogeneous: true, asymmetry_pct: 1.4 }), {
    tone: "ok",
    label: "f(Q) homogeneity passes",
    detail: "asymmetry 1.40 %",
  });
});

test("a valid inhomogeneous result fails the check", () => {
  const verdict = vdpVerdict({ valid: true, invalid_reason: "", homogeneous: false, asymmetry_pct: 12 });
  assert.equal(verdict.tone, "danger");
  assert.equal(verdict.label, "f(Q) homogeneity fails");
});

test("an invalid result is not a homogeneity failure: it says why it is invalid", () => {
  const reason = "voltage compliance at V_32,41: those readings are the 5 V limit, not the sample";
  assert.deepEqual(vdpVerdict({ valid: false, invalid_reason: reason, homogeneous: false, asymmetry_pct: 0 }), {
    tone: "danger",
    label: "Invalid result",
    detail: reason,
  });
});

test("an infinite asymmetry arrives as null and does not throw", () => {
  const asymmetry = null as unknown as number;
  assert.equal(vdpVerdict({ valid: false, invalid_reason: "sign inconsistency", homogeneous: false, asymmetry_pct: asymmetry }).detail, "sign inconsistency");
  assert.equal(vdpVerdict({ homogeneous: false, asymmetry_pct: asymmetry }).detail, "asymmetry —");
});

test("a result from before validity was sent is valid", () => {
  assert.equal(vdpVerdict({ homogeneous: true, asymmetry_pct: 0.5 }).label, "f(Q) homogeneity passes");
});

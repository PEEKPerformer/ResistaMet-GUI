import { test } from "node:test";
import assert from "node:assert/strict";
import { SILENCE_CHOICES, silenceFields, timedSilenceEnd } from "./safetySilence.ts";

const NOW_MS = 1_800_000_000_000;

test("asking every time sends no silence", () => {
  assert.deepEqual(silenceFields("ask"), {});
});

test("seven days and for good send the fields the backend saves", () => {
  assert.deepEqual(silenceFields("week"), { silence_for_days: 7 });
  assert.deepEqual(silenceFields("forever"), { silence_for_profile: true });
});

test("asking every time is the first choice, so the default", () => {
  assert.equal(SILENCE_CHOICES[0]?.value, "ask");
});

test("a timed silence still ahead is shown with its end", () => {
  const until = NOW_MS / 1000 + 3600;
  assert.deepEqual(timedSilenceEnd({ safety_voltage_warn_silenced_until: until }, NOW_MS), new Date(until * 1000));
});

test("an expired, absent or null timed silence is not in force", () => {
  assert.equal(timedSilenceEnd({ safety_voltage_warn_silenced_until: NOW_MS / 1000 - 1 }, NOW_MS), null);
  assert.equal(timedSilenceEnd({ safety_voltage_warn_silenced_until: NOW_MS / 1000 }, NOW_MS), null);
  assert.equal(timedSilenceEnd({ safety_voltage_warn_silenced_until: null }, NOW_MS), null);
  assert.equal(timedSilenceEnd({}, NOW_MS), null);
});

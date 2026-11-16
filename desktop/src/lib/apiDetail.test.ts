import { test } from "node:test";
import assert from "node:assert/strict";
import { describeDetail } from "./apiDetail.ts";

test("a validation error list names the field and says what is wrong", () => {
  const detail = [
    { type: "string_too_long", loc: ["body", "sample_name"], msg: "String should have at most 120 characters", input: "x", ctx: { max_length: 120 } },
    { type: "extra_forbidden", loc: ["body", "overrides", "nplc"], msg: "Extra inputs are not permitted", input: 1 },
  ];
  assert.equal(
    describeDetail(detail, "Unprocessable Entity"),
    "sample_name: String should have at most 120 characters; overrides.nplc: Extra inputs are not permitted",
  );
  assert.doesNotMatch(describeDetail(detail, ""), /object Object/);
});

test("a string detail is shown as it is, and a missing one falls back", () => {
  assert.equal(describeDetail("no prompt is pending", "Conflict"), "no prompt is pending");
  assert.equal(describeDetail(undefined, "Conflict"), "Conflict");
  assert.equal(describeDetail([], "Unprocessable Entity"), "Unprocessable Entity");
});

test("an object detail stays JSON for the caller that parses it", () => {
  const detail = { message: "the profile would not be valid", issues: [{ key: "nplc", message: "too large" }] };
  assert.deepEqual(JSON.parse(describeDetail(detail, "")), detail);
});

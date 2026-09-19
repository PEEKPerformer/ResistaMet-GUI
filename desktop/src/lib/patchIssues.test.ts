import { test } from "node:test";
import assert from "node:assert/strict";
import { patchIssues } from "./patchIssues.ts";

test("a refusal's issues are read from the error detail", () => {
  const detail = JSON.stringify({
    message: "the profile would not be valid",
    issues: [{ section: "measurement", key: "nplc", message: "Input should be less than or equal to 10" }],
  });
  assert.deepEqual(patchIssues(detail), [
    { section: "measurement", key: "nplc", message: "Input should be less than or equal to 10" },
  ]);
  assert.deepEqual(patchIssues("cannot change the instrument address during a run"), []);
  assert.deepEqual(patchIssues('{"issues": "nope"}'), []);
  assert.deepEqual(patchIssues('{"issues": [{"key": 1}]}'), []);
  assert.deepEqual(patchIssues("null"), []);
});

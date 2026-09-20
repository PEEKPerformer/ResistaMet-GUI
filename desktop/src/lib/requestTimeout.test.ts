import { test } from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_TIMEOUT_MS, INSTRUMENT_TIMEOUT_MS, STATUS_TIMEOUT_MS, isTimeout, timeoutFor } from "./requestTimeout.ts";

test("the status poll gives up long before a command does", () => {
  assert.equal(timeoutFor("GET", "/session"), STATUS_TIMEOUT_MS);
  assert.equal(timeoutFor("POST", "/session/stop"), INSTRUMENT_TIMEOUT_MS);
  assert.equal(timeoutFor("POST", "/session/start"), INSTRUMENT_TIMEOUT_MS);
  assert.equal(timeoutFor("GET", "/instruments/resources?visa_library=%40py"), INSTRUMENT_TIMEOUT_MS);
  assert.equal(timeoutFor("GET", "/session/events?since_seq=0&limit=10000"), DEFAULT_TIMEOUT_MS);
  assert.ok(STATUS_TIMEOUT_MS < DEFAULT_TIMEOUT_MS && DEFAULT_TIMEOUT_MS < INSTRUMENT_TIMEOUT_MS);
});

test("a fired AbortSignal.timeout is recognised", async () => {
  const signal = AbortSignal.timeout(1);
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(isTimeout(signal.reason), true);
  assert.equal(isTimeout(new Error("network down")), false);
});

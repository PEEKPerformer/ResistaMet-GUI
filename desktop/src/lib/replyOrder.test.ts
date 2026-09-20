import { test } from "node:test";
import assert from "node:assert/strict";
import { ReplyOrder } from "./replyOrder.ts";

test("a reply to an older request does not replace a newer one", () => {
  const order = new ReplyOrder();
  const poll = order.sent();
  const command = order.sent();
  assert.equal(order.accepts(command), true);
  assert.equal(order.accepts(poll), false);
});

test("replies in order are all accepted", () => {
  const order = new ReplyOrder();
  const first = order.sent();
  assert.equal(order.accepts(first), true);
  const second = order.sent();
  assert.equal(order.accepts(second), true);
});

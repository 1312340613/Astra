import assert from "node:assert/strict";
import test from "node:test";
import { RuntimeTiming } from "./runtime-timing.js";
import { createTextEventBatcher } from "./text-event-batcher.js";
import type { PyEvent } from "./types.js";

test("timings include text batching, handling and the later React commit", () => {
  let now = 100;
  const receipts: unknown[] = [];
  const timing = new RuntimeTiming(samples => receipts.push(...samples), () => now);
  const event = { type: "chunk", content: "private", performance_trace_id: "a".repeat(32) } as PyEvent;
  timing.received(event, 2);
  const batcher = createTextEventBatcher(e => timing.handle(e, () => { now += 3; }), () => {}, () => () => {});
  batcher.accept(event);
  now = 132;
  batcher.flush();
  assert.equal(receipts.length, 0);
  now = 142;
  timing.commit();
  assert.deepEqual(receipts, [{ trace_id: "a".repeat(32), parse_ms: 2,
    batch_ms: 32, handle_ms: 3, react_commit_ms: 7, receive_to_commit_ms: 44 }]);
  assert.ok(!JSON.stringify(receipts).includes("private"));
  timing.commit();
  assert.equal(receipts.length, 1);
});

test("untraced and failed events never produce a false commit receipt", () => {
  const receipts: unknown[] = [];
  const timing = new RuntimeTiming(samples => receipts.push(...samples));
  timing.received({ type: "done" });
  const event = { type: "done", performance_trace_id: "b".repeat(32) } as PyEvent;
  timing.received(event);
  assert.throws(() => timing.handle(event, () => { throw Error("failed"); }));
  timing.commit();
  assert.deepEqual(receipts, []);
  assert.equal(timing.dropped, 1);
});

test("Ink's synchronous commits are measured before an unrelated later render", () => {
  let now = 0;
  const receipts: unknown[] = [];
  const timing = new RuntimeTiming(samples => receipts.push(...samples), () => now);
  const event = { type: "done", performance_trace_id: "c".repeat(32) } as PyEvent;
  timing.received(event);
  now = 5;
  timing.handle(event, () => {
    now = 7;
    timing.commit();
    now = 9;
    timing.commit();
    assert.equal(receipts.length, 0);
    now = 11;
  });
  assert.deepEqual(receipts, [{ trace_id: "c".repeat(32), parse_ms: 0,
    batch_ms: 5, handle_ms: 6, react_commit_ms: 0, receive_to_commit_ms: 9 }]);
  now = 1000;
  timing.commit();
  assert.equal(receipts.length, 1);
});

test("pending receipts are bounded and discarded on reconnect", () => {
  const timing = new RuntimeTiming(() => {});
  for (let i = 0; i < 2100; i++) {
    timing.received({ type: "done", performance_trace_id: i.toString(16).padStart(32, "0") } as PyEvent);
  }
  assert.equal(timing.dropped, 52);
  timing.clear();
  assert.equal(timing.dropped, 2100);
});

import assert from "node:assert/strict";
import test from "node:test";
import { createTextEventBatcher } from "./text-event-batcher.js";
import type { PyEvent } from "./types.js";

function fixture(fail = false) {
  const events: PyEvent[] = [];
  const pending = new Set<() => void>();
  let errors = 0;
  const batch = createTextEventBatcher(event => {
    if (fail && event.type === "chunk") throw new Error("render");
    events.push(event);
  }, () => { errors++; }, callback => {
    pending.add(callback);
    return () => pending.delete(callback);
  });
  return { batch, events, pending, errors: () => errors };
}

test("a thousand tokens parse once per frame without dropping text", () => {
  const { batch, events, pending } = fixture();
  for (let index = 0; index < 1000; index++) batch.accept({ type: "chunk", content: `${index},` });
  assert.equal(pending.size, 1);
  assert.equal(events.length, 0);
  [...pending][0]();
  assert.deepEqual(events, [{ type: "chunk", content: Array.from({ length: 1000 }, (_, i) => `${i},`).join("") }]);
  assert.equal(pending.size, 0);
});

test("role changes, tool boundaries and completion synchronously drain text in order", () => {
  const { batch, events, pending } = fixture();
  batch.accept({ type: "reasoning", content: "think" });
  batch.accept({ type: "chunk", content: "answer" });
  batch.accept({ type: "done" });
  assert.deepEqual(events.map(event => event.type), ["reasoning", "chunk", "done"]);
  assert.equal(pending.size, 0);
  batch.flush();
  assert.equal(events.length, 3);
});

test("failed Markdown parsing and teardown cannot strand task completion", () => {
  const { batch, events, errors, pending } = fixture(true);
  batch.accept({ type: "chunk", content: "bad" });
  batch.accept({ type: "done" });
  assert.equal(errors(), 1);
  assert.deepEqual(events, [{ type: "done" }]);
  batch.accept({ type: "reasoning", content: "pending" });
  batch.discard();
  assert.equal(pending.size, 0);
  assert.equal(events.length, 1);
});

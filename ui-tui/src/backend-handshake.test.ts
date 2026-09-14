import assert from "node:assert/strict";
import test from "node:test";
import { createBackendHandshake } from "./backend-handshake.js";

function fixture() {
  const pending = new Map<number, () => void>();
  const events: string[] = [];
  const close = createBackendHandshake(() => events.push("slow"), () => events.push("expired"), (callback, delay) => {
    pending.set(delay, callback);
    return () => pending.delete(delay);
  });
  return { pending, events, close };
}

test("a silent backend gets one notice and one terminal deadline", () => {
  const { pending, events } = fixture();
  pending.get(3000)!();
  const deadline = pending.get(15000)!;
  deadline(); deadline();
  assert.deepEqual(events, ["slow", "expired"]);
  assert.equal(pending.size, 0);
});

test("an early handshake cancels both timers without timing out model initialization", () => {
  const { pending, events, close } = fixture();
  const staleDeadline = pending.get(15000)!;
  close(); staleDeadline(); close();
  assert.deepEqual(events, []);
  assert.equal(pending.size, 0);
});

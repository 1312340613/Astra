import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createBackendEventReceiver, EventDeliveryState, writeProtocolDiagnostic, type ProtocolDiagnostic } from "./backend-protocol.js";

test("protocol failures are bounded, redacted and do not drop subsequent completion", async () => {
  const diagnostics: ProtocolDiagnostic[] = [];
  const handled: string[] = [];
  const receive = createBackendEventReceiver((event) => {
    if (event.type === "chunk") throw new RangeError("SENSITIVE_SENTINEL \x1b[31m");
    handled.push(event.type);
  }, (diagnostic) => diagnostics.push(diagnostic));
  receive("SENSITIVE_SENTINEL invalid JSON");
  receive("null");
  for (let index = 0; index < 100; index++) receive(JSON.stringify({ type: "chunk", content: "SENSITIVE_SENTINEL" }));
  receive('{"type":"done"}');
  assert.deepEqual(handled, ["done"]);
  assert.deepEqual(diagnostics, [
    { phase: "parse", event_type: "unknown", error_type: "SyntaxError" },
    { phase: "handle", event_type: "chunk", error_type: "RangeError" },
  ]);
  const directory = await mkdtemp(join(tmpdir(), "astra-protocol-"));
  try {
    for (const diagnostic of diagnostics) await writeProtocolDiagnostic(directory, diagnostic);
    const saved = await readFile(join(directory, ".logs", "tui-protocol.jsonl"), "utf8");
    assert.doesNotMatch(saved, /SENSITIVE_SENTINEL|\u001b/);
    assert.equal(saved.trim().split("\n").length, 2);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test("untrusted event and exception names never enter diagnostics", () => {
  const diagnostics: ProtocolDiagnostic[] = [];
  const receive = createBackendEventReceiver(() => {
    const error = new Error("secret"); error.name = "SENSITIVE_SENTINEL"; throw error;
  }, (diagnostic) => diagnostics.push(diagnostic));
  receive('{"type":"SENSITIVE_SENTINEL"}');
  assert.deepEqual(diagnostics, [{ phase: "handle", event_type: "unknown", error_type: "Error" }]);
});

test("replay overlap is idempotent across receiver generations and volatile cursors do not acknowledge failures", () => {
  const delivery = new EventDeliveryState();
  const handled: string[] = [];
  let fail = true;
  const handle = (event: { type: string }) => {
    if (event.type === "tool_calls" && fail) throw new Error("render failed");
    handled.push(event.type);
  };
  const frame = (type: string, cursor: number, extra = {}) => JSON.stringify({ type, cursor, event_id: `id-${cursor}`, replayable: true, ...extra });
  const receiver = createBackendEventReceiver(handle, () => {}, { delivery });
  receiver(frame("task_started", 1));
  receiver(frame("tool_calls", 2));
  receiver(frame("done", 3));
  receiver(frame("chunk", 99, { replayable: false, content: "visible" }));
  assert.equal(delivery.cursor, 1);
  fail = false;
  const reconnected = createBackendEventReceiver(handle, () => {}, { delivery });
  for (const [type, cursor] of [["task_started", 1], ["tool_calls", 2], ["done", 3]] as const) reconnected(frame(type, cursor, { replayed: true }));
  assert.equal(delivery.cursor, 3);
  assert.deepEqual(handled, ["task_started", "done", "chunk"], "completed turns must not resurrect old controls");
});

test("sequence gaps request recovery without confusing restart or replay cursors", () => {
  const gaps: number[] = [];
  const receive = createBackendEventReceiver(() => {}, () => { throw new Error("diagnostic offline"); }, {
    onGap: cursor => gaps.push(cursor),
  });
  receive("not json");
  receive(JSON.stringify({ type: "done", generation: "a", sequence: 1, cursor: 5, replayable: true }));
  receive(JSON.stringify({ type: "chunk", content: "x", generation: "a", sequence: 3, cursor: 5, replayable: false }));
  receive(JSON.stringify({ type: "done", generation: "b", sequence: 1, cursor: 6, replayable: true }));
  receive(JSON.stringify({ type: "done", generation: "a", sequence: 50, cursor: 5, replayed: true, replayable: true }));
  assert.deepEqual(gaps, [5]);
});

test("repeated gaps allow only one pending replay request", () => {
  let requests = 0;
  const receive = createBackendEventReceiver(() => {}, () => {}, { onGap: () => { requests++; } });
  for (const sequence of [1, 3, 5, 7]) receive(JSON.stringify({ type: "chunk", content: "x", generation: "a", sequence }));
  assert.equal(requests, 1);
  receive(JSON.stringify({ type: "event_replay_complete", has_more: false, next_cursor: 0 }));
  receive(JSON.stringify({ type: "chunk", content: "x", generation: "a", sequence: 9 }));
  assert.equal(requests, 2);
});

test("live events cannot advance the cursor past a missing event before paginated recovery finishes", () => {
  const delivery = new EventDeliveryState();
  const applied: number[] = [];
  const requests: number[] = [];
  const handle = (event: { type: string; cursor?: number }) => {
    if (event.type !== "event_replay_complete") applied.push(event.cursor!);
  };
  const frame = (cursor: number, replayed = false) => JSON.stringify({
    type: "process_status", event_id: `id-${cursor}`, cursor, replayable: true,
    generation: "a", sequence: cursor, replayed,
  });
  const receive = createBackendEventReceiver(handle, () => {}, { delivery, onGap: cursor => requests.push(cursor) });
  receive(frame(10));
  receive(frame(12));
  receive(frame(14));
  assert.deepEqual(requests, [10]);
  assert.equal(delivery.cursor, 10);

  // Reconnect while recovery is in flight; the delivery ledger survives.
  const reconnected = createBackendEventReceiver(handle, () => {}, { delivery });
  for (const cursor of [11, 12]) reconnected(frame(cursor, true));
  reconnected(JSON.stringify({ type: "event_replay_complete", has_more: true, next_cursor: 12 }));
  assert.equal(delivery.cursor, 10);
  for (const cursor of [13, 14]) reconnected(frame(cursor, true));
  reconnected(JSON.stringify({ type: "event_replay_complete", has_more: false, next_cursor: 14 }));
  assert.deepEqual(applied, [10, 12, 14, 11, 13]);
  assert.equal(delivery.cursor, 14);
});

import test from "node:test";
import assert from "node:assert/strict";
import { compactionNotice } from "./context-compaction.js";
import type { ContextCompactionEvent } from "./types.js";

test("compaction has start, result, failure and cancellation notices", () => {
  const event: ContextCompactionEvent = { type: "context_compaction", status: "started", messages_before: 168, messages_after: 85 };
  assert.equal(compactionNotice(event), "正在压缩上下文…");
  assert.match(compactionNotice({ ...event, status: "completed" })!, /168 → 85/);
  assert.equal(compactionNotice({ ...event, status: "failed" }), "上下文压缩未完成。");
  assert.equal(compactionNotice({ ...event, status: "cancelled" }), "上下文压缩已取消。");
});

test("invalid compaction events cannot inject terminal text", () => {
  const event: ContextCompactionEvent = { type: "context_compaction", status: "completed", messages_before: 10, messages_after: 5 };
  assert.equal(compactionNotice({ ...event, status: "\x1b[2J" as any }), null);
  for (const count of [-1, 1.5, NaN, Infinity, "\x1b[2J"]) {
    assert.equal(compactionNotice({ ...event, messages_after: count as any }), null);
  }
});

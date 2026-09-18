import assert from "node:assert/strict";
import test from "node:test";

import {
  TurnChangesRenderGate,
  formatTurnChangesSummary,
} from "./turn-changes.js";
import type { TurnChangesEventData, TurnChangesFile } from "./turn-changes.js";

function file(overrides: Partial<TurnChangesFile> = {}): TurnChangesFile {
  return {
    path: "src/a.ts",
    state: "modified",
    added: 4,
    removed: 1,
    compare: "ok",
    reason: "",
    ...overrides,
  };
}

function event(overrides: Partial<TurnChangesEventData> = {}): TurnChangesEventData {
  return {
    session_id: "sess-1",
    request_id: "req-1",
    turn_seq: 1,
    files: [file()],
    unknown_count: 0,
    totals: { files: 1, added: 4, removed: 1 },
    ...overrides,
  };
}

function malformed(payload: Record<string, unknown>): TurnChangesEventData {
  return payload as unknown as TurnChangesEventData;
}

// ---------------------------------------------------------------- formatting

test("files present renders count and totals", () => {
  const line = formatTurnChangesSummary(
    event({
      files: [file({ path: "src/a.ts" }), file({ path: "src/b.ts" })],
      totals: { files: 2, added: 12, removed: 3 },
    }),
  );
  assert.equal(line, "✎ 本轮 2 个文件 +12 −3 · /changes 查看");
});

test("summary uses U+2212 minus and no ASCII hyphen", () => {
  const line = formatTurnChangesSummary(event()) ?? "";
  assert.ok(line.includes("\u2212"), `expected U+2212 in ${JSON.stringify(line)}`);
  assert.ok(!line.includes("-"), `expected no ASCII hyphen in ${JSON.stringify(line)}`);
});

test("unknown_count appends confirmation hint", () => {
  const line = formatTurnChangesSummary(
    event({ totals: { files: 1, added: 5, removed: 2 }, unknown_count: 3 }),
  );
  assert.equal(line, "✎ 本轮 1 个文件 +5 −2 · 另有 3 个路径未能确认 · /changes 查看");
});

test("unknown only renders unknown hint without file count", () => {
  const line = formatTurnChangesSummary(
    event({ files: [], totals: { files: 0, added: 0, removed: 0 }, unknown_count: 2 }),
  );
  assert.equal(line, "✎ 另有 2 个路径未能确认 · /changes 查看");
});

test("no files and no unknown renders nothing", () => {
  const line = formatTurnChangesSummary(
    event({ files: [], totals: { files: 0, added: 0, removed: 0 }, unknown_count: 0 }),
  );
  assert.equal(line, null);
});

test("missing totals fall back to zero counts", () => {
  const line = formatTurnChangesSummary(malformed({
    session_id: "sess-1",
    request_id: "req-1",
    turn_seq: 1,
    files: [file()],
    unknown_count: 0,
  }));
  assert.equal(line, "✎ 本轮 1 个文件 +0 −0 · /changes 查看");
});

test("non numeric totals fall back to zero counts", () => {
  const line = formatTurnChangesSummary(malformed({
    session_id: "sess-1",
    request_id: "req-1",
    turn_seq: 1,
    files: [file(), file({ path: "src/b.ts" })],
    unknown_count: 0,
    totals: { files: 2, added: "7", removed: null },
  }));
  assert.equal(line, "✎ 本轮 2 个文件 +0 −0 · /changes 查看");
});

test("non array files degrade to unknown only or null", () => {
  assert.equal(
    formatTurnChangesSummary(malformed({ files: undefined, unknown_count: 0 })),
    null,
  );
  assert.equal(
    formatTurnChangesSummary(malformed({ files: "oops", unknown_count: 0 })),
    null,
  );
  assert.equal(
    formatTurnChangesSummary(malformed({ files: undefined, unknown_count: 1 })),
    "✎ 另有 1 个路径未能确认 · /changes 查看",
  );
});

test("non numeric unknown_count is treated as zero", () => {
  const line = formatTurnChangesSummary(malformed({
    session_id: "sess-1",
    request_id: "req-1",
    turn_seq: 1,
    files: [file()],
    unknown_count: "2",
    totals: { files: 1, added: 1, removed: 1 },
  }));
  assert.equal(line, "✎ 本轮 1 个文件 +1 −1 · /changes 查看");
});

test("string entries in files do not throw and still count", () => {
  const line = formatTurnChangesSummary(malformed({
    session_id: "sess-1",
    request_id: "req-1",
    turn_seq: 1,
    files: ["", file()],
    unknown_count: 0,
    totals: { files: 2, added: 2, removed: 2 },
  }));
  assert.equal(line, "✎ 本轮 2 个文件 +2 −2 · /changes 查看");
});

test("malformed root object does not throw", () => {
  assert.equal(formatTurnChangesSummary(malformed({})), null);
  assert.doesNotThrow(() => formatTurnChangesSummary(malformed({ files: null })));
});

test("cancelled turn with files renders like a normal turn", () => {
  const line = formatTurnChangesSummary(
    event({ files: [file({ reason: "cancelled" })], totals: { files: 1, added: 4, removed: 1 } }),
  );
  assert.equal(line, "✎ 本轮 1 个文件 +4 −1 · /changes 查看");
});

test("cancelled turn with unknown only renders unknown hint", () => {
  const line = formatTurnChangesSummary(
    event({ files: [], unknown_count: 2, totals: { files: 0, added: 0, removed: 0 } }),
  );
  assert.equal(line, "✎ 另有 2 个路径未能确认 · /changes 查看");
});

// --------------------------------------------------------------------- gate

test("gate renders first sight and suppresses the duplicate", () => {
  const gate = new TurnChangesRenderGate();
  const payload = event();
  assert.equal(gate.shouldRender(payload, "sess-1"), true);
  assert.equal(gate.shouldRender(payload, "sess-1"), false);
  assert.equal(gate.shouldRender(payload, "sess-1"), false);
});

test("gate allows distinct turn_seq and request_id", () => {
  const gate = new TurnChangesRenderGate();
  assert.equal(gate.shouldRender(event({ turn_seq: 1 }), "sess-1"), true);
  assert.equal(gate.shouldRender(event({ turn_seq: 2 }), "sess-1"), true);
  assert.equal(gate.shouldRender(event({ request_id: "req-2" }), "sess-1"), true);
});

test("gate blocks events from another session", () => {
  const gate = new TurnChangesRenderGate();
  assert.equal(gate.shouldRender(event({ session_id: "sess-1" }), "sess-2"), false);
  assert.equal(gate.shouldRender(event({ session_id: "sess-1" }), "sess-2"), false);
  // the blocked event must not poison the key for the owning session
  assert.equal(gate.shouldRender(event({ session_id: "sess-1" }), "sess-1"), true);
});

test("gate passes everything while current session is unknown", () => {
  const gate = new TurnChangesRenderGate();
  assert.equal(gate.shouldRender(event({ session_id: "sess-9" }), ""), true);
  assert.equal(gate.shouldRender(event({ session_id: "sess-9" }), ""), false);
});

test("gate tolerates missing ids without throwing", () => {
  const gate = new TurnChangesRenderGate();
  const payload = malformed({ session_id: "sess-1" });
  assert.equal(gate.shouldRender(payload, "sess-1"), true);
  assert.equal(gate.shouldRender(payload, "sess-1"), false);
});

test("gate keeps at most 4096 keys and evicts the oldest", () => {
  const gate = new TurnChangesRenderGate();
  const payloads: TurnChangesEventData[] = [];
  for (let index = 0; index < 4096; index += 1) {
    const payload = event({ turn_seq: index });
    payloads.push(payload);
    assert.equal(gate.shouldRender(payload, "sess-1"), true);
    assert.equal(gate.shouldRender(payload, "sess-1"), false);
  }
  // 4097th key pushes the set over the cap, evicting the oldest entry
  const extra = event({ turn_seq: 4096 });
  assert.equal(gate.shouldRender(extra, "sess-1"), true);
  assert.equal(gate.shouldRender(payloads[0]!, "sess-1"), true);
  assert.equal(gate.shouldRender(payloads[4095]!, "sess-1"), false);
});

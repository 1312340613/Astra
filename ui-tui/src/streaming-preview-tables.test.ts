import assert from "node:assert/strict";
import { test } from "node:test";

import { StreamingMarkdownCoordinator } from "./streaming-markdown.js";
import { streamingUpdateToRenderLines } from "./app.js";
import type { RuntimeMode } from "./types.js";

const ROWS = [
  "| | **GLM-5.3 Flash**（智谱） | **Qwen3.8 Flash**（阿里） |",
  "|---|---|---|",
  "| 发布 | 8-26 下午 | 8-26 晚 |",
  "| 每 token 激活 | 未公布 | 仅 6B |",
];

function runThrough(prefixLines: string[], columns: number) {
  const coordinator = new StreamingMarkdownCoordinator();
  const committedBorders: string[] = [];
  let previewLeak = "";
  let generation = 0;
  const hasBorder = (text: string) => ["┌", "├", "└"].some((b) => text.includes(b));
  for (let i = 0; i < prefixLines.length; i++) {
    coordinator.start("assistant");
    const update = coordinator.push("assistant", `${prefixLines[i]}\n`);
    const { committed, preview } = streamingUpdateToRenderLines(
      "assistant",
      update,
      columns,
      "work" as RuntimeMode,
      `stream-assistant-${generation++}`,
    );
    for (const l of committed.lines) {
      if (hasBorder(l.text ?? "")) committedBorders.push(l.text ?? "");
    }
    const joined = preview.lines.map((l) => l.text ?? "").join("\n");
    if (hasBorder(joined) && !previewLeak) previewLeak = joined;
  }
  coordinator.start("assistant");
  const tail = streamingUpdateToRenderLines(
    "assistant",
    coordinator.finish("assistant"),
    columns,
    "work" as RuntimeMode,
    `stream-assistant-final`,
  );
  for (const l of tail.committed.lines) {
    if (hasBorder(l.text ?? "")) committedBorders.push(l.text ?? "");
  }
  const tailPreview = tail.preview.lines.map((l) => l.text ?? "").join("\n");
  if (hasBorder(tailPreview) && !previewLeak) previewLeak = tailPreview;
  return { committedBorders, previewLeak };
}

test("open-table preview frames stay border-free (stale-frame hardening)", () => {
  // Feed up to each prefix where the divider row exists but the table has not
  // closed yet: live preview frames must never paint box borders.
  for (let cut = 3; cut < ROWS.length; cut++) {
    const { previewLeak } = runThrough(ROWS.slice(0, cut), 100);
    assert.equal(previewLeak, "", `preview leaked bordered frame at cut=${cut}: ${previewLeak}`);
  }
});

test("committed output still paints the boxed table", () => {
  const { committedBorders } = runThrough(ROWS, 100);
  assert.ok(committedBorders.some((t) => t.includes("┌")), "expected boxed table in committed output");
});

import assert from "node:assert/strict";
import stringWidth from "string-width";
import {
  compatibleMarkdownTableRow,
  consumeStreamingTableLine,
  isMarkdownTableStart,
  renderMarkdownTable,
  terminalWidth,
} from "./markdown-table.js";

const source = [
  "| 节点 | 事件 |",
  "|:---|:---|",
  "| 2026-04-24 | V4 预览版上线 |",
  "| 今天 | 正式版理论上就在这一两周内上线 |",
];

const wide = renderMarkdownTable(source, 0, 100);
assert.ok(wide);
assert.equal(wide.mode, "grid");
assert.equal(wide.consumed, 4);
assert.ok(wide.lines[0].startsWith("┌"));
assert.ok(wide.lines.some((line) => line.includes("V4 预览版上线")));
assert.ok(wide.lines.every((line) => terminalWidth(line) <= 100));

const narrow = renderMarkdownTable(source, 0, 48);
assert.ok(narrow);
assert.equal(narrow.mode, "stacked");
assert.ok(narrow.lines.some((line) => line.includes("节点: 2026-04-24")));
assert.ok(narrow.lines.some((line) => line.includes("事件: V4 预览版上线")));

assert.equal(renderMarkdownTable(["not a table", "still not"], 0, 100), null);
assert.equal(terminalWidth("中文AI"), 6);
assert.equal(isMarkdownTableStart(source[0], source[1]), true);

const graphemeFixtures = [
  { grapheme: "👩‍👩‍👧‍👦", prefix: "a".repeat(12) },
  { grapheme: "e\u0301", prefix: "a".repeat(13) },
  { grapheme: "🇸🇬", prefix: "a".repeat(13) },
  { grapheme: "क्‍ष", prefix: "a".repeat(13) },
];
for (const { grapheme, prefix } of graphemeFixtures) {
  assert.equal(terminalWidth(grapheme), stringWidth(grapheme), `${grapheme} terminal width`);
  const wrappedGrapheme = renderMarkdownTable([
    "Kind | Value",
    "--- | ---",
    `fixture | ${prefix}${grapheme}Z`,
  ], 0, 24);
  assert.ok(wrappedGrapheme);
  assert.equal(wrappedGrapheme.mode, "stacked");
  assert.equal(
    wrappedGrapheme.lines.filter((line) => line.includes(grapheme)).length,
    1,
    `${grapheme} stays intact while a table cell wraps`,
  );
  assert.equal(wrappedGrapheme.lines.every((line) => terminalWidth(line) <= 24), true);
}

const stableUnicodeGrid = renderMarkdownTable([
  "| 列 A | 列 B |",
  "|---|---|",
  "| 👩‍👩‍👧‍👦 家庭 | 🏳️‍🌈 👩‍⚕️ 👨‍❤️‍👨 |",
  "| 🇸🇬国旗 | 中文 |",
], 0, 100);
assert.ok(stableUnicodeGrid);
assert.equal(stableUnicodeGrid.mode, "grid");

const ambiguousUnicodeTable = renderMarkdownTable([
  "| 列 A | 列 B |",
  "|---|---|",
  "| 👩‍👩‍👧‍👦 家庭 | e\u0301 组合重音 |",
  "| 🇸🇬国旗 | क्‍ष 连字 |",
], 0, 100);
assert.ok(ambiguousUnicodeTable);
assert.equal(ambiguousUnicodeTable.mode, "stacked");
assert.ok(ambiguousUnicodeTable.lines.some((line) => line.includes("क्‍ष 连字")));

for (const compatible of [
  ["A | B", "--- | ---", "x | y"],
  ["| A | B", "--- | --- |", "| x | y"],
  ["A | B |", "| --- | ---", "x | y |"],
]) {
  assert.equal(isMarkdownTableStart(compatible[0], compatible[1]), true, compatible.join("\n"));
  const rendered = renderMarkdownTable(compatible, 0, 80);
  assert.ok(rendered, compatible.join("\n"));
  assert.equal(rendered.consumed, 3);

  let compatiblePending: string[] = [];
  const compatibleBlocks: Array<{ kind: "text" | "table"; lines: string[] }> = [];
  for (const line of [...compatible, ""]) {
    const decision = consumeStreamingTableLine(compatiblePending, line);
    compatiblePending = decision.pending;
    compatibleBlocks.push(...decision.blocks);
  }
  assert.deepEqual(compatiblePending, []);
  assert.deepEqual(
    compatibleBlocks.filter((block) => block.kind === "table").map((block) => block.lines),
    [compatible],
    compatible.join("\n"),
  );
}

const truncated = renderMarkdownTable([
  "| Model | Type | Precision | Link |",
  "|---|---|---|---|",
  "| Base | pre-trained | BF16 | Hugging Face |",
  "| Instruct | post-trained | FP8 | Hugging Face | ...",
  "| M...",
], 0, 110);
assert.ok(truncated);
assert.equal(truncated.consumed, 5);
assert.ok(truncated.lines.some((line) => line.includes("Instruct")));
assert.ok(truncated.lines.some((line) => line.includes("M...")));
assert.deepEqual(
  compatibleMarkdownTableRow("| Instruct | post-trained | FP8 | Hugging Face | ...", 4),
  ["Instruct", "post-trained", "FP8", "Hugging Face"],
  "provider prose after the last complete pipe remains a supported truncation fallback",
);

const explicitSurplus = "| x | y | z |";
assert.equal(
  compatibleMarkdownTableRow(explicitSurplus, 2),
  null,
  "a complete surplus cell must not be silently truncated",
);
assert.equal(
  renderMarkdownTable(["| A | B |", "| --- | --- |", explicitSurplus], 0, 80),
  null,
  "complete rendering rejects an explicit surplus row",
);

let surplusPending: string[] = [];
for (const line of ["| A | B |", "| --- | --- |"]) {
  surplusPending = consumeStreamingTableLine(surplusPending, line).pending;
}
const surplusDecision = consumeStreamingTableLine(surplusPending, explicitSurplus);
assert.deepEqual(surplusDecision.pending, [explicitSurplus]);
assert.deepEqual(
  surplusDecision.blocks,
  [{ kind: "table", lines: ["| A | B |", "| --- | --- |"] }],
  "incremental parsing leaves the explicit surplus row outside the table",
);
const flushedSurplus = consumeStreamingTableLine(surplusDecision.pending, "");
assert.deepEqual(flushedSurplus.pending, []);
assert.deepEqual(flushedSurplus.blocks[0], { kind: "text", lines: [explicitSurplus] });

const mixedPipeSurplus = "| x | y | z";
assert.equal(
  compatibleMarkdownTableRow(mixedPipeSurplus, 2),
  null,
  "leading-only outer-pipe syntax makes z an explicit surplus cell",
);
assert.equal(
  renderMarkdownTable(["| A | B |", "| --- | --- |", mixedPipeSurplus], 0, 80),
  null,
  "complete rendering preserves a mixed-pipe surplus row outside the table",
);

let mixedPipePending: string[] = [];
for (const line of ["| A | B |", "| --- | --- |"]) {
  mixedPipePending = consumeStreamingTableLine(mixedPipePending, line).pending;
}
const mixedPipeDecision = consumeStreamingTableLine(mixedPipePending, mixedPipeSurplus);
assert.deepEqual(mixedPipeDecision.pending, [mixedPipeSurplus]);
assert.deepEqual(
  mixedPipeDecision.blocks,
  [{ kind: "table", lines: ["| A | B |", "| --- | --- |"] }],
  "incremental parsing leaves the mixed-pipe surplus row lossless",
);
const flushedMixedPipe = consumeStreamingTableLine(mixedPipeDecision.pending, "");
assert.deepEqual(flushedMixedPipe.pending, []);
assert.deepEqual(flushedMixedPipe.blocks[0], { kind: "text", lines: [mixedPipeSurplus] });

// Simulate a model response arriving one completed line at a time. No table
// line may be emitted as plain text before the block-ending blank line arrives.
let pending: string[] = [];
const streamedBlocks: Array<{ kind: "text" | "table"; lines: string[] }> = [];
for (const line of [...source, ""]) {
  const decision = consumeStreamingTableLine(pending, line);
  pending = decision.pending;
  streamedBlocks.push(...decision.blocks);
}
assert.deepEqual(pending, []);
assert.equal(streamedBlocks.filter((block) => block.kind === "table").length, 1);
assert.deepEqual(streamedBlocks.find((block) => block.kind === "table")?.lines, source);
assert.equal(
  streamedBlocks.some((block) => block.kind === "text" && block.lines.some((line) => line.startsWith("|"))),
  false,
);

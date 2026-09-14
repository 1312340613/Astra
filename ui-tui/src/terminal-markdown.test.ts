import assert from "node:assert/strict";
import {
  MAX_PENDING_BLOCK_CHARS,
  MAX_PENDING_INLINE_CHARS,
  MAX_PENDING_TABLE_LINES,
  TerminalMarkdownStream,
  parseCompleteMarkdown,
  parseInlineMarkdown,
  type MarkdownBlock,
} from "./terminal-markdown.js";

function blockText(block: MarkdownBlock): string {
  if (block.kind === "paragraph" || block.kind === "heading" || block.kind === "quote" || block.kind === "list") {
    return block.spans.map((span) => span.text).join("");
  }
  if (block.kind === "code") return block.text;
  if (block.kind === "table") return block.source.join("\n");
  if (block.kind === "math") return block.source;
  return block.kind === "blank" ? "" : "---";
}

function previewText(parser: TerminalMarkdownStream): string {
  return parser.preview().map(blockText).join("\n");
}

const source = [
  "# P21: Cascade 与 Switching",
  "   ### Re-Ranking",
  "",
  "系统会自动**更信任平时表现好的推荐器**，并淘汰~~旧策略~~。",
  "",
  "```",
  "CF coarse → DNN rerank → Top N",
  "```",
  "",
  "- 新用户 → Popularity",
  "> 这是引用",
  "",
].join("\n");

const complete = parseCompleteMarkdown(source);
assert.equal(complete.some((block) => block.kind === "heading"), true);
assert.deepEqual(
  complete.filter((block) => block.kind === "heading").map(blockText),
  ["P21: Cascade 与 Switching", "Re-Ranking"],
);
assert.equal(complete.some((block) => block.kind === "code"), true);
assert.equal(JSON.stringify(complete).includes("```"), false);
assert.equal(JSON.stringify(complete).includes("**"), false);
assert.equal(JSON.stringify(complete).includes("~~"), false);
assert.equal(
  complete.some((block) =>
    block.kind === "paragraph" && block.spans.some((span) => span.kind === "strikethrough" && span.text === "旧策略")
  ),
  true,
);

function streamed(chunks: string[]): MarkdownBlock[] {
  const parser = new TerminalMarkdownStream();
  const committed: MarkdownBlock[] = [];
  for (const chunk of chunks) committed.push(...parser.push(chunk).committed);
  committed.push(...parser.finish());
  return committed;
}

for (const breakTag of ["<br>", "<br/>", "<br />", "<BR>"]) {
  assert.deepEqual(parseInlineMarkdown(`上半${breakTag}下半`, true), [
    { kind: "text", text: "上半" },
    { kind: "hard_break", text: "" },
    { kind: "text", text: "下半" },
  ]);
  for (let index = 1; index < breakTag.length; index += 1) {
    assert.deepEqual(
      streamed([`上半${breakTag.slice(0, index)}`, `${breakTag.slice(index)}下半`]),
      parseCompleteMarkdown(`上半${breakTag}下半`),
      `break tag boundary ${JSON.stringify(breakTag)} at ${index}`,
    );
  }
}

const partialBreak = new TerminalMarkdownStream();
partialBreak.push("上半<br");
assert.equal(previewText(partialBreak), "上半");
assert.deepEqual(
  [...partialBreak.push(">下半").committed, ...partialBreak.finish()],
  parseCompleteMarkdown("上半<br>下半"),
);
assert.deepEqual(parseInlineMarkdown("`<br>`", true), [{ kind: "code", text: "<br>", href: undefined }]);
assert.deepEqual(parseInlineMarkdown("<broken>", true), [{ kind: "text", text: "<broken>" }]);
for (const { source: styledSource, kind } of [
  { source: "**上半<br>下半**", kind: "strong" },
  { source: "*上半<br/>下半*", kind: "emphasis" },
  { source: "~~上半<br />下半~~", kind: "strikethrough" },
  { source: "[上半<br>下半](https://example.test)", kind: "link" },
] as const) {
  const styledBreak = parseInlineMarkdown(styledSource, true);
  assert.deepEqual(styledBreak.map((span) => span.kind), [kind, "hard_break", kind]);
  assert.equal(styledBreak.map((span) => span.text).join(""), "上半下半");
  if (kind === "link") {
    assert.equal(styledBreak.filter((span) => span.kind === "link").every((span) => span.href === "https://example.test"), true);
  }
  for (let index = 1; index < styledSource.length; index += 1) {
    assert.deepEqual(
      streamed([styledSource.slice(0, index), styledSource.slice(index)]),
      parseCompleteMarkdown(styledSource),
      `styled break boundary ${kind} at ${index}`,
    );
  }
}

const unmatchedInlineThenStrong =
  "……我错了。 (´;ω;`) 你那组数据是对的——**8bit 生成 69.8 tok/s，不是 7.7**。";
const recoveredInline = parseInlineMarkdown(unmatchedInlineThenStrong, true);
assert.equal(
  recoveredInline.map((span) => span.text).join(""),
  "……我错了。 (´;ω;`) 你那组数据是对的——8bit 生成 69.8 tok/s，不是 7.7。",
);
assert.equal(
  recoveredInline.some((span) => span.kind === "strong" && span.text === "8bit 生成 69.8 tok/s，不是 7.7"),
  true,
);
for (let index = 1; index < unmatchedInlineThenStrong.length; index += 1) {
  assert.deepEqual(
    streamed([unmatchedInlineThenStrong.slice(0, index), unmatchedInlineThenStrong.slice(index)]),
    parseCompleteMarkdown(unmatchedInlineThenStrong),
    `unmatched inline recovery boundary ${index}`,
  );
}

assert.deepEqual(streamed([source]), complete);
assert.deepEqual(streamed([...source]), complete);
assert.deepEqual(
  streamed(["# P21\n系统会自动**更信", "任**。\n```\nCF", " → Top N\n```\n"]),
  parseCompleteMarkdown("# P21\n系统会自动**更信任**。\n```\nCF → Top N\n```\n"),
);

const openStrong = new TerminalMarkdownStream();
const strongPreview = openStrong.push("before **pending").preview;
const strongBlock = strongPreview[0];
assert.equal(strongBlock.kind, "paragraph");
if (strongBlock.kind !== "paragraph") throw new Error("expected paragraph preview");
assert.equal(strongBlock.spans.some((span) => span.kind === "strong"), true);

const finalizedStrong = openStrong.finish()[0];
assert.equal(finalizedStrong.kind, "paragraph");
if (finalizedStrong.kind !== "paragraph") throw new Error("expected finalized paragraph");
assert.equal(finalizedStrong.spans.map((span) => span.text).join(""), "before **pending");

const openFence = new TerminalMarkdownStream();
openFence.push("```ts\nconst x = 1;");
assert.equal(JSON.stringify(openFence.preview()).includes("```"), false);
assert.equal(openFence.finish().some((block) => block.kind === "code"), true);

const chunkedSource = "前缀 **跨宽度粗体**、~~删除线~~、`code` 和 [链接](https://example.com)";
for (let index = 1; index < chunkedSource.length; index += 1) {
  assert.deepEqual(
    streamed([chunkedSource.slice(0, index), chunkedSource.slice(index)]),
    parseCompleteMarkdown(chunkedSource),
    `chunk boundary ${index}`,
  );
}

const oversized = `**${"x".repeat(MAX_PENDING_INLINE_CHARS + 1)}`;
const boundedInline = new TerminalMarkdownStream();
const oversizedRelease = boundedInline.push(oversized);
assert.ok(oversizedRelease.committed.length > 0, "oversized inline source must be released");
assert.deepEqual(oversizedRelease.preview, []);
assert.equal(oversizedRelease.committed.every((block) => block.kind === "paragraph"), true);
const boundedInlineBlocks = [...oversizedRelease.committed];
for (let index = 0; index < 3; index += 1) {
  const literalChunk = `tail-${index}-${"y".repeat(MAX_PENDING_BLOCK_CHARS)}`;
  const update = boundedInline.push(literalChunk);
  assert.ok(update.committed.length > 0, `released literal chunk ${index} must not accumulate`);
  assert.deepEqual(update.preview, []);
  boundedInlineBlocks.push(...update.committed);
}
const lateInlineCloser = boundedInline.push("**\n");
boundedInlineBlocks.push(...lateInlineCloser.committed, ...boundedInline.finish());
assert.equal(
  boundedInlineBlocks.map(blockText).join(""),
  `${oversized}${[0, 1, 2].map((index) => `tail-${index}-${"y".repeat(MAX_PENDING_BLOCK_CHARS)}`).join("")}**`,
);
assert.equal(
  boundedInlineBlocks.flatMap((block) => block.kind === "paragraph" ? block.spans : []).every((span) => span.kind === "text"),
  true,
  "a closer after release must stay literal",
);
const completeOversized = parseCompleteMarkdown(`${oversized}**`);
assert.equal(completeOversized.map(blockText).join(""), `${oversized}**`);
assert.equal(completeOversized[0]?.kind, "paragraph");
if (completeOversized[0]?.kind !== "paragraph") throw new Error("expected literal oversized paragraph");
assert.equal(completeOversized[0].spans.every((span) => span.kind === "text"), true);

const singleHugeChunk = new TerminalMarkdownStream();
let observedPendingLine = (singleHugeChunk as unknown as { pendingLine: string }).pendingLine;
Object.defineProperty(singleHugeChunk, "pendingLine", {
  configurable: true,
  get: () => observedPendingLine,
  set: (value: string) => {
    assert.ok(
      value.length <= MAX_PENDING_BLOCK_CHARS,
      `pendingLine assignment exceeded the hard bound: ${value.length}`,
    );
    observedPendingLine = value;
  },
});
const hugeNoNewline = "H".repeat(MAX_PENDING_BLOCK_CHARS * 4 + 37);
const hugeUpdate = singleHugeChunk.push(hugeNoNewline);
assert.deepEqual(hugeUpdate.preview, []);
const hugeBlocks = [...hugeUpdate.committed, ...singleHugeChunk.finish()];
const hugeBlockTexts = hugeBlocks.map(blockText);
assert.equal(hugeBlockTexts.join(""), hugeNoNewline, "one huge chunk is emitted exact-once");
assert.equal(
  hugeBlockTexts.every((text) => text.length <= MAX_PENDING_BLOCK_CHARS),
  true,
  "every released block remains bounded",
);
assert.equal(
  hugeBlockTexts.slice(0, -1).every((text) => text.length === MAX_PENDING_BLOCK_CHARS),
  true,
  "the parser releases full fixed-size blocks before the final remainder",
);

const terminalBoundaryFixtures = [
  { name: "CSI", token: "\x1b[31mX", splitAt: 1 },
  { name: "OSC", token: "\x1b]0;title\x1b\\X", splitAt: 4 },
  { name: "surrogate pair", token: "🚀", splitAt: 1 },
  { name: "family ZWJ", token: "👩‍👩‍👧‍👦", splitAt: 2 },
  { name: "combining mark", token: "e\u0301", splitAt: 1 },
  { name: "regional flag", token: "🇸🇬", splitAt: 2 },
  { name: "Indic conjunct", token: "क्‍ष", splitAt: 2 },
];

for (const fenced of [false, true]) {
  for (const fixture of terminalBoundaryFixtures) {
    const parser = new TerminalMarkdownStream();
    const blocks: MarkdownBlock[] = [];
    if (fenced) blocks.push(...parser.push("```\n").committed);
    const prefix = "a".repeat(MAX_PENDING_BLOCK_CHARS - fixture.splitAt);
    blocks.push(...parser.push(prefix + fixture.token.slice(0, fixture.splitAt)).committed);
    blocks.push(...parser.push(fixture.token.slice(fixture.splitAt)).committed);
    if (fenced) blocks.push(...parser.push("\n```\n").committed);
    blocks.push(...parser.finish());
    const texts = blocks.map(blockText);
    const context = `${fenced ? "fenced" : "ordinary"} ${fixture.name}`;
    assert.equal(texts.join(""), prefix + fixture.token, `${context} is emitted exact-once`);
    assert.equal(
      texts.every((text) => text.length <= MAX_PENDING_BLOCK_CHARS),
      true,
      `${context} blocks remain bounded`,
    );
    assert.equal(
      texts.filter((text) => text.includes(fixture.token)).length,
      1,
      `${context} stays inside one released block`,
    );
  }
}

for (const sourceWithDelimiter of [
  "`code`",
  "\\(x_{CJK}\\)",
  "$x^2$",
  "**strong**",
  "__strong__",
  "*emphasis*",
  "~~strikethrough~~",
  "[链接](https://example.com)",
]) {
  for (let index = 1; index < sourceWithDelimiter.length; index += 1) {
    assert.deepEqual(
      streamed([sourceWithDelimiter.slice(0, index), sourceWithDelimiter.slice(index)]),
      parseCompleteMarkdown(sourceWithDelimiter),
      `delimiter boundary ${JSON.stringify(sourceWithDelimiter)} at ${index}`,
    );
  }
}

for (const malformed of ["`unclosed", "\\(unclosed", "$unclosed", "**unclosed", "__unclosed", "*unclosed", "~~unclosed", "[label](unclosed"]) {
  const blocks = parseCompleteMarkdown(malformed);
  assert.equal(blocks[0]?.kind, "paragraph");
  if (blocks[0]?.kind !== "paragraph") throw new Error("expected malformed paragraph");
  assert.equal(blocks[0].spans.map((span) => span.text).join(""), malformed);
}

for (const emptyConstruct of ["``", "\\(\\)", "****", "____", "~~~~", "[x]()", "[](url)"]) {
  const spans = parseInlineMarkdown(`${emptyConstruct} then **valid**`, true);
  assert.equal(spans[0]?.kind, "text", `${emptyConstruct} must remain literal`);
  assert.equal(spans[0]?.text, `${emptyConstruct} then `, `${emptyConstruct} must remain lossless`);
  assert.deepEqual(spans[1], { kind: "strong", text: "valid", href: undefined });
}

assert.deepEqual(
  parseInlineMarkdown("``code`", true),
  [{ kind: "text", text: "`" }, { kind: "code", text: "code", href: undefined }],
  "the close of an empty code candidate remains a possible code opener",
);

const displayMath = parseCompleteMarkdown("$$\\frac{1}{2}$$\n\\[x^2\\]\n$$\na + b\n$$\n");
assert.equal(displayMath.filter((block) => block.kind === "math").length, 3);

const unfinishedMath = new TerminalMarkdownStream();
unfinishedMath.push("$$\na + b");
const unfinishedMathPreview = unfinishedMath.preview()[0];
assert.equal(unfinishedMathPreview.kind, "math");
if (unfinishedMathPreview.kind !== "math") throw new Error("expected math preview");
assert.equal(unfinishedMathPreview.malformed, true);
const unfinishedMathFinal = unfinishedMath.finish()[0];
assert.equal(unfinishedMathFinal.kind, "math");
if (unfinishedMathFinal.kind !== "math") throw new Error("expected malformed math");
assert.equal(unfinishedMathFinal.malformed, true);

const unfinishedBracketMath = parseCompleteMarkdown("\\[\nx^2");
assert.equal(unfinishedBracketMath[0]?.kind, "math");
if (unfinishedBracketMath[0]?.kind !== "math") throw new Error("expected malformed bracket math");
assert.equal(unfinishedBracketMath[0].malformed, true);

const boundedMath = new TerminalMarkdownStream();
const boundedMathBlocks: MarkdownBlock[] = [];
boundedMathBlocks.push(...boundedMath.push("$$\n").committed);
let mathReleased = false;
for (let index = 0; index < 6; index += 1) {
  const update = boundedMath.push(`${"m".repeat(2_048)}\n`);
  boundedMathBlocks.push(...update.committed);
  mathReleased ||= update.committed.length > 0;
}
assert.equal(mathReleased, true, "open math must release before buffering a whole response");
const mathTail = boundedMath.push("after-release\n");
assert.ok(mathTail.committed.length > 0);
assert.deepEqual(mathTail.preview, []);
boundedMathBlocks.push(...mathTail.committed);
const mathCloser = boundedMath.push("$$\n");
boundedMathBlocks.push(...mathCloser.committed, ...boundedMath.finish());
assert.equal(boundedMathBlocks.some((block) => block.kind === "math"), false);
assert.deepEqual(
  boundedMathBlocks.map(blockText),
  ["$$", ...Array.from({ length: 6 }, () => "m".repeat(2_048)), "after-release", "$$"],
);

for (const rawOpener of ["  $$   ", "\t\\[  "]) {
  const rawMath = new TerminalMarkdownStream();
  const rawMathBlocks: MarkdownBlock[] = [];
  rawMathBlocks.push(...rawMath.push(`${rawOpener}\n`).committed);
  for (let index = 0; index < 6; index += 1) {
    rawMathBlocks.push(...rawMath.push(`${"r".repeat(2_048)}\n`).committed);
  }
  const rawCloser = rawOpener.trim() === "$$" ? "$$" : "\\]";
  rawMathBlocks.push(...rawMath.push(`${rawCloser}\n`).committed, ...rawMath.finish());
  assert.deepEqual(
    rawMathBlocks.map(blockText),
    [rawOpener, ...Array.from({ length: 6 }, () => "r".repeat(2_048)), rawCloser],
    `bounded display math replays the raw opener ${JSON.stringify(rawOpener)}`,
  );
}

const tableSource = "| 名称 | 值 |\n|---|---|\n| 中文 | 42 |\n\n";
const parsedTable = parseCompleteMarkdown(tableSource);
assert.equal(parsedTable.some((block) => block.kind === "table"), true);
assert.deepEqual(streamed([...tableSource]), parsedTable);

const openTable = new TerminalMarkdownStream();
openTable.push("| 名称 | 值 |\n|---|---|\n| 中文 | 42 |");
// Stale-frame hardening: while the table is still open, live preview renders
// plain pipe text only; the boxed layout is painted once by the committed
// block when the table closes.
assert.equal(openTable.preview().every((block) => block.kind !== "table"), true);
assert.equal(openTable.finish()[0]?.kind, "table");

for (const compatibleTable of [
  "A | B\n--- | ---\nx | y\n\n",
  "| A | B\n--- | --- |\n| x | y\n\n",
  "A | B |\n| --- | ---\nx | y |\n\n",
]) {
  const parsed = parseCompleteMarkdown(compatibleTable);
  assert.equal(parsed.filter((block) => block.kind === "table").length, 1, compatibleTable);
  assert.deepEqual(streamed([...compatibleTable]), parsed, compatibleTable);
}

const boundedTable = new TerminalMarkdownStream();
const tableRows = Array.from({ length: MAX_PENDING_TABLE_LINES }, (_item, index) => `x${index} | y${index}`);
const boundedTableSource = ["A | B", "--- | ---", ...tableRows].join("\n") + "\n";
const tableRelease = boundedTable.push(boundedTableSource);
assert.ok(tableRelease.committed.length > 0, "table lookahead must release at its line bound");
assert.equal(tableRelease.committed.some((block) => block.kind === "table"), false);
assert.deepEqual(tableRelease.preview, []);
const releasedRow = boundedTable.push("later | row\n");
assert.ok(releasedRow.committed.length > 0);
assert.deepEqual(releasedRow.preview, []);
const tableBoundary = boundedTable.push("ordinary boundary\n");
const boundedTableBlocks = [
  ...tableRelease.committed,
  ...releasedRow.committed,
  ...tableBoundary.committed,
  ...boundedTable.finish(),
];
assert.deepEqual(
  boundedTableBlocks.map(blockText),
  ["A | B", "--- | ---", ...tableRows, "later | row", "ordinary boundary"],
);

const boundedTableChars = new TerminalMarkdownStream();
const wideTableRow = `${"x".repeat(MAX_PENDING_BLOCK_CHARS - 16)} | y`;
const wideTableRelease = boundedTableChars.push(`A | B\n--- | ---\n${wideTableRow}\n`);
assert.ok(wideTableRelease.committed.length > 0, "table lookahead must release at its character bound");
assert.equal(wideTableRelease.committed.some((block) => block.kind === "table"), false);
assert.deepEqual(wideTableRelease.preview, []);
const wideTableTail = boundedTableChars.push("later | row\nordinary boundary\n");
const wideTableBlocks = [
  ...wideTableRelease.committed,
  ...wideTableTail.committed,
  ...boundedTableChars.finish(),
];
assert.deepEqual(
  wideTableBlocks.map(blockText),
  ["A | B", "--- | ---", wideTableRow, "later | row", "ordinary boundary"],
);

for (const headingPrefix of ["#", "# "]) {
  const parser = new TerminalMarkdownStream();
  parser.push(headingPrefix);
  assert.equal(previewText(parser), "", `heading prefix ${JSON.stringify(headingPrefix)}`);
}

const headingSplits = new TerminalMarkdownStream();
for (const chunk of ["#", " ", "H", "eading"]) {
  headingSplits.push(chunk);
  assert.doesNotMatch(previewText(headingSplits), /#/u);
}

const fenceOpenSplits = new TerminalMarkdownStream();
for (const chunk of ["`", "`", "`", "t", "s"]) {
  fenceOpenSplits.push(chunk);
  assert.doesNotMatch(previewText(fenceOpenSplits), /`/u);
}

const fenceCloseSplits = new TerminalMarkdownStream();
fenceCloseSplits.push("```ts\nbody\n");
for (const chunk of ["`", "`", "`"]) {
  fenceCloseSplits.push(chunk);
  assert.equal(previewText(fenceCloseSplits), "", "pending fence closer must stay hidden");
}

const mathOpenSplits = new TerminalMarkdownStream();
for (const chunk of ["$", "$"]) {
  mathOpenSplits.push(chunk);
  assert.doesNotMatch(previewText(mathOpenSplits), /\$/u);
}

const mathCloseSplits = new TerminalMarkdownStream();
mathCloseSplits.push("$$\nvalue\n");
for (const chunk of ["$", "$"]) {
  mathCloseSplits.push(chunk);
  assert.doesNotMatch(previewText(mathCloseSplits), /\$/u);
}

const bracketOneLineSplits = new TerminalMarkdownStream();
for (const [chunk, expected] of [
  ["\\", ""],
  ["[", ""],
  ["x", "x"],
  ["\\", "x"],
  ["]", "x"],
] as const) {
  bracketOneLineSplits.push(chunk);
  assert.equal(previewText(bracketOneLineSplits), expected, `one-line bracket chunk ${JSON.stringify(chunk)}`);
}

const bracketBlockSplits = new TerminalMarkdownStream();
for (const chunk of ["\\", "["]) {
  bracketBlockSplits.push(chunk);
  assert.equal(previewText(bracketBlockSplits), "", "pending bracket opener must stay hidden");
}
bracketBlockSplits.push("\nvalue\n");
for (const chunk of ["\\", "]"]) {
  bracketBlockSplits.push(chunk);
  assert.equal(previewText(bracketBlockSplits), "value", "pending bracket closer must stay hidden");
}

const unclosedFence = parseCompleteMarkdown("```ts\nconst 中文 = 1;");
assert.deepEqual(unclosedFence, [{ kind: "code", text: "const 中文 = 1;" }]);

console.log("terminal markdown parser tests passed");

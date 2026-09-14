import assert from "node:assert/strict";
import stripAnsi from "strip-ansi";
import { markdownBlocksToLines, wrapInlineSpans } from "./markdown-render-lines.js";
import { terminalWidth } from "./markdown-table.js";
import { parseCompleteMarkdown, type InlineSpan, type MarkdownBlock } from "./terminal-markdown.js";

const spans: InlineSpan[] = [
  { kind: "text", text: "系统会自动" },
  { kind: "strong", text: "更信任平时表现好的推荐器" },
  { kind: "text", text: "。" },
];
const wrapped = wrapInlineSpans(spans, 18);
assert.ok(wrapped.length > 1);
assert.equal(wrapped.flat().map((span) => span.text).join(""), "系统会自动更信任平时表现好的推荐器。");
assert.equal(wrapped.flat().filter((span) => span.text.includes("信任")).every((span) => span.kind === "strong"), true);
assert.ok(wrapped.every((line) => terminalWidth(line.map((span) => span.text).join("")) <= 18));

const links = wrapInlineSpans([
  { kind: "link", text: "一个很长的链接标签", href: "https://example.test" },
  { kind: "math", text: "x²", malformed: false },
  { kind: "code", text: "🚀" },
], 6, "osc8");
assert.equal(links.flat().map((span) => span.text).join(""), "一个很长的链接标签x²🚀");
assert.ok(links.flat().filter((span) => span.kind === "link").every((span) => span.href === "https://example.test"));
assert.ok(links.flat().some((span) => span.kind === "math" && span.malformed === false));

const appleTerminalLinks = wrapInlineSpans([
  { kind: "link", text: "网页", href: "https://example.com/a/very/long/path" },
], 12, "visible-target");
assert.equal(
  appleTerminalLinks.flat().map((span) => span.text).join(""),
  "网页 <https://example.com/a/very/long/path>",
);
assert.equal(appleTerminalLinks.every((line) => terminalWidth(line.map((span) => span.text).join("")) <= 12), true);
assert.equal(
  appleTerminalLinks.flat().every((span) => span.href === "https://example.com/a/very/long/path"),
  true,
);

const rejectedAppleLink = wrapInlineSpans([
  { kind: "link", text: "危险", href: "javascript:alert" },
], 12, "visible-target");
assert.equal(rejectedAppleLink.flat().map((span) => span.text).join(""), "危险");

const heading = markdownBlocksToLines(
  [{ kind: "heading", spans: [{ kind: "text", text: "P21: Cascade" }] }],
  {
    role: "assistant",
    baseKey: "heading",
    bodyWidth: 40,
    firstPrefix: "lyra ",
    continuationPrefix: "    ",
  },
);
assert.equal(heading.lines[0].kind, "header");
assert.equal(heading.lines[0].text, "P21: Cascade");
assert.equal(heading.lines[0].text.startsWith("#"), false);
assert.deepEqual(heading.lines[0].spans, [{ kind: "text", text: "P21: Cascade" }]);

const hardBreak = markdownBlocksToLines(
  parseCompleteMarkdown("alpha<br>beta<br><br>gamma"),
  {
    role: "assistant",
    baseKey: "hard-break",
    bodyWidth: 40,
    firstPrefix: "lyra ",
    continuationPrefix: "    ",
    timeRail: { label: "09:44", kind: "absolute", width: 7, prefixWidth: 7 },
  },
);
assert.deepEqual(hardBreak.lines.map((line) => line.text), ["alpha", "beta", "", "gamma"]);
assert.equal(hardBreak.lines[0].timeRail?.label, "09:44");
assert.equal(hardBreak.lines.slice(1).every((line) => line.timeRail?.label === ""), true);

const blocks: MarkdownBlock[] = [
  { kind: "paragraph", spans: [{ kind: "text", text: "alpha" }] },
  { kind: "quote", spans: [{ kind: "emphasis", text: "quote" }] },
  { kind: "list", ordered: false, spans: [{ kind: "text", text: "item" }] },
  { kind: "list", ordered: true, ordinal: 12, spans: [{ kind: "text", text: "ordered" }] },
  { kind: "rule" },
  { kind: "code", text: "const 值 = 1;" },
  {
    kind: "table",
    source: [
      "| Name | Value |",
      "| --- | --- |",
      "| 中文 | 🚀 |",
    ],
  },
  { kind: "math", source: "\\frac{a}{b}", malformed: false },
  { kind: "blank" },
];
const converted = markdownBlocksToLines(blocks, {
  role: "assistant",
  baseKey: "kinds",
  bodyWidth: 36,
  firstPrefix: "lyra ",
  continuationPrefix: "    ",
});
assert.deepEqual(converted.lines.map((line) => line.kind), ["text", "quote", "list", "list", "rule", "code", "table", "table", "table", "table", "math", "text"]);
assert.equal(converted.lines[1].prefix, "  | ");
assert.equal(converted.lines[2].prefix, "  - ");
assert.equal(converted.lines[3].prefix, "12. ");
assert.equal(converted.lines.find((line) => line.kind === "rule")?.text, "-".repeat(32));
assert.equal(converted.lines.find((line) => line.kind === "code")?.text.includes("```"), false);
assert.equal(converted.lines.filter((line) => line.kind === "table").every((line) => !line.spans), true);
assert.equal(converted.lines.find((line) => line.kind === "math")?.text, "(a)/(b)");
assert.equal(converted.lines.at(-1)?.text, " ");
assert.equal(converted.consumedLines, converted.lines.length);

const malformedMath = markdownBlocksToLines(
  [{ kind: "math", source: "\\boxed{失眠", malformed: true }],
  { role: "assistant", baseKey: "math", bodyWidth: 40, firstPrefix: "lyra ", continuationPrefix: "    " },
);
assert.equal(malformedMath.lines[0].malformedMath, true);

const railed = markdownBlocksToLines(
  [{ kind: "paragraph", spans }],
  {
    role: "reasoning",
    baseKey: "rail",
    bodyWidth: 28,
    firstPrefix: "think",
    continuationPrefix: "    ",
    timeRail: { label: "+11s", kind: "relative", width: 7, prefixWidth: 7 },
  },
);
assert.equal(railed.lines[0].timeRail?.label, "+11s");
assert.equal(railed.lines.slice(1).every((line) => line.timeRail?.label === ""), true);
assert.ok(railed.lines.every((line) => terminalWidth(line.prefix) + terminalWidth(line.text) + (line.timeRail?.width ?? 0) <= 28));
assert.ok(railed.lines.every((line) =>
  (line.timeRail?.prefixWidth ?? terminalWidth(line.prefix))
    + terminalWidth(line.text)
    + (line.timeRail?.width ?? 0) <= 28,
));

const continued = markdownBlocksToLines(
  [{ kind: "paragraph", spans: [{ kind: "text", text: "later activity" }] }],
  {
    role: "assistant",
    baseKey: "continued",
    bodyWidth: 40,
    firstPrefix: "lyra ",
    continuationPrefix: "    ",
    startingLineIndex: 3,
    timeRail: { label: "12:34", kind: "absolute", width: 7, prefixWidth: 7 },
  },
);
assert.equal(continued.lines.every((line) => line.timeRail?.label === ""), true);

const narrowTableOptions = {
  role: "assistant" as const,
  baseKey: "narrow-table",
  bodyWidth: 18,
  firstPrefix: "LYRA› ",
  continuationPrefix: "    ",
  timeRail: { label: "09:44", kind: "absolute" as const, width: 7, prefixWidth: 7 },
};
const narrowTable = markdownBlocksToLines(
  [{
    kind: "table",
    source: [
      "| 描述 | 状态 |",
      "| --- | --- |",
      "| 很长的中文内容🚀继续扩展 | 🧪🚀 |",
    ],
  }],
  narrowTableOptions,
);
assert.equal(narrowTable.lines[0].timeRail?.label, "09:44");
assert.equal(narrowTable.lines.slice(1).every((line) => line.timeRail?.label === ""), true);
assert.ok(narrowTable.lines.every((line) =>
  (line.timeRail?.prefixWidth ?? terminalWidth(line.prefix))
    + terminalWidth(line.text)
    + (line.timeRail?.width ?? 0) <= narrowTableOptions.bodyWidth,
));

const incompleteTableSource = [
  "| 这是一个不完整的表格🚀🚀并且很长",
  "仍然继续有中文和emoji🧪",
];
const incompleteTable = markdownBlocksToLines(
  [{ kind: "table", source: incompleteTableSource }],
  { ...narrowTableOptions, baseKey: "incomplete-table" },
);
assert.equal(incompleteTable.lines.map((line) => line.text).join(""), incompleteTableSource.join(""));
assert.equal(incompleteTable.lines[0].timeRail?.label, "09:44");
assert.equal(incompleteTable.lines.slice(1).every((line) => line.timeRail?.label === ""), true);
assert.ok(incompleteTable.lines.every((line) =>
  (line.timeRail?.prefixWidth ?? terminalWidth(line.prefix))
    + terminalWidth(line.text)
    + (line.timeRail?.width ?? 0) <= narrowTableOptions.bodyWidth,
));

const ansiStyledToolOutput = "\x1b[31mA👩‍👩‍👧‍👦B\x1b[0mC";
const ansiSequence = /\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[ -/]*[@-~])/gu;
for (let width = 1; width <= 5; width += 1) {
  const ansiWrapped = wrapInlineSpans([{ kind: "text", text: ansiStyledToolOutput }], width);
  const rawLines = ansiWrapped.map((line) => line.map((span) => span.text).join(""));
  assert.equal(rawLines.join(""), ansiStyledToolOutput, `ANSI tool raw order at width ${width}`);
  assert.equal(stripAnsi(rawLines.join("")), "A👩‍👩‍👧‍👦BC", `ANSI visible order at width ${width}`);
  assert.equal(rawLines.every((line) => !line.replace(ansiSequence, "").includes("\x1b")), true);
  assert.equal(rawLines.filter((line) => line.includes("👩‍👩‍👧‍👦")).length, 1);
  assert.equal(rawLines.some((line) => /👩(?:$|[^‍])/u.test(line)), false, "family grapheme split");
  const resetLine = rawLines.find((line) => line.includes("\x1b[0m"));
  assert.match(resetLine ?? "", /\x1b\[0mC/u, "reset stays with following grapheme");
}

const oscBel = "\x1b]0;bel\x07";
const oscSt = "\x1b]0;st\x1b\\";
for (const adjacentOsc of [
  `${oscSt}A${oscBel}B`,
  `${oscBel}A${oscSt}B`,
]) {
  const oscWrapped = wrapInlineSpans([{ kind: "text", text: adjacentOsc }], 1);
  const rawLines = oscWrapped.map((line) => line.map((span) => span.text).join(""));
  assert.equal(rawLines.join(""), adjacentOsc, "adjacent OSC sequences stay byte-exact");
  assert.deepEqual(
    rawLines.map((line) => stripAnsi(line)),
    ["A", "B"],
    "each OSC ends at its first BEL or ST before visible width is counted",
  );
}

const combiningAndFlag = "e\u0301🇸🇬क्‍ष";
for (let width = 1; width <= 4; width += 1) {
  const graphemeWrapped = wrapInlineSpans([{ kind: "text", text: combiningAndFlag }], width);
  const rawLines = graphemeWrapped.map((line) => line.map((span) => span.text).join(""));
  assert.equal(rawLines.join(""), combiningAndFlag);
  for (const grapheme of ["e\u0301", "🇸🇬", "क्‍ष"]) {
    assert.equal(rawLines.filter((line) => line.includes(grapheme)).length, 1, `${grapheme} at width ${width}`);
  }
}

function decoratedSpanText(span: InlineSpan): string {
  if (span.kind === "code") return `\`${span.text}\``;
  if (span.kind === "math" && span.malformed) return `⚠ ${span.text}`;
  return span.text;
}

const decorated = wrapInlineSpans([
  { kind: "code", text: "aa" },
  { kind: "text", text: "+" },
  { kind: "code", text: "bb" },
  { kind: "math", text: "cc", malformed: true },
], 8);
assert.equal(decorated.flat().map((span) => span.text).join(""), "aa+bbcc");
assert.ok(decorated.length > 1);
assert.equal(
  decorated.every((line) => terminalWidth(line.map(decoratedSpanText).join("")) <= 8),
  true,
);

const decoratedBlockMath = markdownBlocksToLines(
  [{ kind: "math", source: "x".repeat(15), malformed: true }],
  {
    role: "assistant",
    baseKey: "decorated-block-math",
    bodyWidth: 20,
    firstPrefix: "lyra ",
    continuationPrefix: "    ",
  },
);
assert.ok(decoratedBlockMath.lines.length > 1);
assert.equal(decoratedBlockMath.lines.every((line) =>
  terminalWidth(line.prefix) + terminalWidth(line.text) + terminalWidth(line.malformedMath ? "⚠ " : "") <= 20
), true);

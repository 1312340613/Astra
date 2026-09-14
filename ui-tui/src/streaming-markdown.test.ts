import assert from "node:assert/strict";
import { markdownBlocksToLines } from "./markdown-render-lines.js";
import { StreamingMarkdownCoordinator, type StreamingUpdate } from "./streaming-markdown.js";
import { parseCompleteMarkdown, type MarkdownBlock } from "./terminal-markdown.js";
import type { TimeRail } from "./message-time-rail.js";

const rail: TimeRail = {
  label: "09:44",
  kind: "absolute",
  width: 7,
  prefixWidth: 6,
};

const coordinator = new StreamingMarkdownCoordinator();
coordinator.start("assistant", rail);

let update = coordinator.push("assistant", "系统会自动**更信");
assert.equal(update.committed.length, 0);
assert.equal(update.preview.length, 1);
const previewBlock = update.preview[0];
assert.equal(previewBlock.kind, "paragraph");
if (previewBlock.kind !== "paragraph") throw new Error("expected paragraph preview");
assert.equal(previewBlock.spans.some((span) => span.kind === "strong"), true);

update = coordinator.push("assistant", "任**。\n");
assert.equal(update.committed.length, 1);
assert.equal(update.preview.length, 0);
assert.equal(update.startingLineIndex, 0);

const finished = coordinator.finish("assistant");
assert.equal(finished.committed.length, 0);
assert.equal(coordinator.snapshot("assistant"), null);
assert.deepEqual(coordinator.finish("assistant"), {
  committed: [],
  preview: [],
  startingLineIndex: 0,
});
assert.throws(() => coordinator.advance("assistant", 1), /not started/u);

const committedAndPreview = new StreamingMarkdownCoordinator();
committedAndPreview.start("assistant", rail);
const mixedUpdate = committedAndPreview.push("assistant", "safe line\nstill **open");
assert.equal(mixedUpdate.committed.length, 1);
assert.equal(mixedUpdate.preview.length, 1);
assert.equal(mixedUpdate.startingLineIndex, 0);

const committedLines = markdownBlocksToLines(mixedUpdate.committed, {
  role: "assistant",
  baseKey: "committed",
  bodyWidth: 32,
  firstPrefix: "lyra ",
  continuationPrefix: "    ",
  timeRail: mixedUpdate.timeRail,
  startingLineIndex: mixedUpdate.startingLineIndex,
});
const previewStartingLineIndex = mixedUpdate.startingLineIndex + committedLines.consumedLines;
const previewLines = markdownBlocksToLines(mixedUpdate.preview, {
  role: "assistant",
  baseKey: "preview",
  bodyWidth: 32,
  firstPrefix: previewStartingLineIndex === 0 ? "lyra " : "    ",
  continuationPrefix: "    ",
  timeRail: mixedUpdate.timeRail,
  startingLineIndex: previewStartingLineIndex,
});
assert.equal(committedLines.lines[0]?.timeRail?.label, "09:44");
assert.equal(previewLines.lines.every((line) => line.timeRail?.label === ""), true);
committedAndPreview.advance("assistant", committedLines.consumedLines);
assert.equal(committedAndPreview.snapshot("assistant")?.lineIndex, committedLines.consumedLines);

const secondRail: TimeRail = { ...rail, label: "+03s", kind: "relative" };
committedAndPreview.start("assistant", secondRail);
assert.strictEqual(committedAndPreview.snapshot("assistant")?.timeRail, rail);
assert.equal(committedAndPreview.push("assistant", " end\n").timeRail?.label, "09:44");

const noRail = new StreamingMarkdownCoordinator();
noRail.start("reasoning");
assert.equal(noRail.snapshot("reasoning")?.timeRail, undefined);
assert.equal(noRail.push("reasoning", "thought").timeRail, undefined);

function streamBlocks(chunks: string[]): MarkdownBlock[] {
  const stream = new StreamingMarkdownCoordinator();
  stream.start("assistant", rail);
  const blocks: MarkdownBlock[] = [];
  for (const chunk of chunks) {
    const next = stream.push("assistant", chunk);
    blocks.push(...next.committed);
  }
  blocks.push(...stream.finish("assistant").committed);
  return blocks;
}

const splitCases = [
  {
    chunks: ["before **str", "ong** after\n"],
    source: "before **strong** after\n",
  },
  {
    chunks: ["``", "`ts\nconst x", " = 1;\n`", "``\n"],
    source: "```ts\nconst x = 1;\n```\n",
  },
  {
    chunks: ["| A | B |\n|--", "-|---|\n| x ", "| y |\n\n"],
    source: "| A | B |\n|---|---|\n| x | y |\n\n",
  },
  {
    chunks: ["$", "$\nx +", " y\n$", "$\n"],
    source: "$$\nx + y\n$$\n",
  },
];
for (const { chunks, source } of splitCases) {
  assert.deepEqual(streamBlocks(chunks), parseCompleteMarkdown(source), source);
}

const fence = new StreamingMarkdownCoordinator();
fence.start("assistant", rail);
let fenceUpdate = fence.push("assistant", "```ts\nconst x = 1;");
assert.equal(fenceUpdate.committed.length, 0);
assert.deepEqual(fenceUpdate.preview, [{ kind: "code", text: "const x = 1;" }]);
fenceUpdate = fence.push("assistant", "\n```\n");
assert.deepEqual(fenceUpdate.committed, [{ kind: "code", text: "const x = 1;" }]);
assert.equal(fenceUpdate.preview.length, 0);

const timelineOff = new StreamingMarkdownCoordinator();
timelineOff.start("assistant", rail);
assert.equal(timelineOff.push("assistant", "before **pend").preview.length, 1);
timelineOff.clearTimeRails();
assert.equal(timelineOff.snapshot("assistant")?.timeRail, undefined);
const afterTimelineOff = timelineOff.push("assistant", "ing**\n");
assert.equal(afterTimelineOff.timeRail, undefined);
assert.deepEqual(
  afterTimelineOff.committed,
  parseCompleteMarkdown("before **pending**\n"),
  "turning the timeline off must not discard parser state",
);

const cleared = new StreamingMarkdownCoordinator();
cleared.start("reasoning", rail);
cleared.start("assistant", secondRail);
cleared.clear();
assert.equal(cleared.snapshot("reasoning"), null);
assert.equal(cleared.snapshot("assistant"), null);

function renderUpdate(next: StreamingUpdate) {
  const committed = markdownBlocksToLines(next.committed, {
    role: "assistant",
    baseKey: "finish-committed",
    bodyWidth: 32,
    firstPrefix: next.startingLineIndex === 0 ? "lyra " : "    ",
    continuationPrefix: "    ",
    timeRail: next.timeRail,
    startingLineIndex: next.startingLineIndex,
  });
  const previewStart = next.startingLineIndex + committed.consumedLines;
  const preview = markdownBlocksToLines(next.preview, {
    role: "assistant",
    baseKey: "finish-preview",
    bodyWidth: 32,
    firstPrefix: previewStart === 0 ? "lyra " : "    ",
    continuationPrefix: "    ",
    timeRail: next.timeRail,
    startingLineIndex: previewStart,
  });
  return { committed, preview };
}

const finishWithoutAdvance = new StreamingMarkdownCoordinator();
finishWithoutAdvance.start("assistant", rail);
finishWithoutAdvance.push("assistant", "final preview");
const finishUpdate = finishWithoutAdvance.finish("assistant");
const renderedFinish = renderUpdate(finishUpdate);
assert.equal(renderedFinish.committed.lines.map((line) => line.text).join(""), "final preview");
assert.equal(renderedFinish.preview.lines.length, 0);
assert.equal(finishWithoutAdvance.snapshot("assistant"), null);

const incomplete = new StreamingMarkdownCoordinator();
incomplete.start("assistant");
incomplete.push("assistant", "unfinished **strong");
const firstFinish = incomplete.finish("assistant");
const secondFinish = incomplete.finish("assistant");
assert.equal(
  firstFinish.committed
    .flatMap((block) => block.kind === "paragraph" ? block.spans : [])
    .map((span) => span.text)
    .join(""),
  "unfinished **strong",
);
assert.deepEqual(secondFinish.committed, []);

console.log("streaming markdown coordinator tests passed");

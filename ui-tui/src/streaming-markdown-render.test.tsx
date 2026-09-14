import assert from "node:assert/strict";
import { Writable } from "node:stream";
import React from "react";
import stringWidth from "string-width";
import stripAnsi from "strip-ansi";
import type { MarkdownLineResult } from "./markdown-render-lines.js";
import { StreamingMarkdownCoordinator, type StreamingRole, type StreamingUpdate } from "./streaming-markdown.js";
import {
  createStreamingDisplayState,
  orderedDynamicDisplayLines,
  streamingDisplayReducer,
} from "./streaming-display-state.js";
import type { RuntimeMode } from "./types.js";
import { THEMES } from "./theme.js";
import { ThemeProvider } from "./theme-context.js";

process.env.FORCE_COLOR = "3";
const { Box, Static, render } = await import("ink");
const appModule = await import("./app.js");
const appExports = appModule as unknown as Record<string, unknown>;
assert.equal(typeof appExports.streamingUpdateToRenderLines, "function");
const { MessageLine } = appModule;
const streamingUpdateToRenderLines = appExports.streamingUpdateToRenderLines as (
  role: StreamingRole,
  update: StreamingUpdate,
  columns: number,
  runtimeMode: RuntimeMode,
  committedBaseKey: string,
) => { committed: MarkdownLineResult; preview: MarkdownLineResult };

class CaptureStream extends Writable {
  rows = 12;
  isTTY = true;
  chunks: string[] = [];

  constructor(public columns: number) {
    super();
  }

  getColorDepth(): number {
    return 24;
  }

  _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void) {
    this.chunks.push(String(chunk));
    callback();
  }
}

const columns = 40;
const role = "assistant" as const;
const coordinator = new StreamingMarkdownCoordinator();
coordinator.start(role);

let displayState = createStreamingDisplayState();
let updateId = 0;

function applyUpdate(update: StreamingUpdate): void {
  updateId += 1;
  const { committed, preview } = streamingUpdateToRenderLines(
    role,
    update,
    columns,
    "work",
    `committed-${updateId}`,
  );
  if (committed.consumedLines > 0) coordinator.advance(role, committed.consumedLines);
  displayState = streamingDisplayReducer(displayState, {
    type: "applyStreamUpdate",
    role,
    committed: committed.lines,
    preview: preview.lines,
  });
}

const output = new CaptureStream(columns);
const view = () => (
  <ThemeProvider theme={THEMES.glitchcity}>
    <Box flexDirection="column" width={columns}>
      <Static items={displayState.historyLines}>
        {(line) => <MessageLine key={line.key} line={line} runtimeMode="work" />}
      </Static>
      {orderedDynamicDisplayLines(displayState).map((line) => (
        <MessageLine key={line.key} line={line} runtimeMode="work" />
      ))}
    </Box>
  </ThemeProvider>
);
const instance = render(view(), {
  stdout: output as unknown as NodeJS.WriteStream,
  debug: true,
  patchConsole: false,
  exitOnCtrlC: false,
});

let previewPlain = "";
for (const [index, chunk] of [
  "# P21: Cascade 级联\n\n系统会自动**更信",
  "任平时表现好的推荐器。<b",
  "r>换行以后继续。**\n```\nCF coarse → ",
  "DNN rerank → Top N\n```\n",
].entries()) {
  applyUpdate(coordinator.push(role, chunk));
  instance.rerender(view());
  await new Promise((resolve) => setTimeout(resolve, 10));
  if (index === 2) {
    previewPlain = (displayState.previewLines[role] ?? []).map((line) => line.text).join("");
  }
}
applyUpdate(coordinator.finish(role));
instance.rerender(view());
await new Promise((resolve) => setTimeout(resolve, 10));

const ansi = output.chunks.at(-1) ?? "";
const physicalPlain = stripAnsi(ansi).trimEnd();
const normalizedPhysicalPlain = physicalPlain
  .split(/\r?\n/)
  .map((line) => line.replace(/^(?:lyra | {4})/u, ""))
  .join("");
const visibleCount = (text: string) => normalizedPhysicalPlain.split(text).length - 1;
instance.unmount();

assert.doesNotMatch(physicalPlain, /\*\*|```/u);
assert.doesNotMatch(physicalPlain, /<br\s*\/?>/iu);
assert.doesNotMatch(physicalPlain, /^\s*#\s/mu);
assert.match(physicalPlain, /P21: Cascade 级联/u);
assert.equal(physicalPlain.split(/\r?\n/).every((line) => stringWidth(line) <= columns), true);
assert.match(ansi, /\x1b\[38;2;255;184;77m/u);
assert.equal(previewPlain.match(/CF coarse →/gu)?.length, 1);
assert.doesNotMatch(previewPlain, /DNN rerank → Top N/u);
assert.equal(displayState.previewLines[role], undefined);
assert.equal(visibleCount("P21: Cascade 级联"), 1);
assert.equal(visibleCount("系统会自动更信任平时表现好的推荐器。"), 1);
assert.equal(visibleCount("换行以后继续。"), 1);
assert.equal(visibleCount("CF coarse → DNN rerank → Top N"), 1);

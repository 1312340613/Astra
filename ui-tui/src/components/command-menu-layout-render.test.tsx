import assert from "node:assert/strict";
import { PassThrough, Writable } from "node:stream";
import React, { useState } from "react";
import { Box, render } from "ink";
import stringWidth from "string-width";
import stripAnsi from "strip-ansi";
import { THEMES } from "../theme.js";
import { ThemeProvider } from "../theme-context.js";
import type { ModelMenuItem } from "../command-menu.js";
import { ActivityDock } from "./activity-dock.js";
import { AppHeader } from "./app-header.js";
import { InputBar } from "./input-bar.js";
import { WelcomeScreen } from "./welcome-screen.js";

class TestStdin extends PassThrough {
  isTTY = true;
  setRawMode() {}
  ref() { return this; }
  unref() { return this; }
}

class CaptureStream extends Writable {
  columns = 130;
  rows = 30;
  isTTY = true;
  chunks: string[] = [];

  _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void) {
    this.chunks.push(String(chunk));
    callback();
  }
}

const info = {
  skills: 23,
  tools: 97,
  model: "deepseek-v4-flash",
  learning: { mode: "review" as const, auto: true, pending: 0 },
  mcp: [],
};
const visibilityEvents: boolean[] = [];

function CommandMenuShell({ modelList = [] }: { modelList?: ModelMenuItem[] }): React.ReactElement {
  const [menuVisible, setMenuVisible] = useState(false);
  return (
    <ThemeProvider theme={THEMES.glitchcity}>
      <Box flexDirection="column" width={130} height={29}>
        {!menuVisible && (
          <WelcomeScreen
            columns={130}
            rows={30}
            info={info}
            model={info.model}
            sessionName="session_layout"
          />
        )}
        <AppHeader columns={130} busy={false} />
        <ActivityDock
          expanded={false}
          tools={[]}
          now={Date.now()}
          memory={{}}
          model={info.model}
          totalTokens={0}
          promptTokens={0}
          contextPct={1}
          contextLimit={1_000_000}
          status="ready"
          showReasoning
          sessionName="session_layout"
          columns={130}
        />
        <InputBar
          onSubmit={() => {}}
          disabled={false}
          sessionList={[]}
          modelList={modelList}
          menuRows={8}
          columns={130}
          menuFocusActive={menuVisible}
          onMenuVisibilityChange={(visible) => {
            visibilityEvents.push(visible);
            setMenuVisible(visible);
          }}
        />
      </Box>
    </ThemeProvider>
  );
}

function latestFrameContaining(output: CaptureStream, marker: string): string {
  const chunk = [...output.chunks].reverse().find((candidate) => stripAnsi(candidate).includes(marker));
  return stripAnsi(chunk ?? "").trimEnd();
}

const stdin = new TestStdin();
const stdout = new CaptureStream();
const instance = render(<CommandMenuShell />, {
  stdin: stdin as unknown as NodeJS.ReadStream,
  stdout: stdout as unknown as NodeJS.WriteStream,
  stderr: stdout as unknown as NodeJS.WriteStream,
  debug: true,
  patchConsole: false,
  exitOnCtrlC: false,
});

await new Promise((resolve) => setTimeout(resolve, 20));
const openChunkStart = stdout.chunks.length;
stdin.write("/m");
await new Promise((resolve) => setTimeout(resolve, 50));

const openingFrames = stdout.chunks
  .slice(openChunkStart)
  .map((chunk) => stripAnsi(chunk).trimEnd())
  .filter((frame) => frame.includes("/minimal"));
assert.ok(openingFrames.length > 0);
for (const frame of openingFrames) {
  assert.doesNotMatch(frame, /ASTRA \/\/ OPERATOR CONSOLE/, frame);
}
assert.deepEqual(visibilityEvents, [true]);

const openFrame = latestFrameContaining(stdout, "/minimal");
assert.doesNotMatch(openFrame, /ASTRA \/\/ OPERATOR CONSOLE/);
assert.match(openFrame, /\/minimal/);
assert.match(openFrame, /\/model/);
assert.doesNotMatch(openFrame, /\/mcp(?:\s|$)/);
assert.match(openFrame, /· 1\/6/);
assert.match(openFrame, /order \/m/);
assert.equal(openFrame.split(/\r?\n/).every((line) => stringWidth(line) <= 130), true);

const openLines = openFrame.split(/\r?\n/);
const menuRows = [
  /^\s+CHAT\s*$/u,
  /^\s*(?:◆\s+)?\/minimal(?:\s|$)/u,
  /^\s+MODEL\s*$/u,
  /^\s+\/model(?:\s|$)/u,
  /^\s+\/mode(?:\s|$)/u,
  /^\s+TOOLS\s*$/u,
  /^\s+\/memory(?:\s|$)/u,
];
const matchedRowIndexes = menuRows.map((pattern) => openLines.findIndex((line) => pattern.test(line)));
assert.equal(matchedRowIndexes.every((index) => index >= 0), true, openFrame);
assert.equal(new Set(matchedRowIndexes).size, menuRows.length, openFrame);
const footerIndex = openLines.findIndex((line) => line.includes("↑↓ select"));
const inputLabelIndex = openLines.findIndex((line) => line.includes("order /m"));
const inputTopBorderIndex = openLines.findIndex((line, index) => index > footerIndex && /^[╔┌]/u.test(line));
assert.ok(footerIndex > Math.max(...matchedRowIndexes), openFrame);
assert.ok(inputTopBorderIndex > footerIndex, openFrame);
assert.ok(inputLabelIndex > inputTopBorderIndex, openFrame);
assert.ok(openLines.length <= stdout.rows, `${openLines.length} physical rows\n${openFrame}`);

stdin.write("\u001b");
await new Promise((resolve) => setTimeout(resolve, 50));
const closedFrame = latestFrameContaining(stdout, "ASTRA // OPERATOR CONSOLE");
assert.match(closedFrame, /ASTRA \/\/ OPERATOR CONSOLE/);
assert.doesNotMatch(closedFrame, /\/minimal/);
assert.deepEqual(visibilityEvents, [true, false]);

instance.unmount();

visibilityEvents.length = 0;
const asyncStdin = new TestStdin();
const asyncStdout = new CaptureStream();
const asyncInstance = render(<CommandMenuShell />, {
  stdin: asyncStdin as unknown as NodeJS.ReadStream,
  stdout: asyncStdout as unknown as NodeJS.WriteStream,
  stderr: asyncStdout as unknown as NodeJS.WriteStream,
  debug: true,
  patchConsole: false,
  exitOnCtrlC: false,
});

await new Promise((resolve) => setTimeout(resolve, 20));
asyncStdin.write("/model ");
await new Promise((resolve) => setTimeout(resolve, 20));
const asyncOpenChunkStart = asyncStdout.chunks.length;
asyncInstance.rerender(<CommandMenuShell modelList={[
  { name: "deepseek-v4-flash" },
  { name: "Qwen3.6-35B-A3B" },
]} />);
await new Promise((resolve) => setTimeout(resolve, 50));

const asyncOpeningFrames = asyncStdout.chunks
  .slice(asyncOpenChunkStart)
  .map((chunk) => stripAnsi(chunk).trimEnd())
  .filter((frame) => /deepseek-v4-flash\s+available/u.test(frame));
assert.ok(asyncOpeningFrames.length > 0);
for (const frame of asyncOpeningFrames) {
  assert.doesNotMatch(frame, /ASTRA \/\/ OPERATOR CONSOLE/, frame);
}
assert.deepEqual(visibilityEvents, [true]);
asyncInstance.unmount();

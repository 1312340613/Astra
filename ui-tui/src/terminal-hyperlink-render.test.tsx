import assert from "node:assert/strict";
import { Writable } from "node:stream";
import React from "react";
import stringWidth from "string-width";
import stripAnsi from "strip-ansi";
import { THEMES } from "./theme.js";
import { ThemeProvider } from "./theme-context.js";

const inheritedTerminalProgram = process.env.TERM_PROGRAM;
process.env.FORCE_COLOR = "3";
process.env.TERM_PROGRAM = "iTerm.app";
const { Box, render } = await import("ink");
const { MessageLine, messageToLines } = await import("./app.js");

class CaptureStream extends Writable {
  rows = 20;
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

const columns = 34;
const lines = messageToLines({
  role: "assistant",
  content: [
    "[网页](https://example.com/docs)",
    "[源码](/Users/name/项目/app.ts:42:7)",
    "[这是一个需要换行的很长链接标签](https://example.com/long)",
    "[危险](javascript:alert)",
  ].join("\n"),
}, columns, true, "work");
const wrappedLinkSpanCount = lines
  .flatMap((line) => line.spans ?? [])
  .filter((span) => span.href === "https://example.com/long").length;
assert.ok(wrappedLinkSpanCount > 1);

const stdout = new CaptureStream(columns);
const instance = render(
  <ThemeProvider theme={THEMES.classic}>
    <Box flexDirection="column" width={columns}>
      {lines.map((line) => <MessageLine key={line.key} line={line} runtimeMode="work" />)}
    </Box>
  </ThemeProvider>,
  {
    stdout: stdout as unknown as NodeJS.WriteStream,
    debug: true,
    patchConsole: false,
    exitOnCtrlC: false,
  },
);
await new Promise((resolve) => setTimeout(resolve, 20));
const frame = stdout.chunks
  .map((raw) => ({ raw, plain: stripAnsi(raw) }))
  .filter(({ plain }) => plain.includes("网页"))
  .at(-1);
const raw = frame?.raw ?? "";
const plain = frame?.plain ?? "";
const compactPlain = plain.replace(/\s+/gu, "");
instance.unmount();

assert.match(raw, /\x1b\]8;;https:\/\/example\.com\/docs\x07网页\x1b\]8;;\x07/u);
assert.match(raw, /\x1b\]8;;vscode:\/\/file\/Users\/name\/%E9%A1%B9%E7%9B%AE\/app\.ts:42:7\x07源码\x1b\]8;;\x07/u);
assert.equal((raw.match(/\x1b\]8;;https:\/\/example\.com\/long\x07/gu) ?? []).length, wrappedLinkSpanCount);
assert.equal(raw.includes("javascript:alert"), false);
assert.match(plain, /网页/u);
assert.match(plain, /源码/u);
assert.match(compactPlain, /这是一个需要换行的很长链接标签/u);
assert.match(plain, /危险/u);

process.env.TERM_PROGRAM = "Apple_Terminal";
const appleLines = messageToLines({
  role: "assistant",
  content: [
    "[网页](https://example.com/docs)",
    "[源码](/Users/name/项目/app.ts:42:7)",
    "[危险](javascript:alert)",
  ].join("\n"),
}, columns, true, "work");
const appleStdout = new CaptureStream(columns);
const appleInstance = render(
  <ThemeProvider theme={THEMES.classic}>
    <Box flexDirection="column" width={columns}>
      {appleLines.map((line) => <MessageLine key={line.key} line={line} runtimeMode="work" />)}
    </Box>
  </ThemeProvider>,
  {
    stdout: appleStdout as unknown as NodeJS.WriteStream,
    debug: true,
    patchConsole: false,
    exitOnCtrlC: false,
  },
);
await new Promise((resolve) => setTimeout(resolve, 20));
const appleFrame = appleStdout.chunks
  .map((appleRaw) => ({ raw: appleRaw, plain: stripAnsi(appleRaw) }))
  .filter(({ plain: applePlain }) => applePlain.includes("网页"))
  .at(-1);
const appleRaw = appleFrame?.raw ?? "";
const applePlain = appleFrame?.plain ?? "";
appleInstance.unmount();

assert.equal(appleRaw.includes("\x1b]8;;"), false);
assert.match(applePlain.replace(/\s+/gu, ""), /网页<https:\/\/example\.com\/docs>/u);
assert.match(applePlain.replace(/\s+/gu, ""), /源码<vscode:\/\/file\/Users\/name\/%E9%A1%B9%E7%9B%AE\/app\.ts:42:7>/u);
assert.equal(applePlain.includes("javascript:alert"), false);
assert.equal(applePlain.split(/\r?\n/u).every((line) => stringWidth(line) <= columns), true);

if (inheritedTerminalProgram === undefined) delete process.env.TERM_PROGRAM;
else process.env.TERM_PROGRAM = inheritedTerminalProgram;

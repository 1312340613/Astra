import assert from "node:assert/strict";
import { Writable } from "node:stream";
import React from "react";
import { render } from "ink";
import stringWidth from "string-width";
import stripAnsi from "strip-ansi";
import { ThemeProvider } from "../theme-context.js";
import { THEMES } from "../theme.js";
import { StartupScreen } from "./startup-screen.js";

const info = {
  skills: 15,
  tools: 72,
  model: "deepseek-v4-pro",
  learning: { mode: "review" as const, auto: true, pending: 0 },
  mcp: [{ name: "local", state: "ready" }],
};

class CaptureStream extends Writable {
  rows = 30;
  isTTY = true;
  chunks: string[] = [];

  constructor(public columns: number) {
    super();
  }

  _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void) {
    this.chunks.push(String(chunk));
    callback();
  }
}

for (const columns of [60, 90, 130]) {
  const output = new CaptureStream(columns);
  const instance = render(
    <ThemeProvider theme={THEMES.glitchcity}>
      <StartupScreen columns={columns} info={info} animate={false} />
    </ThemeProvider>,
    { stdout: output as unknown as NodeJS.WriteStream, debug: true, patchConsole: false, exitOnCtrlC: false },
  );
  await new Promise((resolve) => setTimeout(resolve, 20));
  instance.unmount();
  const frame = stripAnsi(output.chunks[0] ?? "").trimEnd();

  assert.match(frame, /ASTRA/);
  assert.match(frame, /BOOT CONSOLE/);
  assert.match(frame, /RUNTIME BUS ONLINE/);
  assert.match(frame, /LEARN\s+MANUAL/);
  assert.equal(
    frame.split(/\r?\n/).every((line) => stringWidth(line) <= columns),
    true,
  );
  if (process.env.SHOW_STARTUP_PREVIEW === "1") {
    process.stdout.write(`\n=== startup ready · ${columns} columns ===\n${frame}\n`);
  }
}

const loadingOutput = new CaptureStream(130);
const loading = render(
  <ThemeProvider theme={THEMES.glitchcity}>
    <StartupScreen columns={130} rows={30} info={null} animate={false} />
  </ThemeProvider>,
  {
    stdout: loadingOutput as unknown as NodeJS.WriteStream,
    debug: true,
    patchConsole: false,
    exitOnCtrlC: false,
  },
);
await new Promise((resolve) => setTimeout(resolve, 20));
loading.unmount();
const loadingFrame = stripAnsi(loadingOutput.chunks[0] ?? "").trimEnd();
assert.match(loadingFrame, /BOOT SEQUENCE ACTIVE/);
assert.match(loadingFrame, /MODEL BUS HANDSHAKE/);
assert.match(loadingFrame, /WAITING FOR SUBSYSTEMS/);
assert.equal(loadingFrame.split(/\r?\n/).every((line) => stringWidth(line) <= 130), true);
if (process.env.SHOW_STARTUP_PREVIEW === "1") {
  process.stdout.write(`\n=== startup loading · 130x30 ===\n${loadingFrame}\n`);
}

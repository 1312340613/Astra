import assert from "node:assert/strict";
import { PassThrough, Writable } from "node:stream";
import React from "react";
import { render } from "ink";
import { THEMES } from "../theme.js";
import { ThemeProvider } from "../theme-context.js";
import { InputBar } from "./input-bar.js";

class TestStdin extends PassThrough {
  isTTY = true;
  setRawMode() {}
  ref() { return this; }
  unref() { return this; }
}

class TestStdout extends Writable {
  columns = 130;
  rows = 32;
  isTTY = true;
  chunks: string[] = [];
  _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void) {
    this.chunks.push(String(chunk));
    callback();
  }
}

async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 100));
}

async function captureInputBar() {
  const stdin = new TestStdin();
  const stdout = new TestStdout();
  const values: string[] = [];
  const instance = render(
    React.createElement(
      ThemeProvider,
      {
        theme: THEMES.glitchcity,
        children: React.createElement(InputBar, {
          onSubmit: ({text}: {text:string}) => values.push(text),
          disabled: false,
          sessionList: [],
          modelList: [],
          columns: 130,
        }),
      },
    ),
    {
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
      stderr: stdout as unknown as NodeJS.WriteStream,
      // Capture intermediate frames even when Ink detects a CI environment.
      debug: true,
      patchConsole: false,
      exitOnCtrlC: false,
    },
  );
  await settle();
  return { stdin, stdout, instance, submitted: () => [...values] };
}

const targeted = await captureInputBar();
targeted.stdin.write("/cancel selected-id");
await settle();
targeted.stdin.write("\r");
await settle();
assert.deepEqual(targeted.submitted(), ["/cancel selected-id"]);
targeted.instance.unmount();

const required = await captureInputBar();
required.stdin.write("/resume");
await settle();
required.stdin.write("\r");
await settle();
assert.deepEqual(required.submitted(), []);
assert.match(required.stdout.chunks.join(""), /Usage: \/resume <task-id>/);
assert.match(required.stdout.chunks.join(""), /\/resume/);
required.stdin.write(" task-id");
await settle();
required.stdin.write("\r");
await settle();
assert.deepEqual(required.submitted(), ["/resume task-id"]);
required.instance.unmount();

const handoff = await captureInputBar();
handoff.stdin.write("/handoff Keep Windows behavior");
await settle();
handoff.stdin.write("\r");
await settle();
assert.deepEqual(handoff.submitted(), ["/handoff Keep Windows behavior"]);
handoff.instance.unmount();

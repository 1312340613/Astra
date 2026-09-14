import assert from "node:assert/strict";
import React from "react";
import { PassThrough, Writable } from "node:stream";
import { render } from "ink";
import { InputBar } from "./input-bar.js";
import { ThemeProvider } from "../theme-context.js";
import { THEMES } from "../theme.js";

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

const stdin = new TestStdin();
const stdout = new TestStdout();
const original = [
  "Shell 与粘贴改造说明",
  "第一段：" + "alpha ".repeat(180),
  "第二段：" + "beta ".repeat(220),
  "结尾内容必须完整保留。",
].join("\n");
let submitted = "";

const instance = render(
  React.createElement(
    ThemeProvider,
    {
      theme: THEMES.glitchcity,
      children: React.createElement(InputBar, {
        onSubmit: ({text}: {text:string}) => { submitted = text; },
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
    patchConsole: false,
    exitOnCtrlC: false,
  },
);

await new Promise((resolve) => setTimeout(resolve, 20));
stdin.write(`\u001b[200~${original.slice(0, 113)}`);
stdin.write(original.slice(113, 977));
stdin.write(original.slice(977, 1901));
stdin.write(`${original.slice(1901)}\u001b[201~`);
await new Promise((resolve) => setTimeout(resolve, 20));
stdin.write("\r");
await new Promise((resolve) => setTimeout(resolve, 20));

assert.equal(submitted, original, stdout.chunks.join(""));
instance.unmount();

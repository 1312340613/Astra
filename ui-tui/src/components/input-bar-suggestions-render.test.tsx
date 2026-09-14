import assert from "node:assert/strict";
import { PassThrough, Writable } from "node:stream";
import React from "react";
import { render } from "ink";
import stringWidth from "string-width";
import stripAnsi from "strip-ansi";
import { THEMES } from "../theme.js";
import { ThemeProvider } from "../theme-context.js";
import { InputBar } from "./input-bar.js";

class TestStdin extends PassThrough {
  isTTY = true;
  setRawMode() {}
  ref() { return this; }
  unref() { return this; }
}

class CaptureStream extends Writable {
  columns = 110;
  rows = 32;
  isTTY = true;
  chunks: string[] = [];

  _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void) {
    this.chunks.push(String(chunk));
    callback();
  }
}

const stdin = new TestStdin();
const stdout = new CaptureStream();
const instance = render(
  <ThemeProvider theme={THEMES.glitchcity}>
    <InputBar
      onSubmit={() => {}}
      disabled={false}
      sessionList={[]}
      modelList={[]}
      columns={stdout.columns}
      personas={[
        { name: "lyra", description: "默认助手：帮助用户处理工程任务、组织信息和日常交流，表达自然，结论有依据。" },
        { name: "example-research", description: "研究示例：检查来源、比较证据、标记尚未验证的假设，说明结论适用的边界。" },
        { name: "example-writing", description: "写作示例：按要求组织内容、调整表达和修改文稿。" },
        { name: "example-coding", description: "编码示例：读取项目现状，完成修改，运行相关检查并报告实际结果。" },
      ]}
    />
  </ThemeProvider>,
  {
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stdout as unknown as NodeJS.WriteStream,
    debug: true,
    patchConsole: false,
    exitOnCtrlC: false,
  },
);

await new Promise((resolve) => setTimeout(resolve, 20));
stdin.write("/persona");
await new Promise((resolve) => setTimeout(resolve, 50));
const frame = stripAnsi(stdout.chunks.at(-1) ?? "").trimEnd();
instance.unmount();

const lines = frame.split(/\r?\n/);
assert.match(frame, /example-research/);
assert.match(frame, /example-writing/);
assert.equal(lines.every((line) => stringWidth(line) <= stdout.columns), true, frame);
assert.equal(lines.filter((line) => /lyra|example-research|example-writing|example-coding/.test(line)).length, 4, frame);
assert.equal(lines.length, 9, `${lines.length} rendered lines\n${frame}`);

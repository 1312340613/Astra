import assert from "node:assert/strict";
import test from "node:test";
import { PassThrough, Writable } from "node:stream";
import React from "react";
import { render } from "ink";
import { InputBar } from "./input-bar.js";

class TestStdin extends PassThrough {
  isTTY = true;
  setRawMode() {}
  ref() { return this; }
  unref() { return this; }
}
class TestStdout extends Writable {
  columns = 80;
  rows = 24;
  isTTY = true;
  _write(_chunk: Buffer, _encoding: BufferEncoding, callback: () => void) { callback(); }
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 25));

for (const grapheme of ["😀", "👩‍💻", "e\u0301", "🇸🇬"]) {
  test(`composer deletes and moves across the whole grapheme ${grapheme}`, async () => {
    const stdin = new TestStdin();
    const stdout = new TestStdout();
    const submitted: string[] = [];
    const instance = render(<InputBar disabled={false} onSubmit={({text}) => submitted.push(text)}
      sessionList={[]} modelList={[]} columns={79} />, {
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
      stderr: stdout as unknown as NodeJS.WriteStream,
      debug: true, patchConsole: false, exitOnCtrlC: false,
    });
    try {
      await settle();
      for (const chunk of [`A${grapheme}B`, "\u001b[D", "\u007f", "\r"]) {
        stdin.write(chunk); await settle();
      }
      assert.deepEqual(submitted, ["AB"]);
      for (const chunk of [`A${grapheme}B`, "\u001b[D", "\u001b[D", "X", "\u001b[C", "Y", "\r"]) {
        stdin.write(chunk); await settle();
      }
      assert.deepEqual(submitted, ["AB", `AX${grapheme}YB`]);
    } finally { instance.unmount(); }
  });
}

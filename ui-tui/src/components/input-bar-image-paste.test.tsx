import assert from "node:assert/strict";
import test from "node:test";
import { PassThrough, Writable } from "node:stream";
import React, { createRef } from "react";
import { render } from "ink";
import { InputBar, type AppshotInputHandle } from "./input-bar.js";
import { formatImageInputDisplay } from "../image-command.js";

class TestStdin extends PassThrough {
  isTTY = true;
  setRawMode() {}
  ref() { return this; }
  unref() { return this; }
}
class TestStdout extends Writable {
  columns = 120;
  rows = 24;
  isTTY = true;
  _write(_chunk: Buffer, _encoding: BufferEncoding, callback: () => void) { callback(); }
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 25));

const cases = [
  { name: "existing Chinese text", chunks: ["哈", "/tmp/first.PNG"], prompt: "哈", paths: ["/tmp/first.PNG"] },
  { name: "fragmented bracketed paste", chunks: ["Hello", "\x1b[200~/tmp/fi", "rst.PNG\x1b[201~"], prompt: "Hello", paths: ["/tmp/first.PNG"] },
  { name: "text after the cursor", chunks: ["前面 后面", "\x1b[D", "\x1b[D", "/tmp/first.PNG"], prompt: "前面 后面", paths: ["/tmp/first.PNG"] },
  { name: "successive images", chunks: ["对照", "/tmp/first.PNG", "/tmp/second.jpg"], prompt: "对照", paths: ["/tmp/first.PNG", "/tmp/second.jpg"] },
  { name: "Windows drive path", chunks: ["哈", "C:\\cards\\first.PNG"], prompt: "哈", paths: ["C:\\cards\\first.PNG"] },
  { name: "quoted image path", chunks: ["hello", '"/tmp/first.PNG"'], prompt: "hello", paths: ["/tmp/first.PNG"] },
  { name: "emoji prefix", chunks: ["😀", "/tmp/first.PNG"], prompt: "😀", paths: ["/tmp/first.PNG"] },
];

for (const fixture of cases) {
  test(`image paste preserves ${fixture.name} in the draft and submission`, async () => {
    const stdin = new TestStdin();
    const stdout = new TestStdout();
    const ref = createRef<AppshotInputHandle>();
    const submitted: string[] = [];
    const instance = render(<InputBar ref={ref} disabled={false} onSubmit={({text}) => submitted.push(text)}
      sessionList={[]} modelList={[]} columns={119} />, {
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
      stderr: stdout as unknown as NodeJS.WriteStream,
      debug: true, patchConsole: false, exitOnCtrlC: false,
    });
    try {
      await settle();
      for (const chunk of fixture.chunks) {
        stdin.write(chunk);
        await settle();
      }
      const labels = fixture.paths.map((_path, index) => `[Image #${index + 1}]`).join(" ");
      assert.equal(ref.current!.snapshot().draft.text, `${labels} ${fixture.prompt}`);
      assert.deepEqual(submitted, [], "pasting must not submit the message");
      stdin.write("\r");
      await settle();
      assert.deepEqual(submitted, [`${fixture.paths.join(" ")} ${fixture.prompt}`]);
      assert.equal(formatImageInputDisplay(submitted[0]), `${fixture.prompt}\n${labels}`);
    } finally {
      instance.unmount();
    }
  });
}

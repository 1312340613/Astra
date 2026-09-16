import assert from "node:assert/strict";
import test from "node:test";
import { TerminalOutput, type AsyncWrite } from "./terminal-output.js";

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
test("partial writes preserve bytes and callbacks in order; idle time is not a stall", async () => {
  const chunks: Buffer[] = [];
  const calls: number[] = [];
  const write: AsyncWrite = (buffer, offset, length, callback) => {
    const count = Math.min(3, length);
    chunks.push(buffer.subarray(offset, offset + count));
    setTimeout(() => callback(null, count), 1);
  };
  const output = new TerminalOutput(write, { stallMs: 40 });
  output.write("中文👩‍💻", () => calls.push(1));
  output.write("\u001b[2Knext", () => calls.push(2));
  await delay(80);
  assert.equal(Buffer.concat(chunks).toString(), "中文👩‍💻\u001b[2Knext");
  assert.deepEqual(calls, [1, 2]);
  assert.equal(output.fault, undefined);
  assert.equal(await output.close(), true);
});

test("stalled output is bounded and fails without blocking timers or late write callbacks", async () => {
  let complete: ((error: NodeJS.ErrnoException | null, written: number) => void) | undefined;
  const output = new TerminalOutput((_buffer, _offset, _length, callback) => { complete = callback; },
    { stallMs: 30, maxPendingBytes: 128, highWaterMark: 16 });
  const errors: string[] = [];
  output.on("fault", error => errors.push(error.code));
  assert.equal(output.write(Buffer.alloc(64)), false);
  await delay(80);
  assert.deepEqual(errors, ["ETIMEDOUT"]);
  assert.ok(output.metrics.peakPendingBytes <= 128);
  assert.equal(output.write("later"), false);
  assert.equal(await output.close(), false);
  complete?.(null, 64);
  await delay(5);
  assert.deepEqual(errors, ["ETIMEDOUT"]);
});

test("overload and terminal detach each fail once; normal close drains", async () => {
  const output = new TerminalOutput(() => {}, { maxPendingBytes: 32 });
  output.write(Buffer.alloc(32));
  assert.equal(output.write("overflow"), false);
  assert.equal(output.fault?.code, "ENOBUFS");
  assert.ok(output.metrics.peakPendingBytes <= 32);
  assert.equal(await output.close(), false);
  for (const code of ["EIO", "EPIPE"]) {
    const broken = new TerminalOutput((_buffer, _offset, _length, cb) => cb(Object.assign(new Error(code), { code }), 0));
    broken.write("test");
    await delay(5);
    assert.equal(broken.fault?.code, code);
    assert.equal(await broken.close(), false);
  }
});

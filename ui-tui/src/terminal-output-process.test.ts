import assert from "node:assert/strict";
import test from "node:test";
import { openSync, closeSync } from "node:fs";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { Writable } from "node:stream";
import { installTerminalOutput } from "./terminal-output.js";

test("owned writer handles real descriptors; redirection preserves stream identity and drain", async () => {
  const directory = await mkdtemp(join(tmpdir(), "astra-output-"));
  const path = join(directory, "terminal.txt");
  const fd = openSync(path, "w");
  const stream = Object.assign(new Writable({ write(_data, _encoding, cb) { cb(); } }),
    { fd, columns: 80, rows: 24, isTTY: true });
  const original = stream.write;
  const { output, restore } = installTerminalOutput(stream as any, { highWaterMark: 16 });
  const drained = new Promise(resolve => stream.once("drain", resolve));
  const content = "中文👩‍💻\u001b[?25l".repeat(10000);
  try {
    assert.equal(stream.columns, 80);
    assert.equal(stream.write(content), false);
    assert.ok(stream.writableLength > 0);
    await drained;
    assert.equal(await output.close(), true);
    assert.equal(await readFile(path, "utf8"), content);
  } finally {
    restore();
    assert.equal(stream.write, original);
    closeSync(fd);
    await rm(directory, { recursive: true, force: true });
  }
});

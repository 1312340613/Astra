import test from "node:test";
import assert from "node:assert/strict";
import { AppshotFocusTracker, normalizeTTY, runFocusProbe } from "./appshot-focus.js";

test("selecting another tab moves recipient without typing; browser preserves it", () => {
  let recipient = "";
  const a = new AppshotFocusTracker("/dev/ttys001", () => { recipient = "A"; });
  const b = new AppshotFocusTracker("/dev/ttys002", () => { recipient = "B"; });
  const observe = (tty: string) => { a.observe(tty); b.observe(tty); };
  observe("/dev/ttys001"); assert.equal(recipient, "A");
  observe("/dev/ttys002"); assert.equal(recipient, "B");
  observe(""); assert.equal(recipient, "B");
  observe("/dev/ttys001"); assert.equal(recipient, "A");
});

test("background tabs and repeated focus probes never claim activity", () => {
  let count = 0;
  const a = new AppshotFocusTracker("/dev/ttys001", () => count++);
  a.observe("/dev/ttys002"); a.observe(""); assert.equal(count, 0);
  a.observe("/dev/ttys001"); a.observe("/dev/ttys001"); assert.equal(count, 1);
  a.observe(""); a.observe("/dev/ttys001"); assert.equal(count, 2);
});

test("TTY matching is exact and rejects absent or invalid process terminals", () => {
  assert.equal(normalizeTTY(" ttys002\n"), "/dev/ttys002");
  for (const value of ["?", "", "ttys002suffix", "/tmp/ttys002", "ttys002\nttys003"])
    assert.equal(normalizeTTY(value), undefined);
});


test("probe runs outside the terminal job group but retains bounded output", { skip: process.platform === "win32" }, async () => {
  const result = await runFocusProbe(process.execPath, ["-e",
    'const cp=require("node:child_process"); console.log(process.pid + ":" + cp.execFileSync("/bin/ps", ["-p", String(process.pid), "-o", "pgid="], {encoding:"utf8"}).trim())'
  ], new AbortController().signal);
  const [pid, group] = result.split(":");
  assert.equal(pid, group);
  await assert.rejects(runFocusProbe(process.execPath, ["-e", 'console.log("x".repeat(4096))'],
    new AbortController().signal));
});

test("disposing the TUI cancels its detached probe", async () => {
  const abort = new AbortController();
  const promise = runFocusProbe(process.execPath, ["-e", "setInterval(()=>{},1000)"], abort.signal);
  abort.abort();
  await assert.rejects(promise);
});

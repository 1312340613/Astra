import assert from "node:assert/strict";
import test from "node:test";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { stopBackend, TuiLifecycle } from "./tui-lifecycle.js";

test("repeated exit requests send one shutdown command and finish after the owned backend exits", async () => {
  const child = spawn(process.execPath, ["-e", `
    process.stdin.on('data', data => { process.stdout.write(data, () => process.exit(0)); });
    console.log('ready');
  `], { stdio: ["pipe", "pipe", "pipe"] });
  child.stderr.resume();
  try {
    await once(child.stdout, "data");
    const commands: string[] = [];
    child.stdout.on("data", data => commands.push(String(data)));
    let finishes = 0;
    const lifecycle = new TuiLifecycle(async (reason, result) => {
      finishes++;
      assert.equal(reason, "terminal_output_failure");
      assert.equal(child.exitCode, 0);
      assert.deepEqual(result, { forced: false, exited: true });
    });
    lifecycle.attachBackend(child);
    const first = lifecycle.requestExit("terminal_output_failure");
    assert.equal(lifecycle.requestExit("user_exit"), first);
    assert.equal(lifecycle.closing, true);
    await first;
    assert.equal(finishes, 1);
    assert.deepEqual(JSON.parse(commands.join("")), { type: "exit", reason: "terminal_output_failure" });
  } finally { if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL"); }
});

test("an unresponsive owned backend has a bounded shutdown deadline", async () => {
  const child = spawn(process.execPath, ["-e", `
    process.on('SIGTERM', () => {});
    process.stdin.resume(); setInterval(() => {}, 1000); console.log('ready');
  `], { stdio: ["pipe", "pipe", "pipe"] });
  child.stderr.resume();
  try {
    await once(child.stdout, "data");
    const started = performance.now();
    const result = await stopBackend(child, "terminal_output_failure", 40, 100);
    assert.deepEqual(result, { forced: true, exited: true });
    assert.ok(performance.now() - started < 2000);
  } finally { if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL"); }
});

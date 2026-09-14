import assert from "node:assert/strict";
import test from "node:test";
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { EventEmitter, once } from "node:events";
import { PassThrough, Writable } from "node:stream";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import React from "react";
import { render } from "ink";
import stripAnsi from "strip-ansi";

const root = fileURLToPath(new URL("../../", import.meta.url));
const originalSpawn = childProcess.spawn;
let activeCase = "ordered-write-read";
let outputDir = "";
const children: childProcess.ChildProcess[] = [];
const received: any[][] = [];
childProcess.spawn = ((command: string, args: string[]) => {
  assert.deepEqual(args, ["-m", "agent.cli.backend"], "Only replace the backend entry point");
  const python = process.env.AGENT_PYTHON || path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const child = originalSpawn(python, ["-m", "agent.evals.turn_replay", "--stdio", "--case", activeCase, "--output", outputDir], {
    cwd: root, env: { ...process.env, PYTHONPATH: root }, stdio: ["pipe", "pipe", "pipe"],
  });
  children.push(child);
  const events: any[] = []; received.push(events);
  createInterface({ input: child.stdout! }).on("line", line => events.push(JSON.parse(line)));
  return child;
}) as typeof childProcess.spawn;
childProcess.execFile = (() => { throw new Error("Replay must not run Appshot"); }) as unknown as typeof childProcess.execFile;
syncBuiltinESMExports();
process.env.TUI_STARTUP_ANIMATION = "0";
const { default: App } = await import("./app.js");
class Input extends PassThrough { isTTY = true; setRawMode() {} ref() { return this; } unref() { return this; } }
class Output extends Writable {
  columns = 110; rows = 35; isTTY = true; chunks: string[] = [];
  _write(data: Buffer, _: BufferEncoding, done: () => void) { this.chunks.push(String(data)); done(); }
}
const settle = () => new Promise(resolve => setTimeout(resolve, 60));
async function until(check: () => boolean) {
  const deadline = Date.now() + 15000;
  while (!check()) { assert.ok(Date.now() < deadline, "Replay subprocess did not complete"); await settle(); }
  await settle();
}
function appshot() {
  return Object.assign(new EventEmitter(), {
    state: { enabled: false, registration: "disabled", connectedTuis: 0, permission: "ready" },
    start: async () => {}, close: async () => {}, recordInput: () => {}, updateDraft: () => {}, release: () => {},
  }) as any;
}

for (const name of ["ordered-write-read", "budget-during-tool", "truncated-tool-recovery"]) {
  test(`real provider replay subprocess clears the Ink turn: ${name}`, async () => {
    activeCase = name; outputDir = await mkdtemp(path.join(tmpdir(), "astra-ink-replay-"));
    const stdin = new Input(); const stdout = new Output();
    const app = render(<App appshotClientFactory={appshot} />, { stdin: stdin as any, stdout: stdout as any, stderr: stdout as any, debug: true, patchConsole: false, exitOnCtrlC: false });
    const child = children.at(-1);
    try {
      await until(() => received.at(-1)?.some(e => e.type === "backend_hello") ?? false);
      const events = received.at(-1)!;
      const submit = async (text: string) => { stdin.write(text); await settle(); stdin.write("\r"); await settle(); };
      for (let run = 1; run <= 2; run++) {
        await submit(`Run fixture ${run}`);
        await until(() => events.filter(e => e.name === "replay_oracle").length === run);
        const report = JSON.parse(await readFile(path.join(outputDir, String(run), "report.json"), "utf8"));
        assert.equal(report.passed, true, JSON.stringify(report));
        assert.equal(events.filter(e => e.type === "done").length, run);
        const frame = stdout.chunks.map(stripAnsi).filter(c => c.trim()).at(-1) ?? "";
        assert.doesNotMatch(frame, /· RUNNING|· RUN$/m, "Completed/expired turn must release busy status");
      }
      const text = stdout.chunks.map(stripAnsi).join("\n");
      if (name !== "budget-during-tool") assert.match(text, /REPLAY VERIFIED/);
      else {
        assert.match(text, /Turn time budget exhausted/);
        const calls = events.flatMap(e => e.type === "tool_calls" ? e.calls : []);
        for (const call of calls) assert.ok(events.some(e => e.type === "tool_result" && e.call_id === call.id), "Pending tool UI must receive a terminal result");
      }
      if (name === "truncated-tool-recovery") {
        const recovery = events.find(e => e.type === "tool_progress" && e.stage === "recovered");
        assert.equal(recovery?.call_id, "bad", "Recovery progress must retain its original call identity");
      }
      assert.ok(events.some(e => e.type === "done" && e.event_id && e.generation), "Actual backend event envelope reached the TUI");
    } finally {
      app.unmount();
      const process = child ?? children.at(-1)!;
      if (process.exitCode === null && process.signalCode === null) { const exited = once(process, "exit"); process.kill(); await exited; }
      await rm(outputDir, { recursive: true, force: true });
    }
  });
}

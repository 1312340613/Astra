/** Explicit synthetic integration harness. No window capture, model, or production service. */
import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";
import { once } from "node:events";
import { mkdtempSync, readdirSync, rmdirSync, lstatSync } from "node:fs";
import { join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { AppshotClient } from "../../ui-tui/src/appshot-client.js";
import { windowsAppshotDependencies } from "../../ui-tui/src/appshot-windows.js";
import { appendAppshot, emptyDraft, type AppshotInputOffer } from "../../ui-tui/src/appshot-input.js";

const [helper, repo] = process.argv.slice(2);
assert.equal(process.platform, "win32"); assert.ok(helper && repo);
const root = mkdtempSync(join(resolve(repo), ".test-appshot-consumer-")), runtime = join(root, "private");
const scope = "consumer-" + randomUUID();
const broker = spawn(helper, ["--self-test-consumer-broker", runtime, scope], { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
let output = "", stderr = "";
broker.stdout.on("data", data => { output += data.toString(); if (output.length > 8192) broker.kill(); });
broker.stderr.on("data", data => { stderr += data.toString(); if (stderr.length > 8192) broker.kill(); });
const exited = once(broker, "exit");
const overall = setTimeout(() => { broker.kill(); }, 14000);
const staged = new Map<string, AppshotInputOffer>();
let draft = emptyDraft(), verified = false, committed = false;
const dependencies = windowsAppshotDependencies(helper, runtime, scope);
// This fixture owns a synthetic broker. Do not launch a production replacement
// while the harness is intentionally testing broker-exit media persistence.
dependencies.launch = async () => { throw Error("fixture_broker_exited"); };
const reader = dependencies.verifyOffer;
dependencies.verifyOffer = async (...args) => { const result = await reader(...args); verified = true; return result; };
const client = new AppshotClient({ platform: "win32", windowsDeps: dependencies, retryMS: 10, consumer: {
  stage: () => { throw Error("POSIX reader used"); },
  stageWindows: offer => { assert.equal(verified, true); assert.equal(draft.attachments.length, 0); staged.set(offer.requestId, offer); return true; },
  commit: event => {
    const offer = staged.get(event.request_id)!; assert.ok(offer);
    assert.equal(offer.manifestPath, event.manifest_path); draft = appendAppshot(draft, offer); committed = true;
    return { appshotCount: draft.attachments.length, canAccept: true };
  },
  revoke: event => staged.delete(event.requestID), disconnect: () => ({ appshotCount: 0, canAccept: false }),
} });
try {
  const until = Date.now() + 3000;
  while (!output.includes('"ready":true') && broker.exitCode === null && Date.now() < until) await new Promise(r => setTimeout(r, 10));
  assert.ok(output.includes('"ready":true'), `broker did not start: ${stderr} ${output}`);
  await client.start(); assert.equal(client.state.connection, "connected", "real relay hello failed");
  const descriptor = await dependencies.discover(new AbortController().signal);
  // Native stdio cannot silently become a blocking reader; stale discovery,
  // malformed direction and a forged TUI PID all fail before draft authority.
  const probe = async (stdio: "overlapped" | "pipe", nonce: string, message?: object, start = descriptor.process.process_start) => {
    const child = spawn(helper, ["--appshot-relay", runtime, scope, descriptor.instance_id, nonce,
      String(descriptor.process.pid), start, descriptor.process.user_sid],
      { windowsHide: true, stdio: [stdio, stdio, "ignore"] });
    const completed = once(child, "exit");
    child.stdin!.on("error", () => {}); child.stdout!.resume();
    const timer = setTimeout(() => child.kill(), 1700);
    try {
      if (message) child.stdin!.write(JSON.stringify(message) + "\n");
      else child.stdin!.end();
      const [code, signal] = await completed;
      assert.equal(signal, null, "probe must exit without emergency kill"); assert.equal(code, 1);
    } finally { clearTimeout(timer); if (child.exitCode === null && child.signalCode === null) { child.kill(); await completed; } }
  };
  await probe("pipe", descriptor.broker_nonce);
  await probe("overlapped", "wrong-nonce");
  await probe("overlapped", descriptor.broker_nonce, undefined, "1");
  await probe("overlapped", descriptor.broker_nonce, { type: "hello", version: 2, platform: "windows", session_id: "forged",
    pid: process.pid + 1, process_start: "1", user_sid: descriptor.process.user_sid, client_nonce: descriptor.broker_nonce });
  await probe("overlapped", descriptor.broker_nonce, { type: "hello", version: 1 });
  await probe("overlapped", descriptor.broker_nonce); // EOF must cancel pending overlapped reads.
  client.updateDraft(0n, 0, true); client.recordInput();
  assert.equal((await client.command("shortcut", "Ctrl+Alt+Shift+F23")).ok, true);
  assert.equal((await client.command("enable", "")).ok, true);
  const capturedBy = Date.now() + 3500;
  while (!committed && Date.now() < capturedBy) await new Promise(r => setTimeout(r, 10));
  assert.equal(verified, true, "native verified read failed"); assert.equal(committed, true, "draft commit missing");
  assert.equal(draft.attachments.length, 1); assert.match(draft.text, /仅截图/);
  const binding = client.recipientBinding!;
  assert.ok("recipient" in binding);
  const backend = await promisify(execFile)(join(repo, ".venv/Scripts/python.exe"),
    [join(repo, "tests/fixtures/appshot_windows_backend.py"), helper, draft.attachments[0].manifestPath,
      binding.instance_id, binding.session_id, runtime, JSON.stringify(binding.recipient)],
    { windowsHide: true, timeout: 7000, maxBuffer: 8192 });
  assert.equal(JSON.parse(backend.stdout).independent_parent, true);
  assert.equal(JSON.parse(backend.stdout).admission_accepted, true);
  assert.equal(JSON.parse(backend.stdout).media_durable, true);
  // Explicit synthetic submission only. Production admission, no model calls.
  client.release(draft.attachments[0].requestId);
  const [code] = await exited; assert.equal(code, 0, `broker failed: ${stderr} ${output}`);
  assert.ok(output.includes('"owned_cleanup":true'));
  const reloaded = await promisify(execFile)(join(repo, ".venv/Scripts/python.exe"),
    [join(repo, "tests/fixtures/appshot_windows_backend.py"), "--reload-media", helper, root],
    { windowsHide: true, timeout: 4000, maxBuffer: 8192 });
  assert.equal(JSON.parse(reloaded.stdout).reload_after_broker_exit, true);
  console.log(JSON.stringify({ ok: true, real_node_client: true, native_relay: true, verified_read: verified,
    draft_committed: committed, independent_python_read: true, rejected_native_probes: 6,
    admission_and_media: true, reload_after_broker_exit: true, session_rename_delete: true,
    auto_submitted: false, capture_is_synthetic: true, owned_cleanup: true }));
} catch (error) {
  console.error(error);
  throw error;
} finally {
  await client.close(); clearTimeout(overall);
  // Let broker disconnect handling clean its receipts before emergency kill.
  if (broker.exitCode === null && broker.signalCode === null) {
    await Promise.race([exited, new Promise(r => setTimeout(r, 3000))]);
  }
  if (broker.exitCode === null && broker.signalCode === null) { broker.kill(); await exited; }
  // Empty test-owned directories only. Preserve unexpected files for diagnosis.
  for (const folder of [runtime, root]) {
    assert.ok(resolve(folder).startsWith(resolve(repo) + "\\"));
    assert.equal(lstatSync(folder).isSymbolicLink(), false);
    assert.deepEqual(readdirSync(folder), []); rmdirSync(folder);
  }
}

/** Real production launch/idle shutdown; no capture, enabled hotkey or model. */
import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, rmdirSync, lstatSync } from "node:fs";
import { join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { AppshotClient } from "../../ui-tui/src/appshot-client.js";
import { windowsAppshotDependencies } from "../../ui-tui/src/appshot-windows.js";

const [helper, repo] = process.argv.slice(2);
const root = mkdtempSync(join(resolve(repo), ".test-appshot-daemon-")), runtime = join(root, "private");
const scope = "daemon-" + randomUUID();
// Child helpers must work with only Windows system directories on PATH.
const oldPath = process.env.PATH;
process.env.PATH = join(process.env.SystemRoot!, "System32");
let client: AppshotClient | undefined;
try {
  for (let round = 0; round < 2; round++) {
    client = new AppshotClient({ platform: "win32", windowsDeps: windowsAppshotDependencies(helper, runtime, scope),
      consumer: { stage: () => false, stageWindows: () => false, commit: () => { throw Error("unexpected capture"); },
        revoke: () => {}, disconnect: () => ({ appshotCount: 0, canAccept: false }) } });
    await client.start(); assert.equal(client.state.connection, "connected");
    const status = await client.command("status", "");
    assert.equal(status.ok, true);
    assert.equal(client.state.enabled, false);
    await client.close(); client = undefined;
    const until = Date.now() + 5000;
    while (readdirSync(runtime).length && Date.now() < until) await new Promise(r => setTimeout(r, 50));
    assert.deepEqual(readdirSync(runtime), [], "last TUI close must remove discovery and stop idle service");
  }
  console.log(JSON.stringify({ ok: true, production_daemon: true, launch_restart: true,
    app_local_runtime: true, default_disabled: true, capture_requested: false, idle_cleanup: true }));
} finally {
  await client?.close(); process.env.PATH = oldPath;
  const until = Date.now() + 5000;
  while (readdirSync(runtime).length && Date.now() < until) await new Promise(r => setTimeout(r, 50));
  for (const path of [runtime, root]) {
    assert.ok(resolve(path).startsWith(resolve(repo) + "\\"));
    assert.equal(lstatSync(path).isSymbolicLink(), false);
    assert.deepEqual(readdirSync(path), []); rmdirSync(path);
  }
}

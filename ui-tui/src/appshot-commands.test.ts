import test from "node:test";
import assert from "node:assert/strict";
import { runAppshotCommand } from "./appshot-commands.js";
import { readFileSync } from "node:fs";
import { APPSHOT_ERROR_CODES } from "./appshot-client.js";
test("canonical native error vocabulary parity", () =>
  assert.deepEqual(
    [...APPSHOT_ERROR_CODES],
    JSON.parse(
      readFileSync(
        new URL(
          "../../native/macos-computer-helper/Tests/Fixtures/appshot_error_codes_v1.json",
          import.meta.url,
        ),
        "utf8",
      ),
    ),
  ));
test("exact local routing and bounded validation", async () => {
  const calls: any[] = [];
  const client: any = {
    command: async (...args: any[]) => {
      calls.push(args);
      return { ok: false, code: "shortcut_conflict" };
    },
    state: {},
  };
  assert.equal(await runAppshotCommand("/appshotx status", client), null);
  assert.match(
    (await runAppshotCommand("/appshot shortcut ctrl+shift+s", client))!,
    /shortcut_conflict/,
  );
  assert.deepEqual(calls, [["shortcut", "ctrl+shift+s"]]);
  assert.match(
    (await runAppshotCommand("/appshot enable now", client))!,
    /Usage/,
  );
  assert.equal(calls.length, 1);
  assert.match(
    (await runAppshotCommand("/appshot status", undefined))!,
    /broker_unavailable/,
  );
});

test("successful settings changes refresh actual broker status", async () => {
  const calls: any[] = [];
  const client: any = {
    state: {
      enabled: false,
      chord: "ctrl+shift+s",
      registration: "registered",
      connectedTuis: 1,
      permission: "ready",
    },
    command: async (...args: any[]) => {
      calls.push(args);
      return { ok: true };
    },
  };
  assert.match(
    (await runAppshotCommand("/appshot disable", client))!,
    /disabled/,
  );
  assert.deepEqual(calls, [
    ["disable", ""],
    ["status", ""],
  ]);
});

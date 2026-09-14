import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { parseAppshotManifest, parseAppshotMessage } from "./appshot-protocol.js";
import { parseWindowsAppshotManifest, parseWindowsAppshotMessage, WindowsAppshotFrameDecoder,
  parseWindowsAppshotBrokerMessage, encodeWindowsAppshotMessage } from "./appshot-protocol-windows.js";

const cases = JSON.parse(readFileSync(new URL(
  "../../native/appshot-core/Tests/Fixtures/appshot_protocol_cases_v2.json", import.meta.url
), "utf8")) as { name: string; kind: string; raw: string; valid: boolean }[];

for (const row of cases) {
  test("Windows v2: " + row.name, () => {
    const parser = row.kind === "manifest" ? parseWindowsAppshotManifest : parseWindowsAppshotMessage;
    if (row.valid) {
      assert.equal(parser(row.raw).platform, "windows");
      const legacy = row.kind === "manifest" ? parseAppshotManifest : parseAppshotMessage;
      assert.throws(() => legacy(row.raw));
    } else assert.throws(() => parser(row.raw));
  });
}

test("Windows v2 stream framing is bounded, split-safe and direction strict", () => {
  const message = parseWindowsAppshotMessage(cases.find(c => c.valid && c.kind !== "manifest")!.raw);
  const bytes = encodeWindowsAppshotMessage(message), decoder = new WindowsAppshotFrameDecoder();
  assert.deepEqual(decoder.feed(bytes.subarray(0, 3)), []);
  assert.deepEqual(decoder.feed(bytes.subarray(3)), [message]); decoder.finish();
  assert.throws(() => new WindowsAppshotFrameDecoder().feed(Buffer.alloc(65537, 32)));
  const partial = new WindowsAppshotFrameDecoder(); partial.feed(Buffer.from("{")); assert.throws(() => partial.finish());
  assert.throws(() => parseWindowsAppshotBrokerMessage(JSON.stringify({ type: "hello", version: 2, platform: "windows",
    session_id: "s", pid: 42, process_start: "1", user_sid: "S-1-5-21-1", client_nonce: "n" })));
});

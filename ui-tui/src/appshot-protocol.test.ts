import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import {
  parseAppshotManifest,
  parseAppshotMessage,
} from "./appshot-protocol.js";
const base = new URL(
  "../../native/macos-computer-helper/Tests/Fixtures/",
  import.meta.url,
);
const fixture = () =>
  JSON.parse(readFileSync(new URL("appshot_manifest_v1.json", base), "utf8"));
test("manifest parity", () => {
  assert.equal(
    parseAppshotManifest(JSON.stringify(fixture())).source.process_start,
    "9001",
  );
});
test("all lifecycle messages and exact fields", () => {
  for (const m of JSON.parse(
    readFileSync(new URL("appshot_messages_v1.json", base), "utf8"),
  )) {
    assert.equal(parseAppshotMessage(JSON.stringify(m)).type, m.type);
    for (const change of [{ type: "unknown" }, { version: 2 }, { extra: true }])
      assert.throws(() =>
        parseAppshotMessage(JSON.stringify({ ...m, ...change })),
      );
  }
});
test("duplicate keys and bounded strict values", () => {
  for (const raw of [
    '{"schema_version":1,"schema_version":1}',
    '{"schema_version":2}',
    "[]",
    "",
    " ".repeat(65537),
  ])
    assert.throws(() => parseAppshotManifest(raw));
  for (const [path, value] of [
    ["png.name", "../x"],
    ["png.sha256", "A".repeat(64)],
    ["png.width", 0],
    ["png.mode", 420],
    ["png.device", 1],
    ["png.inode", "01"],
    ["source.process_start", "18446744073709551616"],
    ["ax.node_count", 2001],
    ["ax.depth", 65],
    ["ax.coverage", "all"],
    ["source.app_label", "x".repeat(257)],
    ["source.window_title", "x".repeat(1025)],
    ["png.extra", 1],
    ["png.link_count", 2],
  ] as const) {
    const m = fixture();
    const [a, b] = path.split(".");
    m[a][b] = value;
    assert.throws(() => parseAppshotManifest(JSON.stringify(m)), path);
  }
});
import {
  AppshotFrameDecoder,
  encodeAppshotMessage,
  parseAppshotClientMessage,
  parseAppshotBrokerMessage,
} from "./appshot-protocol.js";
test("framing, directions, UTF8 and escaped duplicate keys", () => {
  for (const row of JSON.parse(
    readFileSync(new URL("appshot_messages_v1.json", base), "utf8"),
  )) {
    const raw = JSON.stringify(row);
    const m = parseAppshotMessage(raw);
    const frame = encodeAppshotMessage(m);
    const decoder = new AppshotFrameDecoder();
    assert.deepEqual(decoder.feed(frame.subarray(0, 7)), []);
    assert.deepEqual(decoder.feed(frame.subarray(7)), [m]);
    decoder.finish();
    const parser = [
      "hello",
      "client_state",
      "attach_ack",
      "release",
      "command",
    ].includes(row.type)
      ? parseAppshotClientMessage
      : parseAppshotBrokerMessage;
    assert.deepEqual(parser(raw), m);
  }
  for (const raw of [
    "\n",
    " ".repeat(65537),
    '{"type":"hello","ty\\u0070e":"hello"}\n',
    "[".repeat(33) + "]".repeat(33) + "\n",
  ])
    assert.throws(() => new AppshotFrameDecoder().feed(Buffer.from(raw)));
  const decoder = new AppshotFrameDecoder();
  decoder.feed(Buffer.from("{"));
  assert.throws(() => decoder.finish());
  assert.throws(() => parseAppshotMessage(new Uint8Array([255])));
});

test("authoritative cross-language cases", () => {
  for (const c of JSON.parse(
    readFileSync(new URL("appshot_protocol_cases_v1.json", base), "utf8"),
  )) {
    const parser =
      c.kind === "manifest" ? parseAppshotManifest : parseAppshotMessage;
    if (c.valid) assert.doesNotThrow(() => parser(c.raw), c.raw);
    else assert.throws(() => parser(c.raw), c.raw);
  }
});

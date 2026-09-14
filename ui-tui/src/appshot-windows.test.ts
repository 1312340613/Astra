import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { validateWindowsArtifactRead, validateWindowsAppshotIdentity, validateWindowsDescriptor, type WindowsAppshotOffer } from "./appshot-windows.js";
import type { WindowsAppshotManifest } from "./appshot-protocol-windows.js";

function fixture() {
  const manifest = JSON.parse(readFileSync(new URL("../../native/appshot-core/Tests/Fixtures/appshot_manifest_v2.json", import.meta.url), "utf8")) as WindowsAppshotManifest;
  const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg==", "base64");
  const uia = JSON.stringify({ schema_version: 2, platform: "windows", coverage: "unavailable", node_count: 0, depth: 0,
    truncated: true, truncation_reasons: ["uia_unavailable"], nodes: [] });
  const hash = (data: string | Buffer) => createHash("sha256").update(data).digest("hex");
  Object.assign(manifest.png, { width: 1, height: 1, size: png.length, sha256: hash(png) });
  Object.assign(manifest.uia, { size: Buffer.byteLength(uia), sha256: hash(uia), coverage: "unavailable", node_count: 0, depth: 0, truncated: true, truncation_reasons: ["uia_unavailable"] });
  const value = { version: 2, platform: "windows", manifest, png_base64: png.toString("base64"), uia_json: uia };
  const offer: WindowsAppshotOffer = { type: "attach_offer", version: 2, platform: "windows", request_id: "offer", broker_id: manifest.broker.instance_id,
    session_id: manifest.broker.session_id, manifest_path: `C:/Fixture/appshot-${manifest.token}.manifest.json` };
  return { value, offer, recipient: structuredClone(manifest.broker) };
}
test("native Windows read returns only typed draft metadata, never bytes to the composer", () => {
  const f = fixture(), result = validateWindowsArtifactRead(f.value, f.offer, f.recipient);
  assert.deepEqual(Object.keys(result).sort(), ["requestId", "manifestPath", "appLabel", "windowTitle", "screenshotOnly", "binding"].sort());
  assert.equal(result.screenshotOnly, true); assert.deepEqual(result.binding, f.recipient);
});
for (const [name, mutate] of Object.entries<(f: ReturnType<typeof fixture>) => void>({
  "wrong parent": f => { f.value.manifest.broker.recipient.pid++; },
  "PID reuse": f => { f.value.manifest.broker.recipient.process_start = "999"; },
  "wrong SID": f => { f.recipient.recipient.user_sid = "S-1-5-21-9"; },
  "wrong session": f => { f.offer.session_id = "different"; },
  "wrong filename": f => { f.offer.manifest_path = "C:/Fixture/manifest.json"; },
  "PNG hash": f => { f.value.manifest.png.sha256 = "0".repeat(64); },
  "PNG dimensions": f => { f.value.manifest.png.width = 2; },
  "UIA hash": f => { f.value.uia_json += " "; },
  "permissive base64": f => { f.value.png_base64 += "\n"; },
  "untrusted extra field": f => { (f.value as any).authoritative_pid = 1; },
  "POSIX injection": f => { (f.value.manifest.broker as any).process_start = "1"; },
  "UNC": f => { f.offer.manifest_path = "//host/share/manifest.json"; },
})) test(`native Windows read rejects ${name}`, () => {
  const f = fixture(); mutate(f); assert.throws(() => validateWindowsArtifactRead(f.value, f.offer, f.recipient));
});
test("Windows identity and descriptor reject UID substitutes, zero starts, extras and wrong process", () => {
  const identity = { version: 2, platform: "windows", pid: 42, process_start: "1", user_sid: "S-1-5-21-1", monotonic_ns: "2" };
  assert.deepEqual(validateWindowsAppshotIdentity(identity, 42), identity);
  for (const value of [{ ...identity, uid: 501 }, { ...identity, process_start: "0" }, { ...identity, monotonic_ns: "-1" }, { ...identity, user_sid: "501" }])
    assert.throws(() => validateWindowsAppshotIdentity(value, 42));
  assert.throws(() => validateWindowsAppshotIdentity(identity, 43));
  const descriptor = { version: 2, platform: "windows", scope: "test", instance_id: "broker", broker_nonce: "nonce",
    process: { pid: 42, process_start: "1", user_sid: "S-1-5-21-1" } };
  assert.deepEqual(validateWindowsDescriptor(descriptor, "test"), descriptor);
  assert.throws(() => validateWindowsDescriptor(descriptor, "other"));
  assert.throws(() => validateWindowsDescriptor({ ...descriptor, socketPath: "C:/Fake" }, "test"));
});

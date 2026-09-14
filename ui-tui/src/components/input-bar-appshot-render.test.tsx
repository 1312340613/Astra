import assert from "node:assert/strict";
import test from "node:test";
import { PassThrough, Writable } from "node:stream";
import { readFileSync } from "node:fs";
import React, { createRef } from "react";
import { render } from "ink";
import { InputBar, type AppshotInputHandle } from "./input-bar.js";
import type { InputSubmission } from "../types.js";
const manifest = JSON.parse(
  readFileSync(
    new URL(
      "../../../native/macos-computer-helper/Tests/Fixtures/appshot_manifest_v1.json",
      import.meta.url,
    ),
    "utf8",
  ),
);
class Stdin extends PassThrough {
  isTTY = true;
  setRawMode() {}
  ref() {
    return this;
  }
  unref() {
    return this;
  }
}
class Stdout extends Writable {
  columns = 140;
  rows = 30;
  isTTY = true;
  output = "";
  _write(c: Buffer, _e: BufferEncoding, cb: () => void) {
    this.output += c.toString();
    cb();
  }
}
const tick = () => new Promise((r) => setTimeout(r, 30));
async function setup(sourceTitle?: string) {
  const fixture = structuredClone(manifest);
  if (sourceTitle) fixture.source.window_title = sourceTitle;
  const stdin = new Stdin(),
    stdout = new Stdout(),
    ref = createRef<AppshotInputHandle>(),
    sent: InputSubmission[] = [],
    released: string[] = [];
  const app = render(
    <InputBar
      ref={ref}
      onSubmit={(s) => sent.push(s)}
      disabled={false}
      sessionList={[]}
      modelList={[]}
      appshotManifestReader={() => JSON.stringify(fixture)}
      onAppshotRelease={(id) => released.push(id)}
    />,
    {
      stdin: stdin as any,
      stdout: stdout as any,
      stderr: stdout as any,
      debug: true,
      patchConsole: false,
      exitOnCtrlC: false,
    },
  );
  await tick();
  const offer = (id: string) => ({
    type: "attach_offer" as const,
    version: 1,
    request_id: id,
    broker_id: manifest.broker.instance_id,
    session_id: manifest.broker.session_id,
    manifest_path: `/fixture/appshot-${manifest.token}.manifest.json`,
  });
  const capture = (id: string, title?: string) => {
    if (title) fixture.source.window_title = title;
    const o = offer(id);
    assert.equal(ref.current!.stage(o, manifest.broker), true);
    return ref.current!.commit({ ...o, type: "attach_commit" });
  };
  const type = async (s: string) => {
    stdin.write(s);
    await tick();
  };
  return { stdin, stdout, ref, sent, released, app, capture, offer, type };
}

test("verified Windows offer stays staged until commit and never auto-submits", async () => {
  const h = await setup(); try {
    const offer = { requestId: "win", manifestPath: "C:/Fixture/appshot-0123456789abcdef0123456789abcdef.manifest.json",
      appLabel: "Windows fixture", windowTitle: "unsent screenshot", screenshotOnly: true,
      binding: { instance_id: "broker", session_id: "session", recipient: { pid: 123, process_start: "1", user_sid: "S-1-5-21-1" } } };
    assert.equal(h.ref.current!.stageWindows!(offer), true);
    assert.equal(h.ref.current!.stageWindows!(offer), false); await tick();
    assert.equal(h.ref.current!.snapshot().draft.attachments.length, 0); assert.equal(h.sent.length, 0);
    h.ref.current!.commit({ type: "attach_commit", version: 2, platform: "windows", request_id: "win",
      broker_id: "broker", session_id: "session", manifest_path: offer.manifestPath }); await tick();
    assert.equal(h.ref.current!.snapshot().draft.attachments.length, 1); assert.equal(h.sent.length, 0);
    assert.match(h.ref.current!.snapshot().draft.text, /仅截图/);
    await h.type("\r"); assert.equal(h.sent.length, 1);
    assert.equal(h.sent[0].appshots?.[0].manifest_path, offer.manifestPath);
  } finally { h.app.unmount(); }
});
test("stage is hidden, commit inserts at cursor without submit, submit freezes until matching acceptance", async () => {
  const h = await setup();
  try {
    await h.type("AB");
    await h.type("\x1b[D");
    const o = h.offer("a");
    assert.equal(h.ref.current!.stage(o, manifest.broker), true);
    await tick();
    assert.ok(!h.stdout.output.includes("[Appshot #1"));
    assert.deepEqual(h.ref.current!.commit({ ...o, type: "attach_commit" }), {
      appshotCount: 1,
      canAccept: true,
    });
    await tick();
    assert.equal(h.sent.length, 0);
    assert.match(h.ref.current!.snapshot().draft.text, /^A\[Appshot #1.*\]B$/);
    await h.type("\r");
    assert.equal(h.sent.length, 1);
    assert.equal(h.sent[0].appshots.length, 1);
    const id = h.sent[0].submissionId!;
    await h.type("new");
    await h.type("\r");
    assert.equal(h.sent.length, 1);
    h.ref.current!.acceptSubmission("stale");
    assert.equal(h.ref.current!.snapshot().pending?.submissionId, id);
    h.ref.current!.acceptSubmission(id);
    await tick();
    assert.equal(h.ref.current!.snapshot().draft.text, "new");
    assert.deepEqual(h.released, ["a"]);
  } finally {
    h.app.unmount();
  }
});
test("cap includes frozen pending and rejection prepends retained content", async () => {
  const h = await setup();
  try {
    for (const id of ["a", "b", "c"]) h.capture(id);
    await h.type("\r");
    const id = h.sent[0].submissionId!;
    await h.type("new");
    assert.deepEqual(h.capture("d"), { appshotCount: 4, canAccept: false });
    assert.equal(h.ref.current!.stage(h.offer("e"), manifest.broker), false);
    h.ref.current!.rejectSubmission(id);
    await tick();
    assert.deepEqual(
      h.ref.current!.snapshot().draft.attachments.map((a) => a.requestId),
      ["a", "b", "c", "d"],
    );
    assert.ok(h.ref.current!.snapshot().draft.text.includes("new"));
  } finally {
    h.app.unmount();
  }
});
test("damaged placeholder releases; disconnect retains uncertain frozen content; discard only pending", async () => {
  const h = await setup();
  try {
    h.capture("a");
    await tick();
    await h.type("\x7f");
    assert.equal(h.ref.current!.snapshot().draft.attachments.length, 0);
    assert.deepEqual(h.released, ["a"]);
    h.capture("b");
    await h.type("\r");
    const id = h.sent[0].submissionId!;
    await h.type("new");
    h.capture("c");
    h.ref.current!.disconnect({
      reason: "recipient_disconnected",
      unsent: "revoked",
      pending: "unknown",
    });
    await tick();
    assert.equal(h.ref.current!.snapshot().pending?.status, "unknown");
    assert.equal(h.ref.current!.snapshot().draft.text, "new");
    h.ref.current!.discardSubmission(id);
    assert.equal(h.ref.current!.snapshot().draft.text, "new");
  } finally {
    h.app.unmount();
  }
});

test("image and pasted text payloads survive rejection with newer independent placeholders", async () => {
  const h = await setup();
  try {
    await h.type("/tmp/first.png");
    await h.type(" first\nsecond");
    h.capture("a");
    await h.type("\r");
    const first = h.sent[0];
    assert.match(first.text, /\/tmp\/first.png/);
    assert.match(first.text, /first\nsecond/);
    await h.type("/tmp/new.png");
    await h.type(" new\nparagraph");
    h.capture("b");
    h.ref.current!.rejectSubmission(first.submissionId!);
    await tick();
    await h.type("\r");
    const second = h.sent[1];
    assert.match(second.text, /\/tmp\/first.png/);
    assert.match(second.text, /\/tmp\/new.png/);
    assert.match(second.text, /first\nsecond/);
    assert.match(second.text, /new\nparagraph/);
    assert.equal(second.appshots.length, 2);
  } finally {
    h.app.unmount();
  }
});

test("ordinary edits around duplicate presentation text cannot retarget typed custody", async () => {
  const h = await setup();
  try {
    h.capture("a");
    await tick();
    const label = h.ref.current!.snapshot().draft.attachments[0].label;
    await h.type(label);
    assert.equal(h.ref.current!.snapshot().draft.attachments.length, 1);
    await h.type("\r");
    assert.equal(h.sent[0].appshots.length, 1);
    assert.ok(!h.sent[0].text.includes(manifest.source.window_title));
  } finally {
    h.app.unmount();
  }
});

test("source title image paths remain metadata through ordinary editing", async () => {
  const h = await setup("/tmp/untrusted-image.png");
  try {
    h.capture("a");
    await h.type(" hello");
    await h.type("\r");
    assert.equal(h.sent[0].appshots.length, 1);
    assert.ok(!h.sent[0].text.includes("/tmp/untrusted-image.png"));
    assert.equal(h.sent[0].text, "[Appshot #1] hello");
  } finally {
    h.app.unmount();
  }
});

test("pending discard command with newer capture stays local and preserves the editable segment", async () => {
  const h = await setup();
  try {
    h.capture("a");
    await h.type("\r");
    const pending = h.sent[0].submissionId!;
    await h.type("/appshot pending discard");
    h.capture("b");
    await h.type(" newer");
    await h.type("\r");
    assert.equal(h.sent.length, 2);
    assert.deepEqual(h.sent[1], {
      text: "/appshot pending discard",
      appshots: [],
    });
    assert.equal(h.ref.current!.snapshot().pending?.submissionId, pending);
    assert.deepEqual(
      h.ref.current!.snapshot().draft.attachments.map((a) => a.requestId),
      ["b"],
    );
    assert.match(h.ref.current!.snapshot().draft.text, /newer/);
    h.ref.current!.discardSubmission(pending);
    assert.deepEqual(
      h.ref.current!.snapshot().draft.attachments.map((a) => a.requestId),
      ["b"],
    );
  } finally {
    h.app.unmount();
  }
});
test("local Appshot command with captures never becomes a typed backend message", async () => {
  const h = await setup();
  try {
    await h.type("/appshot status");
    h.capture("a");
    await h.type("\r");
    assert.deepEqual(h.sent, [{ text: "/appshot status", appshots: [] }]);
    assert.equal(h.ref.current!.snapshot().draft.attachments.length, 1);
  } finally {
    h.app.unmount();
  }
});

for (const kind of ["image", "paste"] as const) {
  test(`rejection preserves newer capture containing ordinary ${kind} placeholder`, async () => {
    const title = kind === "image" ? "[Image #1]" : "[Pasted text #1 · 3 chars · 2 lines]";
    const h = await setup(title);
    try {
      await h.type(kind === "image" ? "/tmp/first.png" : "a\nb");
      h.capture("a");
      await h.type("\r");
      await h.type(kind === "image" ? "/tmp/new.png" : "c\nd");
      h.capture("b");
      h.ref.current!.rejectSubmission(h.sent[0].submissionId!);
      await tick();
      const d = h.ref.current!.snapshot().draft;
      assert.deepEqual(d.attachments.map(a => a.requestId), ["a", "b"]);
      for (const a of d.attachments) assert.equal(d.text.slice(a.start, a.start + a.label.length), a.label);
      assert.deepEqual(h.released, []);
      await h.type("\r");
      assert.equal(h.sent[1].appshots.length, 2);
      assert.ok(h.sent[1].text.includes(kind === "image" ? "/tmp/new.png" : "c\nd"));
    } finally { h.app.unmount(); }
  });
}
for (const pending of [false, true]) {
  test(`adjacent typed capture permits local reconnect (pending=${pending})`, async () => {
    const h = await setup();
    try {
      if (pending) { h.capture("a"); await h.type("\r"); }
      const prior = h.ref.current!.snapshot().pending;
      await h.type("/reconnect");
      h.capture("b");
      await h.type("\r");
      assert.deepEqual(h.sent.at(-1), {text: "/reconnect", appshots: []});
      assert.equal(h.ref.current!.snapshot().pending, prior);
      assert.deepEqual(h.ref.current!.snapshot().draft.attachments.map(a => a.requestId), ["b"]);
    } finally { h.app.unmount(); }
  });
}
test("nested labels survive component edit and positional shielding in reverse order", async () => {
  const h = await setup("B");
  try {
    h.capture("a");
    const label = h.ref.current!.snapshot().draft.attachments[0].label;
    for (const _ of Array.from(label)) await h.type("\x1b[D");
    h.capture("b", label);
    const before = h.ref.current!.snapshot().draft;
    await h.type(" hello");
    const after = h.ref.current!.snapshot().draft;
    assert.equal(after.attachments.length, 2);
    for (const a of after.attachments) assert.equal(after.text.slice(a.start, a.start + a.label.length), a.label);
    assert.deepEqual(after.attachments.map(a => a.label), before.attachments.map(a => a.label));
    await h.type("\r");
    assert.equal(h.sent[0].text, "[Appshot #2] hello[Appshot #1]");
  } finally { h.app.unmount(); }
});

test("unowned bracket text does not form a local reconnect boundary", async () => {
  const h = await setup();
  try {
    h.capture("a");
    await h.type("\r");
    const prior = h.ref.current!.snapshot().pending;
    await h.type("/reconnect[ordinary]");
    await h.type("\r");
    assert.equal(h.sent.length, 1);
    assert.equal(h.ref.current!.snapshot().pending, prior);
    assert.equal(h.ref.current!.snapshot().draft.text, "/reconnect[ordinary]");
  } finally { h.app.unmount(); }
});

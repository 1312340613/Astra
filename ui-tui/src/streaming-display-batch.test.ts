import assert from "node:assert/strict";
import test from "node:test";
import type { RenderLine } from "./markdown-render-lines.js";
import * as display from "./streaming-display-state.js";

const line = (key: string): RenderLine => ({ key, role: "assistant", text: key, prefix: "", kind: "text", spans: [] });

test("a burst preserves all history, stream boundaries and prior state", () => {
  const original = display.createStreamingDisplayState(Array.from({ length: 2600 }, (_, i) => line(String(i))));
  Object.freeze(original.historyLines);
  const actions: display.StreamingDisplayAction[] = [
    { type: "applyStreamUpdate", role: "assistant", committed: [line("a")], preview: [line("preview")] },
    { type: "appendActivity", lines: [line("activity")] },
    { type: "setToolDetailOpen", open: true },
    { type: "applyStreamUpdate", role: "assistant", committed: [line("b")], preview: [] },
    { type: "appendActivity", lines: [line("tool")] },
    { type: "setToolDetailOpen", open: false },
    { type: "applyStreamUpdate", role: "reasoning", committed: [line("reason")], preview: [line("pending")] },
    { type: "hideDynamicTimeRails" },
    { type: "clearStreaming" },
  ];
  const expected = actions.reduce(display.streamingDisplayReducer, original);
  const actual = display.streamingDisplayReducer(original, { type: "batch", actions } as display.StreamingDisplayAction);
  assert.deepEqual(actual, expected);
  assert.equal(original.historyLines.length, 2600);
  assert.equal(actual.historyLines.length, 2605);
  assert.deepEqual(actual.historyLines.slice(-5).map(item => item.key), ["a", "b", "activity", "tool", "reason"]);
});

test("replacement and reset inside a burst never mutate action arrays", () => {
  const restored = [line("restored")];
  Object.freeze(restored);
  const actions: display.StreamingDisplayAction[] = [
    { type: "appendActivity", lines: [line("old")] },
    { type: "replaceHistory", lines: restored },
    { type: "appendActivity", lines: [line("new")] },
  ];
  const original = display.createStreamingDisplayState();
  assert.deepEqual(display.streamingDisplayReducer(original, { type: "batch", actions } as display.StreamingDisplayAction), actions.reduce(display.streamingDisplayReducer, original));
  assert.deepEqual(restored.map(item => item.key), ["restored"]);
  const clearActions: display.StreamingDisplayAction[] = [...actions, { type: "clearHistory" }, { type: "appendActivity", lines: [line("after-reset")] }];
  assert.deepEqual(display.streamingDisplayReducer(original, { type: "batch", actions: clearActions } as display.StreamingDisplayAction), clearActions.reduce(display.streamingDisplayReducer, original));
});

test("a synchronous burst copies pre-existing history only once", () => {
  const anchor = line("anchor");
  const original = display.createStreamingDisplayState([anchor, ...Array.from({ length: 9999 }, (_, i) => line(String(i)))]);
  const actions: display.StreamingDisplayAction[] = Array.from({ length: 100 }, (_, i) => ({
    type: "applyStreamUpdate", role: "assistant", committed: [line(`new-${i}`)], preview: [],
  }));
  const iterate = Array.prototype[Symbol.iterator];
  let copiedHistoryEntries = 0;
  Array.prototype[Symbol.iterator] = function () {
    if (this[0] === anchor) copiedHistoryEntries += this.length;
    return iterate.call(this);
  };
  let result: display.StreamingDisplayState;
  try {
    result = display.streamingDisplayReducer(original, { type: "batch", actions });
  } finally {
    Array.prototype[Symbol.iterator] = iterate;
  }
  assert.equal(result!.historyLines.length, 10100);
  assert.equal(copiedHistoryEntries, 10000, "copy the old prefix once, not once per commit");
  assert.equal(original.historyLines.length, 10000);
});

test("streaming bursts publish once at the microtask boundary", async () => {
  assert.equal(typeof display.createStreamingDisplayBatcher, "function");
  const published: display.StreamingDisplayAction[] = [];
  const batcher = display.createStreamingDisplayBatcher(action => published.push(action));
  for (let i = 0; i < 100; i++) batcher.enqueue({ type: "applyStreamUpdate", role: "assistant", committed: [line(String(i))], preview: [] });
  assert.equal(published.length, 0);
  await Promise.resolve();
  assert.equal(published.length, 1);
  const state = published.reduce(display.streamingDisplayReducer, display.createStreamingDisplayState());
  assert.deepEqual(state.historyLines.map(item => item.key), Array.from({ length: 100 }, (_, i) => String(i)));
});

test("terminal and control actions flush prior chunks synchronously", async () => {
  assert.equal(typeof display.createStreamingDisplayBatcher, "function");
  const published: display.StreamingDisplayAction[] = [];
  const batcher = display.createStreamingDisplayBatcher(action => published.push(action));
  batcher.enqueue({ type: "applyStreamUpdate", role: "assistant", committed: [line("before-final")], preview: [line("old-preview")] });
  batcher.dispatch({ type: "applyStreamUpdate", role: "assistant", committed: [line("final")], preview: [] });
  batcher.dispatch({ type: "appendActivity", lines: [line("cancel-notice")] });
  const state = published.reduce(display.streamingDisplayReducer, display.createStreamingDisplayState());
  assert.deepEqual(state.historyLines.map(item => item.key), ["before-final", "final", "cancel-notice"]);
  assert.deepEqual(display.orderedDynamicDisplayLines(state), []);
  assert.equal(published.length, 3);
  await Promise.resolve();
  assert.equal(published.length, 3, "a previously scheduled flush cannot replay committed history");
});

test("reset and explicit flush preserve order without replay", async () => {
  assert.equal(typeof display.createStreamingDisplayBatcher, "function");
  const published: display.StreamingDisplayAction[] = [];
  const batcher = display.createStreamingDisplayBatcher(action => published.push(action));
  batcher.enqueue({ type: "appendActivity", lines: [line("old")] });
  batcher.dispatch({ type: "clearHistory" });
  batcher.enqueue({ type: "appendActivity", lines: [line("after-reset")] });
  batcher.flush();
  assert.deepEqual(published.reduce(display.streamingDisplayReducer, display.createStreamingDisplayState()).historyLines.map(item => item.key), ["after-reset"]);
  assert.equal(published.length, 3);
  await Promise.resolve();
  assert.equal(published.length, 3);
});

test("a later burst cannot mutate a previously published batch", () => {
  const first = display.streamingDisplayReducer(display.createStreamingDisplayState(), {
    type: "batch", actions: [
      { type: "appendActivity", lines: [line("first")] },
      { type: "appendActivity", lines: [line("second")] },
    ],
  });
  Object.freeze(first.historyLines);
  const second = display.streamingDisplayReducer(first, {
    type: "batch", actions: [
      { type: "appendActivity", lines: [line("third")] },
      { type: "appendActivity", lines: [line("fourth")] },
    ],
  });
  assert.deepEqual(first.historyLines.map(item => item.key), ["first", "second"]);
  assert.deepEqual(second.historyLines.map(item => item.key), ["first", "second", "third", "fourth"]);
});

test("large unfinished previews have bounded render work while full history commits", () => {
  const preview = Array.from({ length: 10000 }, (_, i) => line(`preview-${i}`));
  const state = display.streamingDisplayReducer(display.createStreamingDisplayState(), {
    type: "applyStreamUpdate", role: "assistant", committed: [], preview,
  });
  assert.equal(display.visibleDynamicDisplayLines(state, 30).length, 30);
  assert.equal(display.visibleDynamicDisplayLines(state, 30)[0], preview[9970]);
  assert.equal(state.previewLines.assistant?.length, 10000);
  const finished = display.streamingDisplayReducer(state, {
    type: "applyStreamUpdate", role: "assistant", committed: preview, preview: [],
  });
  assert.equal(finished.historyLines.length, 10000);
  assert.deepEqual(display.visibleDynamicDisplayLines(finished, 30), []);
});

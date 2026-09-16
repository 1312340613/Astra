import assert from "node:assert/strict";
import test from "node:test";
import stringWidth from "string-width";
import { inputViewport } from "./input-viewport.js";
import { textPreviewInterval } from "./text-event-batcher.js";

test("viewport follows the cursor without splitting graphemes or modifying the draft", () => {
  const draft = "中👩‍💻🇸🇬e\u0301".repeat(100) + "END";
  for (const part of new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(draft)) {
    const view = inputViewport(draft, part.index, 38);
    assert.ok(stringWidth(view.value) <= 38);
    assert.equal(view.value.slice(view.cursor, view.cursor + part.segment.length), part.segment);
  }
  const end = inputViewport(draft, draft.length, 38);
  assert.match(end.value, /END$/);
  assert.equal(end.cursor, end.value.length);
  assert.deepEqual(inputViewport("hello", 2, 38), { value: "hello", cursor: 2 });
});

test("Apple Terminal preview cadence is explicit and other terminals keep their default", () => {
  assert.equal(textPreviewInterval("Apple_Terminal"), 80);
  assert.equal(textPreviewInterval("Windows_Terminal"), 32);
});

import assert from "node:assert/strict";
import test from "node:test";
import stringWidth from "string-width";

import {
  clampToolDetailOffset,
  shouldCollapseToolResult,
  summarizeToolResult,
  wrapToolResult,
} from "./tool-results.js";

test("long or multi-line tool results collapse by default", () => {
  assert.equal(shouldCollapseToolResult("x".repeat(701)), true);
  assert.equal(shouldCollapseToolResult("1\n2\n3\n4\n5\n6\n7"), true);
  assert.equal(shouldCollapseToolResult("short\nresult"), false);
});

test("tool result summary uses the first meaningful line", () => {
  assert.equal(summarizeToolResult("\n  Search results: 5\nsecond"), "Search results: 5");
  assert.equal(summarizeToolResult("x".repeat(20), 10), "xxxxxxxxx…");
});

test("tool details wrap and clamp pagination", () => {
  assert.deepEqual(wrapToolResult("abcdefgh\nnext", 4), ["abcd", "efgh", "next"]);
  assert.deepEqual(wrapToolResult("中文测试", 4), ["中文", "测试"]);
  assert.equal(clampToolDetailOffset(-1, 20, 5), 0);
  assert.equal(clampToolDetailOffset(99, 20, 5), 15);
});

test("tool text bounds never split emoji grapheme clusters", () => {
  const emoji = "👩🏽‍💻❤️";

  assert.deepEqual(wrapToolResult(emoji, 2), ["👩🏽‍💻", "❤️"]);
  assert.equal(summarizeToolResult(`A${emoji}B`, 4), "A👩🏽‍💻…");
  assert.equal(stringWidth(summarizeToolResult(`A${emoji}B`, 4)) <= 4, true);
});

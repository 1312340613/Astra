import assert from "node:assert/strict";
import stringWidth from "string-width";
import { shortenToWidth, suggestionCommandWidth } from "./input-bar.js";

const items = [{ command: "dawncrow-qwen3.6-35b-a3b-long" }, { command: "gemma-4" }];

assert.equal(suggestionCommandWidth(items, 150, "wide"), 33);
assert.equal(suggestionCommandWidth(items, 90, "standard"), 33);
assert.equal(suggestionCommandWidth(items, 32, "compact"), 26);

assert.equal(shortenToWidth("short", 5), "short");
assert.equal(shortenToWidth("abcdef", 5), "abcd…");
assert.equal(shortenToWidth("中文说明", 5), "中文…");
assert.equal(shortenToWidth("A猫🙂B", 5), "A猫…");
assert.equal(shortenToWidth("anything", 1), "…");
assert.equal(shortenToWidth("anything", 0), "");
for (const value of ["abcdef", "中文说明", "A猫🙂B"]) {
  assert.ok(stringWidth(shortenToWidth(value, 5)) <= 5);
}

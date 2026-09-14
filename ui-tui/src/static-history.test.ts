import assert from "node:assert/strict";
import {
  appendLiveHistory,
  limitRestoredHistory,
  restoreRecentHistory,
} from "./static-history.js";

const restored = Array.from({ length: 2600 }, (_item, index) => index);
assert.deepEqual(limitRestoredHistory(restored, 2500), restored.slice(100));

const messages = Array.from({ length: 10_000 }, (_item, index) => index);
let converted = 0;
const recent = restoreRecentHistory(messages, 5, (message) => {
  converted += 1;
  return [`${message}:a`, `${message}:b`];
});
assert.deepEqual(recent, ["9997:b", "9998:a", "9998:b", "9999:a", "9999:b"]);
assert.equal(converted, 3);
assert.deepEqual(restoreRecentHistory(messages, 0, (message) => [message]), []);

const atOldBoundary = Array.from({ length: 2500 }, (_item, index) => index);
const appended = appendLiveHistory(atOldBoundary, [2500, 2501]);
assert.equal(appended.length, 2502);
assert.deepEqual(appended.slice(-2), [2500, 2501]);
assert.notEqual(appended, atOldBoundary);

assert.equal(appendLiveHistory(appended, []), appended);

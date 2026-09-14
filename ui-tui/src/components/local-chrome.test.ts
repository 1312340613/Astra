import assert from "node:assert/strict";
import { localModeStatus } from "./local-chrome.js";

assert.equal(localModeStatus({ busy: false, disconnected: false }), "LOCAL MODE");
assert.equal(localModeStatus({ busy: true, disconnected: false }), "WORKING…");
assert.equal(localModeStatus({ busy: false, disconnected: true }), "SIGNAL LOST");

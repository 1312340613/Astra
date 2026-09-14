import assert from "node:assert/strict";
import { minimalModeStatus } from "./minimal-chrome.js";

assert.equal(minimalModeStatus({ busy: false, disconnected: false, toolCount: 0 }), "MINIMAL OPEN");
assert.equal(minimalModeStatus({ busy: true, disconnected: false, toolCount: 0 }), "RUNNING");
assert.equal(minimalModeStatus({ busy: false, disconnected: false, toolCount: 1 }), "RUNNING");
assert.equal(minimalModeStatus({ busy: false, disconnected: true, toolCount: 0 }), "SIGNAL LOST");

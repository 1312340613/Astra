import assert from "node:assert/strict";
import { startupMcpSummary, startupPhase, startupProgress } from "./startup-screen.js";

const frame = startupProgress(0, 8);
assert.equal(frame.length, 8);
assert.equal(frame.includes("━"), true);
assert.notEqual(startupProgress(0, 8), startupProgress(1, 8));
assert.equal(startupPhase(0), "MODEL BUS HANDSHAKE");
assert.equal(startupPhase(7), "MEMORY CORE MOUNT");
assert.equal(startupPhase(28), "MODEL BUS HANDSHAKE");

assert.deepEqual(startupMcpSummary({
  skills: 15,
  tools: 72,
  model: "test-model",
  learning: { mode: "review", auto: true, pending: 0 },
  mcp: [
    { name: "ready", state: "ready" },
    { name: "broken", state: "error", error: "offline" },
    { name: "off", state: "disabled" },
  ],
}), { ready: 1, total: 2, errors: 1 });

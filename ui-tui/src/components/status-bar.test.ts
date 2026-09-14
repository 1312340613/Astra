import assert from "node:assert/strict";
import { buildStatusParts, computerIndicator, normalizeComputerControl, sanitizeComputerApplication } from "./status-bar.js";
import stringWidth from "string-width";
import { THEMES } from "../theme.js";
import * as statusBar from "./status-bar.js";
import type { ComputerStateEvent } from "../types.js";

function join(segments: { text: string; color?: string }[]): string {
  return segments.map((s) => s.text).join("");
}

const compact = buildStatusParts({
  columns: 120,
  model: "Qwen3.6-35B-A3B",
  sessionName: "session_20260518_164649",
  promptTokens: 0,
  totalTokens: 0,
  contextPct: 0,
  contextLimit: 196_608,
  showReasoning: false,
});

assert.equal(join(compact).includes("session_20260518_164649"), true);
assert.equal(join(compact).length <= 116, true);

const narrow = buildStatusParts({
  columns: 80,
  model: "Qwen3.6-35B-A3B",
  sessionName: "session_20260518_164649",
  promptTokens: 0,
  totalTokens: 0,
  contextPct: 0,
  contextLimit: 196_608,
  showReasoning: false,
});

assert.equal(join(narrow).includes("session_20260518_164649"), false);
assert.equal(join(narrow).length <= 76, true);

const glitchCity = buildStatusParts({
  columns: 120,
  model: "Qwen3.6-35B-A3B",
  promptTokens: 0,
  totalTokens: 0,
  contextPct: 12,
  contextLimit: 196_608,
  theme: THEMES.glitchcity,
});

assert.equal(join(glitchCity).includes(" │ ctx "), true);
assert.equal(join(glitchCity).includes(" · "), false);

assert.equal(
  computerIndicator({ active: true, handedOff: false, control: "background", application: "WPS Office" }).label,
  "COOP · WPS Office",
);
assert.equal(
  computerIndicator({ active: true, handedOff: true, application: "Finder" }).label,
  "USER CONTROL · Finder",
);
assert.equal(
  computerIndicator({ active: false, handedOff: false, permission: "degraded" }).label,
  "COMPUTER DEGRADED",
);
assert.equal(computerIndicator({ active: true, handedOff: false, control: "foreground_takeover", application: "WPS Office" }).label, "TAKEOVER · WPS Office");
assert.equal(computerIndicator({ active: true, handedOff: false, control: "paused", application: "WPS Office" }).label, "COMPUTER PAUSED");
assert.equal(computerIndicator({ active: true, handedOff: true, control: "paused", application: "Finder" }).label, "USER CONTROL · Finder");
assert.equal(normalizeComputerControl({ active: true, handedOff: true }), "user_control");
assert.equal(normalizeComputerControl({ active: true, handedOff: false }), "background");
assert.equal(normalizeComputerControl({ active: false, handedOff: false }), "inactive");
assert.equal(normalizeComputerControl({ active: true, handedOff: false, control: "stale" as never }), "background");
const narrowComputer = buildStatusParts({
  columns: 48,
  model: "Qwen3.6-35B-A3B",
  promptTokens: 0,
  totalTokens: 0,
  contextPct: 0,
  computer: { active: true, handedOff: false, application: "WPS Office" },
});
assert.equal(join(narrowComputer).length <= 44, true);

const legacyEvent: ComputerStateEvent = {
  type: "computer_state",
  active: true,
  handed_off: false,
  application: "stale replay",
  permission: "available",
};
for (const strong of ["paused", "foreground_takeover", "user_control"] as const) {
  const current = {
    active: true,
    handedOff: strong === "user_control",
    control: strong,
    application: "Current app",
    permission: "available" as const,
  };
  assert.deepEqual(statusBar.mergeComputerStateEvent(current, legacyEvent), current);
  assert.equal(statusBar.mergeComputerStateEvent(
    current,
    { ...legacyEvent, control: "background", application: "WPS Office" },
  ).control, "background");
}
const takeover = statusBar.mergeComputerStateEvent(
  { active: true, handedOff: false, control: "background", application: "WPS Office" },
  { ...legacyEvent, control: "foreground_takeover", application: "WPS Office" },
);
assert.equal(takeover.control, "foreground_takeover");
assert.equal(statusBar.mergeComputerStateEvent(
  takeover,
  { ...legacyEvent, control: "background", application: "WPS Office" },
).control, "background");

const unsafeApplication = "WPS\u001b[2J\u001b]8;;https://bad.example\u0007文档\u202e👩‍💻";
assert.equal(sanitizeComputerApplication(unsafeApplication), "WPS文档👩‍💻");
assert.equal(sanitizeComputerApplication("WPS\u200b\u2060文档👩🏽‍💻❤️"), "WPS文档👩🏽‍💻❤️");
for (const columns of [28, 32, 40, 48]) {
  const parts = buildStatusParts({
    columns,
    model: "模型👩‍💻e\u0301",
    promptTokens: 0,
    totalTokens: 0,
    contextPct: 0,
    computer: { active: true, handedOff: false, application: unsafeApplication },
  });
  assert.equal(stringWidth(join(parts)) <= columns - 4, true);
  assert.equal(join(parts).includes("\u001b"), false);
}
const tinyNormal = buildStatusParts({
  columns: 20,
  model: "模型👩‍💻e\u0301",
  promptTokens: 0,
  totalTokens: 0,
  contextPct: 0,
});
assert.equal(stringWidth(join(tinyNormal)) <= 16, true);

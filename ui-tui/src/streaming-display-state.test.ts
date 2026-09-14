import assert from "node:assert/strict";
import type { RenderLine } from "./markdown-render-lines.js";
import {
  createStreamingDisplayState,
  orderedDynamicDisplayLines,
  streamingDisplayReducer,
} from "./streaming-display-state.js";

function line(
  key: string,
  text = key,
  role: RenderLine["role"] = "system",
  railed = false,
): RenderLine {
  return {
    key,
    role,
    text,
    prefix: "    ",
    kind: "text",
    spans: [{ kind: "text", text }],
    timeRail: railed
      ? { label: "09:44", kind: "absolute", width: 7, prefixWidth: 6 }
      : undefined,
  };
}

let state = createStreamingDisplayState([line("static-anchor", "static", "assistant", true)]);
state = streamingDisplayReducer(state, {
  type: "applyStreamUpdate",
  role: "assistant",
  committed: [],
  preview: [line("preview-1", "unfinished assistant", "assistant", true)],
});
state = streamingDisplayReducer(state, {
  type: "appendActivity",
  lines: [line("steering", "steering notice")],
});
state = streamingDisplayReducer(state, {
  type: "appendActivity",
  lines: [line("theme", "theme switched")],
});
state = streamingDisplayReducer(state, {
  type: "appendActivity",
  lines: [line("cancel", "Cancelling active task")],
});

assert.deepEqual(state.historyLines.map((item) => item.key), ["static-anchor"]);
assert.deepEqual(
  orderedDynamicDisplayLines(state).map((item) => item.key),
  ["preview-1", "steering", "theme", "cancel"],
);
assert.equal(state.previewLines.assistant?.[0]?.timeRail?.label, "09:44");

state = streamingDisplayReducer(state, { type: "hideDynamicTimeRails" });
assert.equal(state.historyLines[0]?.timeRail?.label, "09:44", "committed Static rail stays intact");
assert.equal(state.previewLines.assistant?.[0]?.timeRail, undefined);
assert.equal(state.pendingActivityTail.every((item) => item.timeRail === undefined), true);

state = streamingDisplayReducer(state, {
  type: "applyStreamUpdate",
  role: "assistant",
  committed: [line("assistant-final", "finished assistant", "assistant")],
  preview: [],
});
assert.deepEqual(
  state.historyLines.map((item) => item.key),
  ["static-anchor", "assistant-final", "steering", "theme", "cancel"],
);
assert.deepEqual(orderedDynamicDisplayLines(state), []);

let mixed = createStreamingDisplayState();
mixed = streamingDisplayReducer(mixed, {
  type: "applyStreamUpdate",
  role: "reasoning",
  committed: [line("safe-prefix", "safe prefix", "reasoning")],
  preview: [line("reasoning-preview", "pending thought", "reasoning")],
});
mixed = streamingDisplayReducer(mixed, {
  type: "appendActivity",
  lines: [line("later", "later activity")],
});
mixed = streamingDisplayReducer(mixed, {
  type: "applyStreamUpdate",
  role: "reasoning",
  committed: [line("next-safe", "next safe", "reasoning")],
  preview: [line("next-preview", "next pending", "reasoning")],
});
assert.deepEqual(
  mixed.historyLines.map((item) => item.key),
  ["safe-prefix", "next-safe", "later"],
);
assert.deepEqual(
  orderedDynamicDisplayLines(mixed).map((item) => item.key),
  ["next-preview"],
);

let detailBoundary = createStreamingDisplayState();
detailBoundary = streamingDisplayReducer(detailBoundary, {
  type: "setToolDetailOpen",
  open: true,
});
detailBoundary = streamingDisplayReducer(detailBoundary, {
  type: "applyStreamUpdate",
  role: "assistant",
  committed: [],
  preview: [line("detail-old-preview", "old preview", "assistant")],
});
detailBoundary = streamingDisplayReducer(detailBoundary, {
  type: "appendActivity",
  lines: [line("detail-tail", "steering while old preview is active")],
});
detailBoundary = streamingDisplayReducer(detailBoundary, {
  type: "applyStreamUpdate",
  role: "assistant",
  committed: [line("detail-old-final", "old final", "assistant")],
  preview: [line("detail-new-preview", "new preview", "assistant")],
});
assert.deepEqual(detailBoundary.historyLines, []);
assert.deepEqual(
  detailBoundary.toolDetailQueue.map((item) => item.key),
  ["detail-old-final", "detail-tail"],
);
assert.deepEqual(
  orderedDynamicDisplayLines(detailBoundary).map((item) => item.key),
  ["detail-new-preview"],
);

let detail = createStreamingDisplayState();
detail = streamingDisplayReducer(detail, { type: "setToolDetailOpen", open: true });
detail = streamingDisplayReducer(detail, {
  type: "applyStreamUpdate",
  role: "assistant",
  committed: [],
  preview: [line("detail-preview", "preview while detail open", "assistant")],
});
detail = streamingDisplayReducer(detail, {
  type: "appendActivity",
  lines: [line("detail-steering", "steering while detail open")],
});
detail = streamingDisplayReducer(detail, {
  type: "applyStreamUpdate",
  role: "assistant",
  committed: [line("detail-final", "final while detail open", "assistant")],
  preview: [],
});
detail = streamingDisplayReducer(detail, {
  type: "appendActivity",
  lines: [line("detail-tool", "tool while detail open", "tool")],
});
assert.deepEqual(detail.historyLines, []);
assert.deepEqual(
  detail.toolDetailQueue.map((item) => item.key),
  ["detail-final", "detail-steering", "detail-tool"],
);
assert.deepEqual(orderedDynamicDisplayLines(detail), []);

detail = streamingDisplayReducer(detail, { type: "setToolDetailOpen", open: false });
assert.deepEqual(
  detail.historyLines.map((item) => item.key),
  ["detail-final", "detail-steering", "detail-tool"],
);
assert.deepEqual(detail.toolDetailQueue, []);

let ctrlC = createStreamingDisplayState();
ctrlC = streamingDisplayReducer(ctrlC, {
  type: "applyStreamUpdate",
  role: "assistant",
  committed: [],
  preview: [line("ctrl-preview", "still streaming", "assistant")],
});
const previewBeforeCtrlC = ctrlC.previewLines.assistant;
ctrlC = streamingDisplayReducer(ctrlC, {
  type: "appendActivity",
  lines: [line("ctrl-c", "Cancelling active task")],
});
assert.strictEqual(ctrlC.previewLines.assistant, previewBeforeCtrlC);
assert.deepEqual(
  orderedDynamicDisplayLines(ctrlC).map((item) => item.key),
  ["ctrl-preview", "ctrl-c"],
);

const replaced = streamingDisplayReducer(ctrlC, {
  type: "replaceHistory",
  lines: [line("restored", "restored")],
});
assert.deepEqual(replaced.historyLines.map((item) => item.key), ["restored"]);
assert.deepEqual(orderedDynamicDisplayLines(replaced), []);
assert.deepEqual(replaced.toolDetailQueue, []);

console.log("streaming display reducer tests passed");

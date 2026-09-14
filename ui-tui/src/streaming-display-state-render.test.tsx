import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { Writable } from "node:stream";
import React, { useEffect, useReducer, useState } from "react";
import stripAnsi from "strip-ansi";
import type { RenderLine } from "./markdown-render-lines.js";
import {
  createStreamingDisplayState,
  createStreamingDisplayBatcher,
  orderedDynamicDisplayLines,
  streamingDisplayReducer,
} from "./streaming-display-state.js";
import { THEMES } from "./theme.js";
import { ThemeProvider } from "./theme-context.js";

process.env.FORCE_COLOR = "3";
const { Box, Static, render } = await import("ink");
const { MessageLine } = await import("./app.js");

class CaptureStream extends Writable {
  rows = 12;
  columns = 50;
  isTTY = true;
  chunks: string[] = [];

  getColorDepth(): number {
    return 24;
  }

  _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void) {
    this.chunks.push(String(chunk));
    callback();
  }
}

function line(key: string, text: string): RenderLine {
  return {
    key,
    role: "assistant",
    text,
    prefix: "lyra ",
    kind: "text",
    spans: [{ kind: "text", text }],
  };
}

const preview = line("external-preview", "external callback handoff");
const committed = line("external-committed", "external callback handoff");
const initial = streamingDisplayReducer(createStreamingDisplayState(), {
  type: "applyStreamUpdate",
  role: "assistant",
  committed: [],
  preview: [preview],
});
const callbacks = new EventEmitter();
const renderSnapshots: Array<{ history: string[]; dynamic: string[] }> = [];

function Harness() {
  const [state, dispatch] = useReducer(streamingDisplayReducer, initial);
  const [batcher] = useState(() => createStreamingDisplayBatcher(dispatch));
  const dynamic = orderedDynamicDisplayLines(state);
  renderSnapshots.push({
    history: state.historyLines.map((item) => item.text),
    dynamic: dynamic.map((item) => item.text),
  });

  useEffect(() => {
    const finalize = () => batcher.dispatch({
      type: "applyStreamUpdate",
      role: "assistant",
      committed: [committed],
      preview: [],
    });
    const burst = () => {
      batcher.enqueue({ type: "applyStreamUpdate", role: "assistant", committed: [line("burst-first", "burst first")], preview: [line("burst-preview", "burst final")] });
      batcher.enqueue({ type: "applyStreamUpdate", role: "assistant", committed: [line("burst-second", "burst second")], preview: [line("burst-preview", "burst final")] });
      batcher.dispatch({ type: "applyStreamUpdate", role: "assistant", committed: [line("burst-final", "burst final")], preview: [] });
    };
    callbacks.on("backend-finalize", finalize);
    callbacks.on("backend-burst", burst);
    return () => {
      callbacks.off("backend-finalize", finalize);
      callbacks.off("backend-burst", burst);
    };
  }, []);

  return (
    <ThemeProvider theme={THEMES.glitchcity}>
      <Box flexDirection="column" width={50}>
        <Static items={state.historyLines}>
          {(item) => <MessageLine key={item.key} line={item} runtimeMode="work" />}
        </Static>
        {dynamic.map((item) => <MessageLine key={item.key} line={item} runtimeMode="work" />)}
      </Box>
    </ThemeProvider>
  );
}

const output = new CaptureStream();
const instance = render(<Harness />, {
  stdout: output as unknown as NodeJS.WriteStream,
  debug: true,
  patchConsole: false,
  exitOnCtrlC: false,
});
await new Promise((resolve) => setTimeout(resolve, 10));
callbacks.emit("backend-finalize");
await new Promise((resolve) => setTimeout(resolve, 20));
const finalFrame = stripAnsi(output.chunks.at(-1) ?? "");

assert.equal(renderSnapshots.some((snapshot) =>
  snapshot.history.includes("external callback handoff")
  && snapshot.dynamic.includes("external callback handoff")
), false);
assert.deepEqual(renderSnapshots.at(-1), {
  history: ["external callback handoff"],
  dynamic: [],
});
assert.equal(finalFrame.match(/external callback handoff/gu)?.length, 1);

callbacks.emit("backend-burst");
await new Promise((resolve) => setTimeout(resolve, 20));
const burstFrame = stripAnsi(output.chunks.at(-1) ?? "");
instance.unmount();
assert.deepEqual(renderSnapshots.at(-1), {
  history: ["external callback handoff", "burst first", "burst second", "burst final"],
  dynamic: [],
});
for (const text of ["external callback handoff", "burst first", "burst second", "burst final"]) {
  assert.equal(burstFrame.split(text).length - 1, 1, `${text} renders once after burst`);
}
assert.equal(renderSnapshots.some(snapshot => snapshot.history.includes("burst final") && snapshot.dynamic.includes("burst final")), false);
console.log("external callback atomic handoff and batch cursor tests passed");

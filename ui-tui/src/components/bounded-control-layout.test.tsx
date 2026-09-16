import assert from "node:assert/strict";
import { PassThrough, Writable } from "node:stream";
import React from "react";
import stripAnsi from "strip-ansi";

// Set this before loading Ink: CI/debug modes skip its real erase/replay path.
process.env.CI = "0";
const { Box, Text, Static, render } = await import("ink");
const { StreamingControlLayout } = await import("./streaming-control-layout.js");
const { InputBar } = await import("./input-bar.js");
const { ActivityDock } = await import("./activity-dock.js");
const { AppHeader } = await import("./app-header.js");
const { ThemeProvider } = await import("../theme-context.js");
const { THEMES } = await import("../theme.js");
const { useTerminalSize } = await import("../terminal-size.js");
const { ToolApprovalPanel } = await import("./tool-approval-panel.js");
const { QuestionCard } = await import("./question-card.js");

class Input extends PassThrough {
  isTTY = true;
  setRawMode() {}
  ref() { return this; }
  unref() { return this; }
}
class Output extends Writable {
  isTTY = true;
  chunks: string[] = [];
  constructor(public columns: number, public rows: number) { super(); }
  _write(chunk: Buffer, _encoding: string, callback: () => void) {
    this.chunks.push(String(chunk)); callback();
  }
}
const settle = () => new Promise(resolve => setTimeout(resolve, 50));
for (const [cols, rows] of [[143, 36], [80, 24], [40, 12]]) {
  for (const mode of ["normal", "menu", "long", "expanded", "approval", "question"]) {
    const interaction = mode === "approval" || mode === "question";
    const input = new Input();
    const output = new Output(cols!, rows!);
    const width = cols! - 1;
    const submitted: string[] = [];
    const history = Array.from({ length: 100 }, (_, i) => `SAVED_${i}: neutral history`);
    const tools = Array.from({ length: 6 }, (_, i) => ({ id: `t${i}`, name: "read_file", arguments: "{}", startedAt: 0 }));
    function Fixture({ tick }: { tick: number }) {
      const { rows: height } = useTerminalSize(output as any);
      return <ThemeProvider theme={THEMES.glitchcity}>
      <Box flexDirection="column" width={width}>
        <Static items={history}>{(line, i) => <Text key={i}>{line}</Text>}</Static>
        <StreamingControlLayout rows={height}
          interactionActive={interaction}
          dynamic={Array.from({ length: 20 }, (_, i) => <Text key={i}>preview {i} {tick}</Text>)}
          header={<AppHeader columns={width} busy mode="work" />}
          status={<ActivityDock expanded={mode === "expanded"} tools={tools} now={tick * 1000}
            memory={{}} model="fixture" totalTokens={0} promptTokens={0} contextPct={5}
            status={`generating ${tick}`} showReasoning sessionName="fixture" columns={width} />}
          auxiliary={mode === "approval" ? <ToolApprovalPanel
            request={{ type: "tool_approval_request", request_id: "fixture", tool_name: "execute_shell",
              risk: "execute", kind: "host_execution", reason: "Verify output", agent_reason: "Verify a local fixture.",
              target: "echo fixture", scope: "exact command", choices: ["once", "session", "deny"] }}
            expanded width={width} queueIndex={1} queueTotal={1} offset={tick} pageSize={4} maxHeight={height - 2} />
            : mode === "question" ? <QuestionCard request={{ type: "user_question_request", request_id: `q-${tick}`,
              questions: [{ id: "q", question: "Choose an option " + "content ".repeat(30), multi_select: false,
                options: [{ label: "First" }, { label: "Second" }] }] }}
              active width={width} maxHeight={height - 2} onAnswer={() => {}} onCancel={() => {}} /> : null}
          input={<InputBar disabled={interaction} yolo sessionList={[]} modelList={[]} columns={width}
            onSubmit={({ text }) => submitted.push(text)} />}
        />
      </Box>
    </ThemeProvider>;
    }
    const view = (tick: number) => <Fixture tick={tick} />;
    const instance = render(view(0), { stdin: input as any, stdout: output as any,
      stderr: output as any, debug: false, patchConsole: false, exitOnCtrlC: false });
    try {
      await settle();
      assert.match(output.chunks.join(""), /SAVED_0:/);
      assert.doesNotMatch(output.chunks.join(""), /\u001b\[2J/, "first frame must be bounded");
      const initial = [...output.chunks].reverse().find(chunk => stripAnsi(chunk).trim()) ?? "";
      output.chunks = [];
      const draft = mode === "menu" ? "/" : mode === "long" ? "中".repeat(600) + "👩‍💻END" : "";
      if (draft) { input.write(draft); await settle(); }
      for (let tick = 1; tick <= 3; tick++) { instance.rerender(view(tick)); await settle(); }
      const data = output.chunks.join("");
      if (!interaction) assert.ok(data.length, "must exercise interactive output, not CI mode");
      assert.doesNotMatch(data, /\u001b\[2J|SAVED_0:/, `${cols}x${rows} ${mode} replayed history`);
      const last = stripAnsi([...output.chunks].reverse().find(chunk => stripAnsi(chunk).trim()) ?? initial);
      assert.match(last, interaction ? mode === "approval" ? /Ctrl\+Y/ : /Esc cancel/ : /order|you/,
        "active controls remain visible");
      if (mode === "normal") assert.match(last, /preview 19 3/, "preview keeps its newest lines");
      if (mode === "long") {
        assert.match(last, /END/);
        input.write("\u007f"); await settle();
        input.write("\r"); await settle();
        assert.deepEqual(submitted, [draft.slice(0, -1)], "viewport must not truncate the submitted draft");
      }
      output.chunks = [];
      // Shrinking the viewport must be safe on its first frame too.
      output.rows = 10;
      output.emit("resize"); await settle();
      assert.doesNotMatch(output.chunks.join(""), /\u001b\[2J|SAVED_0:/);
    } finally { instance.unmount(); instance.cleanup(); }
  }
}
console.log("bounded interactive layout tests passed");

import assert from "node:assert/strict";
import { Writable } from "node:stream";
import React from "react";
import { render } from "ink";
import stringWidth from "string-width";
import stripAnsi from "strip-ansi";
import type { ToolApprovalRequest } from "../types.js";
import { ThemeProvider } from "../theme-context.js";
import { THEMES } from "../theme.js";
import { ToolApprovalPanel } from "./tool-approval-panel.js";

class CaptureStream extends Writable {
  rows = 40;
  isTTY = true;
  chunks: string[] = [];

  constructor(public columns: number) {
    super();
  }

  _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void) {
    this.chunks.push(String(chunk));
    callback();
  }
}

const command = "npm --prefix ui-tui exec -- tsx --test src/审批面板测试.tsx && npm --prefix ui-tui run build";
const request: ToolApprovalRequest = {
  type: "tool_approval_request",
  request_id: "approval-render-1",
  tool_name: "execute_shell",
  risk: "execute",
  kind: "host_execution",
  reason: "仅允许执行精确命令；命令之外的宿主机能力没有被授权。",
  agent_reason: "需要运行审批面板测试并构建终端界面，以确认响应式改动没有破坏现有行为。",
  target: command,
  approval_effect: "运行测试并生成本地构建产物，不会发布或上传文件。",
  approval_boundary: "精确命令 · 仅本次",
  scope: "精确命令",
  session_scope_label: "这条精确命令",
  compound_commands: ["运行审批面板测试", "构建终端界面"],
  verifier: "sha256:approval-render-fixture",
  preview: "审计：命令由后端原样记录",
  choices: ["once", "session", "deny"],
};

async function capture(
  columns: number,
  expanded: boolean,
  choices = request.choices,
  panelRequest: ToolApprovalRequest = request,
): Promise<string> {
  const output = new CaptureStream(columns);
  const instance = render(
    <ThemeProvider theme={THEMES.glitchcity}>
      <ToolApprovalPanel
        request={{ ...panelRequest, choices }}
        expanded={expanded}
        width={columns}
        queueIndex={1}
        queueTotal={2}
        offset={0}
        pageSize={40}
      />
    </ThemeProvider>,
    {
      stdout: output as unknown as NodeJS.WriteStream,
      debug: true,
      patchConsole: false,
      exitOnCtrlC: false,
    },
  );
  await new Promise((resolve) => setTimeout(resolve, 20));
  instance.unmount();
  return stripAnsi(output.chunks[0] ?? "").trimEnd();
}

for (const columns of [60, 90, 130]) {
  const collapsed = await capture(columns, false);
  const collapsedLines = collapsed.split(/\r?\n/);

  assert.equal(
    collapsedLines.every((line) => stringWidth(line) <= columns),
    true,
    `collapsed panel overflowed at ${columns} columns`,
  );
  assert.match(collapsedLines[0], /^╭.*╮$/, `expected one rounded warning frame at ${columns} columns`);
  assert.doesNotMatch(collapsed, /[╔╗╚╝║═]/, `double border remained at ${columns} columns`);
  assert.match(collapsed, /◆ 审批请求/);
  assert.match(collapsed, /1\/2/);
  assert.match(collapsed, /执行 Shell 命令/);
  assert.match(collapsed, /宿主机/);

  const explanationIndex = collapsedLines.findIndex((line) => line.includes("模型说明"));
  const factsIndex = collapsedLines.findIndex((line) => line.includes("风险："));
  const commandLines = collapsedLines.filter((line) => line.includes("命令："));
  assert.ok(explanationIndex > 0 && explanationIndex < factsIndex, "model explanation must lead backend facts");
  assert.match(collapsed, /风险：执行/);
  assert.match(collapsed, /位置：宿主机/);
  assert.match(collapsed, /范围：精确命令/);
  assert.match(collapsed, /操作数：2 项/);
  assert.equal(commandLines.length, 1, `raw command preview wrapped at ${columns} columns`);
  assert.ok(stringWidth(commandLines[0]) > 20, `CJK command preview collapsed vertically at ${columns} columns`);
  assert.doesNotMatch(collapsed, /\[A\]/);
  assert.match(collapsed, /\[V\] 完整详情/);
  assert.ok(collapsed.indexOf("[Y/回车]") < collapsed.indexOf("[V] 完整详情"));
  assert.ok(collapsed.indexOf("[V] 完整详情") < collapsed.indexOf("[N/Esc]"));

  const expanded = await capture(columns, true);
  assert.equal(
    expanded.split(/\r?\n/).every((line) => stringWidth(line) <= columns),
    true,
    `expanded panel overflowed at ${columns} columns`,
  );
  assert.match(expanded, /模型说明/);
  assert.match(expanded, /系统事实/);
  assert.match(expanded, /边界：精确命令 · 仅本次/);
  assert.match(expanded, /影响：运行测试并生成本地构建产物/);
  assert.match(expanded, /sha256:approval-render-fixture/);
  assert.match(expanded, /审计：命令由后端原样记录/);
  assert.match(expanded, /\[A\] 会话内允许 这条精确命令/);
  assert.ok(
    expanded.replace(/[\s│╭╮╰╯]/g, "").includes(command.replace(/\s/g, "")),
    `expanded panel omitted part of the raw command at ${columns} columns`,
  );

  if (process.env.SHOW_APPROVAL_PREVIEW === "1") {
    process.stdout.write(`\n=== approval collapsed · ${columns} columns ===\n${collapsed}\n`);
    process.stdout.write(`\n=== approval expanded · ${columns} columns ===\n${expanded}\n`);
  }
}

const expandedWithoutSession = await capture(90, true, ["once", "deny"]);
assert.doesNotMatch(expandedWithoutSession, /\[A\]/);

const controlRequest: ToolApprovalRequest = {
  ...request,
  approval_title: "检查\u0085标题\u{1D173}",
  agent_reason: "\u202e\u200b\u{E0020}\u{1BCA0}",
  approval_question: "Backend \x1b[31m fallback",
  reason: "system\x00fact",
  target: "printf first\nprintf second\u2066\u{E0020}\u{1BCA3}",
  arguments: { command: "printf first\nprintf second\u2066\u{E0020}\u{1BCA3}" },
};
const controlCollapsed = await capture(130, false, controlRequest.choices, controlRequest);
const controlExpanded = await capture(130, true, controlRequest.choices, controlRequest);

assert.match(controlCollapsed, /系统说明：Backend \\x1b\[31m fallback/);
assert.match(
  controlCollapsed,
  /命令：printf first\\nprintf second\\u\{2066\}\\u\{E0020\}\\u\{1BCA3\}/,
);
assert.match(controlExpanded, /检查\\x85标题\\u\{1D173\}/);
assert.match(controlExpanded, /system\\x00fact/);
assert.match(
  controlExpanded,
  /printf first\\nprintf second\\u\{2066\}\\u\{E0020\}\\u\{1BCA3\}/,
);
for (const rendered of [controlCollapsed, controlExpanded]) {
  assert.doesNotMatch(
    rendered,
    /[\u0000\u0085\u200b\u202e\u2066\u{1D173}\u{E0020}\u{1BCA0}\u{1BCA3}]/u,
  );
}

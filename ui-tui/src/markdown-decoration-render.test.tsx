import assert from "node:assert/strict";
import { Writable } from "node:stream";
import React from "react";
import stringWidth from "string-width";
import stripAnsi from "strip-ansi";
import { THEMES, THEME_NAMES, type ThemeName } from "./theme.js";
import { ThemeProvider } from "./theme-context.js";

process.env.FORCE_COLOR = "3";
const { Box, render } = await import("ink");
const {
  MESSAGE_TIME_RAIL_WIDTH,
  MessageLine,
  messageRolePrefixWidth,
  messageToLines,
} = await import("./app.js");

class CaptureStream extends Writable {
  rows = 18;
  isTTY = true;
  chunks: string[] = [];

  constructor(public columns: number) {
    super();
  }

  getColorDepth(): number {
    return 24;
  }

  _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void) {
    this.chunks.push(String(chunk));
    callback();
  }
}

async function capture(themeName: ThemeName): Promise<{ plain: string; raw: string }> {
  const columns = 40;
  const lines = messageToLines(
    {
      role: "assistant",
      content: [
        "prefix `alpha` + `beta` + ~~retired~~ + $\\boxed{broken$ suffix wraps here",
        "颜文字 (´;ω;`) 后续 **69.8 tok/s**",
        "$$",
        "\\boxed{also broken",
        "$$",
      ].join("\n"),
    },
    columns,
    true,
    "work",
    {
      label: "09:44",
      kind: "absolute",
      width: MESSAGE_TIME_RAIL_WIDTH,
      prefixWidth: messageRolePrefixWidth("assistant", "work"),
    },
  );
  const output = new CaptureStream(columns);
  const instance = render(
    <ThemeProvider theme={THEMES[themeName]}>
      <Box flexDirection="column" width={columns}>
        {lines.map((line) => <MessageLine key={line.key} line={line} runtimeMode="work" />)}
      </Box>
    </ThemeProvider>,
    {
      stdout: output as unknown as NodeJS.WriteStream,
      debug: true,
      patchConsole: false,
      exitOnCtrlC: false,
    },
  );
  await new Promise((resolve) => setTimeout(resolve, 10));
  const frame = output.chunks
    .map((raw) => ({ raw, plain: stripAnsi(raw) }))
    .filter(({ plain }) => plain.trim())
    .at(-1);
  const raw = frame?.raw.trimEnd() ?? "";
  const plain = frame?.plain.trimEnd() ?? "";
  instance.unmount();
  return { plain, raw };
}

for (const themeName of THEME_NAMES) {
  const { plain, raw } = await capture(themeName);
  assert.equal(
    plain.split(/\r?\n/).every((line) => stringWidth(line) <= 40),
    true,
    `${themeName}: ${plain}`,
  );
  assert.ok((plain.match(/⚠ /gu)?.length ?? 0) >= 2, `${themeName} warning decorations: ${JSON.stringify(plain)}`);
  assert.match(plain, /alpha/u);
  assert.match(plain, /beta/u);
  assert.match(plain, /retired/u);
  assert.match(plain, /颜文字 \(´;ω;`\) 后/u);
  assert.match(plain, /续 69\.8 tok\/s/u);
  assert.equal(plain.includes("**"), false);
  assert.equal(plain.includes("~~"), false);
  assert.match(raw, /\x1b\[9m/u, `${themeName} strikethrough style missing`);
}

console.log("cross-theme markdown decoration render tests passed");

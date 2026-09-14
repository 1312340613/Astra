import assert from "node:assert/strict";
import React from "react";
import { Writable } from "node:stream";
import { render } from "ink";
import stripAnsi from "strip-ansi";
import { ThemeProvider } from "../theme-context.js";
import { THEMES } from "../theme.js";
import { formatLatexForTerminal } from "../markdown-math.js";
import { MarkdownRenderer } from "./markdown.js";

const fraction = formatLatexForTerminal(
  String.raw`c_{\text{rest}} = \frac{1}{c_{\text{intensity}}} \Rightarrow \text{失眠}`,
);
assert.equal(fraction.malformed, false);
assert.equal(fraction.text, "cᵣₑₛₜ = (1)/(c_(intensity)) ⇒ 失眠");

const broken = formatLatexForTerminal(String.raw`\boxed{\text{失眠}\gg)`);
assert.equal(broken.malformed, true);
assert.ok(broken.text.includes("失眠"));

class TestStdout extends Writable {
  columns = 100;
  rows = 30;
  isTTY = true;
  chunks: string[] = [];

  _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void) {
    this.chunks.push(String(chunk));
    callback();
  }
}

const stdout = new TestStdout();
const instance = render(
  <ThemeProvider theme={THEMES.classic}>
    <MarkdownRenderer text={String.raw`公式如下：

$$
c_{\text{rest}} = \frac{1}{x} \Rightarrow \text{休息}
$$

行内公式 $x^2 \neq 0$。

$$
\boxed{\text{失眠}\gg)
$$`} />
  </ThemeProvider>,
  {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stdout as unknown as NodeJS.WriteStream,
    // Ink otherwise writes non-static CI output only when the app unmounts.
    debug: true,
    patchConsole: false,
    exitOnCtrlC: false,
  },
);

await new Promise((resolve) => setTimeout(resolve, 30));
const output = stripAnsi(stdout.chunks.join(""));
assert.ok(output.includes("cᵣₑₛₜ = (1)/(x) ⇒ 休息"), output);
assert.ok(output.includes("x² ≠ 0"), output);
assert.ok(output.includes("⚠"), output);
assert.equal(output.includes("$$"), false, output);
instance.unmount();

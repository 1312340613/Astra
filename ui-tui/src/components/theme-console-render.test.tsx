import assert from "node:assert/strict";
import { Writable } from "node:stream";
import React from "react";
import { render } from "ink";
import stringWidth from "string-width";
import stripAnsi from "strip-ansi";
import { ThemeProvider } from "../theme-context.js";
import { THEMES, THEME_NAMES } from "../theme.js";
import { AppHeader } from "./app-header.js";
import { StartupScreen } from "./startup-screen.js";
import { WelcomeScreen } from "./welcome-screen.js";

class CaptureStream extends Writable {
  isTTY = true;
  chunks: string[] = [];

  constructor(public columns: number, public rows: number) {
    super();
  }

  _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void) {
    this.chunks.push(String(chunk));
    callback();
  }
}

const info = {
  skills: 15,
  tools: 72,
  model: "deepseek-v4-pro",
  learning: { mode: "review" as const, auto: true, pending: 0 },
  mcp: [{ name: "local", state: "ready" }],
};

async function capture(element: React.ReactElement, columns: number, rows: number): Promise<string> {
  const output = new CaptureStream(columns, rows);
  const instance = render(element, {
    stdout: output as unknown as NodeJS.WriteStream,
    debug: true,
    patchConsole: false,
    exitOnCtrlC: false,
  });
  await new Promise((resolve) => setTimeout(resolve, 10));
  instance.unmount();
  return stripAnsi(output.chunks[0] ?? "").trimEnd();
}

for (const themeName of THEME_NAMES) {
  const theme = THEMES[themeName];
  for (const columns of [60, 90, 130]) {
    for (const rows of [30, 40]) {
      const startup = await capture(
        <ThemeProvider theme={theme}>
          <StartupScreen columns={columns} rows={rows} info={info} animate={false} />
        </ThemeProvider>,
        columns,
        rows,
      );
      const welcome = await capture(
        <ThemeProvider theme={theme}>
          <WelcomeScreen
            columns={columns}
            rows={rows}
            info={info}
            model={info.model}
            sessionName="session_test"
          />
        </ThemeProvider>,
        columns,
        rows,
      );
      const header = await capture(
        <ThemeProvider theme={theme}>
          <AppHeader columns={columns} busy={false} />
        </ThemeProvider>,
        columns,
        rows,
      );

      assert.match(startup, new RegExp(theme.console.bootTitle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
      const consoleTitle = new RegExp(theme.console.consoleTitle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
      // Approved compact Lyra layout reserves the short terminal for her portrait
      // and runtime information. Tall cards and narrow text-only cards keep titles.
      if (themeName === "lyra" && rows === 30 && columns >= 90) {
        assert.doesNotMatch(welcome, consoleTitle);
        assert.match(welcome, /[▀▄█]/, "compact Lyra card must retain its pixel portrait");
      } else {
        assert.match(welcome, consoleTitle);
      }
      assert.ok(welcome.includes(theme.console.runtimeTitle));
      assert.ok(welcome.includes(info.model));
      assert.match(header, new RegExp((theme.chrome?.brand ?? "").slice(0, 8).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
      assert.equal(
        startup.split(/\r?\n/).every((line) => stringWidth(line) <= columns),
        true,
        `${themeName} startup overflowed at ${columns} columns`,
      );
      assert.equal(
        welcome.split(/\r?\n/).every((line) => stringWidth(line) <= columns),
        true,
        `${themeName} welcome overflowed at ${columns} columns`,
      );
      assert.equal(
        header.split(/\r?\n/).every((line) => stringWidth(line) <= columns),
        true,
        `${themeName} header overflowed at ${columns} columns`,
      );
      if (columns === 130 && process.env.SHOW_THEME_CONSOLES === "1") {
        process.stdout.write(`\n=== ${themeName} · startup · ${rows} rows ===\n${startup}\n`);
        process.stdout.write(`\n=== ${themeName} · welcome · ${rows} rows ===\n${welcome}\n`);
      }
    }
  }
}

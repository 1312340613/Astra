import assert from "node:assert/strict";
import { Writable } from "node:stream";
import React from "react";
import { render } from "ink";
import stripAnsi from "strip-ansi";
import { ThemeProvider } from "../theme-context.js";
import { THEMES } from "../theme.js";
import { MinimalStatusDock } from "./minimal-chrome.js";

class CaptureStream extends Writable {
  rows = 8;
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

async function capture(active: boolean, withStats = false): Promise<string> {
  const output = new CaptureStream(100);
  const statsProps = withStats
    ? {
        cacheHitTokens: 800,
        cacheMissTokens: 200,
        contextUsed: 12000,
        contextLimit: 128000,
        contextPct: 9,
      }
    : {};
  const instance = render(
    <ThemeProvider theme={THEMES.glitchcity}>
      <MinimalStatusDock
        session="minimal_20260815_172728"
        busy={active}
        disconnected={false}
        tools={active ? [{ id: "1", name: "execute_shell", startedAt: 1_000 }] : []}
        now={2_250}
        columns={100}
        {...statsProps}
      />
    </ThemeProvider>,
    {
      stdout: output as unknown as NodeJS.WriteStream,
      debug: true,
      patchConsole: false,
      exitOnCtrlC: false,
    },
  );
  await new Promise((resolve) => setTimeout(resolve, 10));
  instance.unmount();
  return stripAnsi(output.chunks[0] ?? "").trimEnd();
}

const idle = await capture(false);
assert.equal(idle.split(/\r?\n/).length, 3, "idle Minimal dock must use one content row");
assert.match(idle, /minimal_20260815_172728/);
assert.doesNotMatch(idle, /ZERO INJECTION|no injected memory|bash · str_replace_editor/);

const active = await capture(true);
assert.equal(active.split(/\r?\n/).length, 3, "active Minimal dock must use one content row");
assert.match(active, /execute_shell/);



const withStats = await capture(false, true);
assert.match(withStats, /cache 80%/);
assert.match(withStats, /ctx 12,000\/128,000 9%/);

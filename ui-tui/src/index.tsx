// Entry point — render Ink App to stderr. Backend IPC uses a child-process pipe,
// so the Node process stdout is not part of the JSON protocol.
import React from "react";
import { render } from "ink";
import App from "./app.js";

const outputStream = process.stderr;
const bracketedPasteOn = "\u001b[?2004h";
const bracketedPasteOff = "\u001b[?2004l";

// Ensure the output stream reports terminal size. Ink/Yoga rely on these values,
// and Windows can leave stderr without columns/rows even when stdout has them.
if (!outputStream.columns && process.stdout.columns) {
  Object.defineProperty(outputStream, "columns", {
    value: process.stdout.columns,
    writable: false,
    configurable: true,
  });
}
if (!outputStream.rows && process.stdout.rows) {
  Object.defineProperty(outputStream, "rows", {
    value: process.stdout.rows,
    writable: false,
    configurable: true,
  });
}

// Tell Windows Terminal that this full-screen input safely handles multiline
// paste as one value. The terminal then avoids the shell-oriented warning and
// wraps pasted data in markers that paste-command.ts strips before storage.
if (process.stdin.isTTY && outputStream.isTTY) {
  outputStream.write(bracketedPasteOn);
}

const { waitUntilExit } = render(<App />, {
  stdout: outputStream as any,
  patchConsole: false,
  // App owns Ctrl+C: first press cancels an active durable task, a second
  // press (or an idle press) exits. Ink's default would consume it first.
  exitOnCtrlC: false,
});

// Keep alive
waitUntilExit().finally(() => {
  if (process.stdin.isTTY && outputStream.isTTY) {
    outputStream.write(bracketedPasteOff);
  }
  process.exit(0);
});

// Owned subprocess fixture: never opens Terminal.app or a model connection.
import { spawn } from "node:child_process";
import { installTerminalOutput } from "../terminal-output.js";
import { TuiLifecycle } from "../tui-lifecycle.js";

const mode = process.argv[2];
const report = (value: object) => process.stdout.write(JSON.stringify(value) + "\n");
const { output } = installTerminalOutput(process.stderr, { stallMs: mode === "recover" ? 4000 : 350 });
const backend = spawn(process.execPath, ["-e", `
  const fs = require('node:fs'); let data = '';
  process.stdin.on('data', chunk => { data += chunk;
    if (!data.includes('\\n')) return;
    const command = JSON.parse(data.trim());
    process.stdout.write('x'.repeat(1024 * 1024), () => {
      fs.writeFileSync(process.argv[1], JSON.stringify(command)); process.exit(0);
    });
  });
`, process.argv[3]!], { stdio: ["pipe", "pipe", "pipe"] });
backend.stdout.resume(); backend.stderr.resume();
const lifecycle = new TuiLifecycle(async (reason, result) => {
  clearInterval(heartbeat);
  const drained = await output.close(500);
  report({ type: "closed", reason, ...result, drained, ...output.metrics, code: output.fault?.code });
  process.exit(0);
});
lifecycle.attachBackend(backend);
output.on("fault", error => { report({ type: "fault", code: error.code }); void lifecycle.requestExit("terminal_output_failure"); });
process.stderr.on("resize", () => report({ type: "resize", rows: process.stderr.rows, columns: process.stderr.columns }));
process.stdin.setEncoding("utf8");
process.stdin.on("data", data => {
  if (String(data).includes("ping")) report({ type: "pong" });
  if (String(data).includes("close")) void lifecycle.requestExit("user_exit");
});
const heartbeat = setInterval(() => report({ type: "heartbeat" }), 40);
report({ type: "ready" });
// Global stderr writes (including cursor sequences) must use the same queue.
process.stderr.write("\u001b[?25l");
process.stderr.write(Buffer.alloc(1024 * 1024, 97), error => report({ type: "written", ok: !error }));

import { spawn } from "node:child_process";

// Read only: never activate Terminal, change tabs, or evaluate shell input.
const selectedTTY = `if application id "com.apple.Terminal" is not running then return ""
tell application id "com.apple.Terminal"
  if not frontmost then return ""
  if (count of windows) is 0 then return ""
  return tty of selected tab of front window
end tell`;

// Separate OS session: probes must not become the terminal's foreground job.
// Pipes, timeout and AbortSignal remain owned by the TUI; do not unref the child.
export function runFocusProbe(file: string, args: string[], signal: AbortSignal): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(file, args, {
      detached: true, stdio: ["ignore", "pipe", "ignore"], timeout: 1500, signal,
    });
    const chunks: Buffer[] = [];
    let bytes = 0;
    child.stdout.on("data", (chunk: Buffer) => {
      bytes += chunk.length;
      if (bytes > 2048) {
        child.kill();
        reject(new Error("focus_probe_output_limit"));
      } else chunks.push(chunk);
    });
    child.once("error", reject);
    child.once("close", (code) => {
      if (code === 0 && bytes <= 2048) resolve(Buffer.concat(chunks).toString("utf8").trim());
      else reject(new Error("focus_probe_failed"));
    });
  });
}

export function normalizeTTY(value: string): string | undefined {
  const name = value.trim().replace(/^\/dev\//, "");
  return /^ttys[0-9]+$/.test(name) ? "/dev/" + name : undefined;
}

export class AppshotFocusTracker {
  private focused = false;
  constructor(private tty: string, private onFocus: () => void) {}
  observe(selected: string) {
    const focused = normalizeTTY(selected) === this.tty;
    if (focused && !this.focused) this.onFocus();
    this.focused = focused;
  }
}

/** One bounded probe at a time. A failed probe cannot claim a recipient. */
export function startAppshotFocusMonitor(onFocus: () => void): () => void {
  if (process.platform !== "darwin" || process.env.TERM_PROGRAM !== "Apple_Terminal"
      || !process.stdin.isTTY) return () => {};
  const abort = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  void (async () => {
    try {
      // Derive the owning TTY from the process, not inherited shell variables.
      const tty = normalizeTTY(await runFocusProbe("/bin/ps", ["-p", String(process.pid), "-o", "tty="], abort.signal));
      if (!tty || abort.signal.aborted) return;
      const tracker = new AppshotFocusTracker(tty, onFocus);
      let failures = 0;
      const poll = async () => {
        try {
          const selected = await runFocusProbe("/usr/bin/osascript", ["-e", selectedTTY], abort.signal);
          if (abort.signal.aborted) return;
          tracker.observe(selected);
          failures = 0;
        } catch {
          // Preserve last successful focus state across transient failures:
          // otherwise recovery could steal a newer recipient.
          failures++;
        }
        if (!abort.signal.aborted && failures < 3) {
          timer = setTimeout(poll, 400);
          timer.unref();
        }
      };
      await poll();
    } catch { /* Noninteractive/permission failure: existing keyboard path remains. */ }
  })();
  return () => { abort.abort(); clearTimeout(timer); };
}

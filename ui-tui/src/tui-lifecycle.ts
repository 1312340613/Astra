import type { ChildProcess } from "node:child_process";

export type ExitReason = "user_exit" | "terminal_output_failure" | "signal" | "ui_exit";
export type ShutdownResult = { forced: boolean; exited: boolean };

/** Only the backend created by this TUI is owned here. Pipes remain drained by
 * App until it exits, so cancellation/persistence cannot wait on terminal I/O. */
export async function stopBackend(proc: ChildProcess | undefined, reason: ExitReason,
  graceMs = 10000, killMs = 500): Promise<ShutdownResult> {
  if (!proc || proc.exitCode !== null || proc.signalCode !== null) return { forced: false, exited: true };
  let exited = false;
  let notify = () => {};
  const onExit = () => { exited = true; notify(); };
  proc.once("exit", onExit); proc.once("error", onExit);
  const wait = async (ms: number) => {
    if (exited) return;
    await new Promise<void>(resolve => {
      const timer = setTimeout(() => { notify = () => {}; resolve(); }, ms);
      notify = () => { clearTimeout(timer); resolve(); };
    });
  };
  let forced = false;
  try {
    if (proc.stdin && !proc.stdin.destroyed && !proc.stdin.writableEnded) {
      try { proc.stdin.end(JSON.stringify({ type: "exit", reason }) + "\n"); }
      catch { /* A closed command pipe still uses the bounded process wait. */ }
    }
    await wait(graceMs);
    if (!exited) { forced = true; proc.kill("SIGTERM"); await wait(killMs); }
    if (!exited) { proc.kill("SIGKILL"); await wait(killMs); }
    return { forced, exited };
  } finally { proc.off("exit", onExit); proc.off("error", onExit); }
}

export class TuiLifecycle {
  closing = false;
  private backend: ChildProcess | undefined;
  private exitPromise: Promise<void> | undefined;
  constructor(private readonly finish: (reason: ExitReason, result: ShutdownResult) => Promise<void>) {}
  attachBackend(backend: ChildProcess) { this.backend = backend; }
  requestExit(reason: ExitReason) {
    if (!this.exitPromise) {
      this.closing = true;
      this.exitPromise = stopBackend(this.backend, reason).then(result => this.finish(reason, result));
    }
    return this.exitPromise;
  }
}

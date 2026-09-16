import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { Writable } from "node:stream";

export type AsyncWrite = (buffer: Buffer, offset: number, length: number,
  callback: (error: NodeJS.ErrnoException | null, written: number) => void) => void;
type Options = { stallMs?: number; maxPendingBytes?: number; highWaterMark?: number };

// fs.write in libuv's thread pool leaves process.exit waiting for a blocked
// syscall. An owned writer process can be killed without waiting for that
// syscall, while the TUI keeps reading backend events and requesting save.
// Length framing makes partial writes explicit: the parent resends only the
// unwritten suffix, never an ambiguously delivered terminal frame.
const writerSource = String.raw`
const fs = require('node:fs');
let pending = Buffer.alloc(0);
let busy = false;
const ack = value => fs.writeSync(3, JSON.stringify(value) + '\n');
function pump() {
  if (!busy && pending.length >= 4) {
    const length = pending.readUInt32BE(0);
    if (length > 16384) process.exit(2);
    if (pending.length < length + 4) return;
    const data = pending.subarray(4, length + 4);
    pending = pending.subarray(length + 4);
    busy = true;
    function attempt() {
      try { ack({ written: fs.writeSync(2, data) }); busy = false; pump(); }
      catch (error) {
        if (error.code === 'EAGAIN' || error.code === 'EINTR') { setTimeout(attempt, 5); return; }
        ack({ written: 0, code: error.code || 'EIO' }); process.exit(1);
      }
    }
    attempt();
  }
}
process.stdin.on('data', chunk => {
  pending = Buffer.concat([pending, chunk]); pump();
});
process.stdin.on('end', () => process.exit(0));
`;

function createWriterProcess(fd: number) {
  const child = spawn(process.execPath, ["-e", writerSource], {
    stdio: ["pipe", "ignore", fd, "pipe"], windowsHide: true,
  });
  let pending: Parameters<AsyncWrite>[3] | undefined;
  let failure: NodeJS.ErrnoException | undefined;
  const fail = (error: NodeJS.ErrnoException) => {
    failure = error;
    const callback = pending; pending = undefined;
    callback?.(error, 0);
  };
  const ack = createInterface({ input: child.stdio[3] as NodeJS.ReadableStream });
  ack.on("line", line => {
    try {
      const result = JSON.parse(line);
      const callback = pending; pending = undefined;
      callback?.(result.code ? Object.assign(new Error("Terminal writer failed"), { code: result.code }) : null, result.written);
    } catch { fail(Object.assign(new Error("Invalid terminal writer response"), { code: "EIO" })); }
  });
  child.on("error", fail);
  child.stdin!.on("error", fail);
  const kill = () => { if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL"); };
  process.once("exit", kill);
  child.once("exit", () => {
    process.off("exit", kill);
    ack.close();
    fail(Object.assign(new Error("Terminal writer exited"), { code: "EPIPE" }));
  });
  const write: AsyncWrite = (buffer, offset, length, callback) => {
    if (failure) { callback(failure, 0); return; }
    pending = callback;
    const packet = Buffer.allocUnsafe(4 + length);
    packet.writeUInt32BE(length);
    buffer.copy(packet, 4, offset, offset + length);
    child.stdin!.write(packet);
  };
  return { write, kill, end: () => child.stdin!.end() };
}

/** One bounded byte stream. A failed ANSI stream cannot resume mid-frame. */
export class TerminalOutput extends Writable {
  fault: NodeJS.ErrnoException | undefined;
  readonly metrics = { bytesWritten: 0, peakPendingBytes: 0, fullClearRequests: 0, maxWriteMs: 0 };
  private readonly stallMs: number;
  private readonly maxPendingBytes: number;
  private lastProgress = 0;
  private watchdog: ReturnType<typeof setInterval> | undefined;

  constructor(private readonly writeAsync: AsyncWrite, options: Options = {}) {
    super({ highWaterMark: options.highWaterMark ?? 65536 });
    this.stallMs = options.stallMs ?? 5000;
    this.maxPendingBytes = options.maxPendingBytes ?? 4 * 1024 * 1024;
    this.on("error", error => this.fail(error));
  }

  override write(chunk: string | Uint8Array, encoding?: BufferEncoding | ((error?: Error | null) => void),
    callback?: (error?: Error | null) => void): boolean {
    const cb = typeof encoding === "function" ? encoding : callback;
    const bytes = typeof chunk === "string" ? Buffer.byteLength(chunk, typeof encoding === "string" ? encoding : undefined) : chunk.byteLength;
    if (!this.fault && this.writableLength + bytes > this.maxPendingBytes) {
      this.fail(Object.assign(new Error("Terminal output backlog exceeded its limit"), { code: "ENOBUFS" }));
    }
    if (this.fault || this.writableEnded || this.destroyed) {
      if (cb) queueMicrotask(() => cb(this.fault ?? new Error("Terminal output closed")));
      return false;
    }
    this.metrics.peakPendingBytes = Math.max(this.metrics.peakPendingBytes, this.writableLength + bytes);
    if (bytes && !this.watchdog) {
      this.lastProgress = performance.now();
      this.watchdog = setInterval(() => {
        if (this.writableLength && performance.now() - this.lastProgress >= this.stallMs) {
          this.fail(Object.assign(new Error("Terminal output stopped making progress"), { code: "ETIMEDOUT" }));
        }
      }, Math.max(5, Math.min(100, this.stallMs / 2)));
      this.watchdog.unref();
    }
    return super.write(chunk, typeof encoding === "string" ? encoding : "utf8", cb);
  }

  override _write(buffer: Buffer, _encoding: BufferEncoding, done: (error?: Error | null) => void) {
    let offset = 0;
    if (buffer.includes(Buffer.from("\u001b[2J"))) this.metrics.fullClearRequests++;
    const next = () => {
      if (this.fault) { done(this.fault); return; }
      if (offset === buffer.length) {
        done();
        queueMicrotask(() => { if (!this.writableLength) this.stopWatchdog(); });
        return;
      }
      const started = performance.now();
      const length = Math.min(16384, buffer.length - offset);
      const completed = (error: NodeJS.ErrnoException | null, written: number) => {
        if (this.fault) { done(this.fault); return; }
        if (error || !Number.isInteger(written) || written <= 0 || written > length) {
          const failure = error ?? Object.assign(new Error("Invalid terminal write result"), { code: "EIO" });
          this.fail(failure); done(failure); return;
        }
        offset += written;
        this.lastProgress = performance.now();
        this.metrics.bytesWritten += written;
        this.metrics.maxWriteMs = Math.max(this.metrics.maxWriteMs, this.lastProgress - started);
        // Yield even if an adapter completes synchronously.
        setImmediate(next);
      };
      try { this.writeAsync(buffer, offset, length, completed); }
      catch (error) { completed(error as NodeJS.ErrnoException, 0); }
    };
    next();
  }

  private stopWatchdog() { clearInterval(this.watchdog); this.watchdog = undefined; }

  private fail(error: NodeJS.ErrnoException) {
    if (this.fault) return;
    this.fault = error;
    this.stopWatchdog();
    this.destroy(error);
    // Never unmount Ink from inside its own write/React commit.
    queueMicrotask(() => this.emit("fault", error));
  }

  async close(timeoutMs = 1000): Promise<boolean> {
    if (this.fault) return false;
    if (this.writableFinished) return true;
    return new Promise(resolve => {
      const finish = () => {
        clearTimeout(timer);
        this.off("finish", finish); this.off("close", finish);
        this.stopWatchdog();
        resolve(!this.fault && this.writableFinished);
      };
      const timer = setTimeout(() => this.fail(Object.assign(new Error("Terminal output drain timed out"), { code: "ETIMEDOUT" })), timeoutMs);
      this.once("finish", finish); this.once("close", finish);
      this.end();
    });
  }
}

/** Keep the original TTY identity, dimensions, resize and color APIs for Ink.
 * Redirect global writes too: Ink 5 cli-cursor bypasses its stdout option.
 * The returned restore is for tests/embedding; keep the guard until process
 * exit in the CLI so signal-exit cursor cleanup cannot hit a broken sync TTY.
 */
export function installTerminalOutput(target: NodeJS.WriteStream, options: Options = {},
  additional: NodeJS.WriteStream[] = []) {
  const originalWrite = target.write.bind(target);
  const fd = (target as NodeJS.WriteStream & { fd?: number }).fd;
  const writer = typeof fd === "number" ? createWriterProcess(fd) : undefined;
  const asyncWrite: AsyncWrite = writer ? writer.write
    : (buffer, offset, length, callback) => { originalWrite(buffer.subarray(offset, offset + length), error => callback(error ?? null, error ? 0 : length)); };
  const output = new TerminalOutput(asyncWrite, options);
  output.once("fault", () => writer?.kill());
  output.once("finish", () => writer?.end());
  const restores: Array<() => void> = [];
  for (const stream of new Set([target, ...additional])) {
    const previousWrite = stream.write;
    const previousLength = Object.getOwnPropertyDescriptor(stream, "writableLength");
    const redirect = output.write.bind(output) as typeof stream.write;
    stream.write = redirect;
    Object.defineProperty(stream, "writableLength", { configurable: true, get: () => output.writableLength });
    const drain = () => stream.emit("drain");
    output.on("drain", drain);
    restores.push(() => {
      output.off("drain", drain);
      if (stream.write === redirect) stream.write = previousWrite;
      if (previousLength) Object.defineProperty(stream, "writableLength", previousLength);
      else delete (stream as any).writableLength;
    });
  }
  return { output, restore: () => restores.forEach(restore => restore()) };
}

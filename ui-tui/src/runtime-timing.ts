import type { PyEvent } from "./types.js";

type TracedEvent = PyEvent & { performance_trace_id?: string; performance_trace_ids?: string[] };
type Sample = {
  trace_id: string;
  parse_ms: number;
  batch_ms: number;
  handle_ms: number;
  react_commit_ms: number;
  receive_to_commit_ms: number;
};
type Pending = { received: number; parse: number; handled?: number; completed?: number; committed?: number };

export function eventTraceIds(event: PyEvent): string[] {
  const traced = event as TracedEvent;
  const ids = traced.performance_trace_ids ?? [traced.performance_trace_id];
  return Array.isArray(ids) ? ids.slice(0, 128).filter((id): id is string =>
    typeof id === "string" && /^[a-f0-9]{32}$/.test(id)) : [];
}

/** Local monotonic durations only. A React commit does not prove terminal paint. */
export class RuntimeTiming {
  private pending = new Map<string, Pending>();
  dropped = 0;

  constructor(
    private readonly send: (samples: Sample[]) => void,
    private readonly now: () => number = () => performance.now(),
  ) {}

  received(event: PyEvent, parseMs = 0): void {
    const ids = eventTraceIds(event);
    if (!ids.length) return;
    const now = this.now();
    for (const [id, sample] of this.pending) {
      if (now - sample.received > 60_000) { this.pending.delete(id); this.dropped++; }
    }
    for (const id of ids) {
      if (this.pending.has(id)) continue;
      if (this.pending.size >= 2048) { this.dropped++; continue; }
      const parse = Number.isFinite(parseMs) ? Math.max(0, parseMs) : 0;
      this.pending.set(id, { received: now - parse, parse });
    }
  }

  handle(event: PyEvent, apply: () => void): void {
    const ids = eventTraceIds(event).filter(id => this.pending.has(id));
    if (!ids.length) { apply(); return; }
    const started = this.now();
    for (const id of ids) this.pending.get(id)!.handled = started;
    try { apply(); } catch (error) {
      for (const id of ids) this.pending.delete(id);
      this.dropped += ids.length;
      throw error;
    }
    const completed = this.now();
    for (const id of ids) {
      const sample = this.pending.get(id)!;
      sample.completed = completed;
    }
    this.flushReady();
  }

  commit(): void {
    if (!this.pending.size) return;
    const now = this.now();
    // Ink's legacy root may commit synchronously inside apply(). Capture that
    // commit now, then report once the handler has finished. Otherwise an idle
    // screen would appear to wait until an unrelated later render.
    for (const sample of this.pending.values()) {
      if (sample.handled !== undefined) sample.committed = now;
    }
    this.flushReady();
  }

  private flushReady(): void {
    let samples: Sample[] = [];
    const flush = () => {
      try { this.send(samples); } catch { this.dropped += samples.length; }
      samples = [];
    };
    for (const [id, sample] of this.pending) {
      if (sample.handled === undefined || sample.completed === undefined || sample.committed === undefined) continue;
      samples.push({
        trace_id: id, parse_ms: sample.parse,
        batch_ms: Math.max(0, sample.handled - sample.received - sample.parse),
        handle_ms: Math.max(0, sample.completed - sample.handled),
        react_commit_ms: Math.max(0, sample.committed - sample.completed),
        receive_to_commit_ms: Math.max(0, sample.committed - sample.received),
      });
      this.pending.delete(id);
      if (samples.length === 128) flush();
    }
    if (samples.length) flush();
  }

  clear(): void {
    this.dropped += this.pending.size;
    this.pending.clear();
  }
}

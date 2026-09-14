import type { PyEvent } from "./types.js";
import { eventTraceIds } from "./runtime-timing.js";

type TextEvent = Extract<PyEvent, { type: "chunk" | "reasoning" }>;
type Schedule = (callback: () => void) => () => void;

/** Merge token bursts before parsing Markdown; controls are synchronous barriers. */
export function createTextEventBatcher(
  handle: (event: PyEvent) => void,
  failed: (event: TextEvent, error: unknown) => void,
  schedule: Schedule = (callback) => {
    const timer = setTimeout(callback, 32);
    return () => clearTimeout(timer);
  },
) {
  let kind: TextEvent["type"] | null = null;
  let pieces: string[] = [];
  let traces: string[] = [];
  let size = 0;
  let cancel: (() => void) | undefined;
  const discard = () => {
    cancel?.(); cancel = undefined;
    kind = null; pieces = []; traces = []; size = 0;
  };
  const flush = () => {
    const event = kind ? { type: kind, content: pieces.join(""),
      ...(traces.length ? { performance_trace_ids: traces } : {}) } as TextEvent : null;
    discard();
    if (event) {
      try { handle(event); } catch (error) {
        try { failed(event, error); } catch { /* The next lifecycle event still runs. */ }
      }
    }
  };
  const accept = (event: PyEvent) => {
    if (event.type !== "chunk" && event.type !== "reasoning") {
      flush();
      handle(event);
      return;
    }
    if (kind && kind !== event.type) flush();
    kind = event.type;
    pieces.push(event.content);
    traces.push(...eventTraceIds(event));
    size += event.content.length;
    if (size >= 65536 || traces.length >= 128) flush();
    else if (!cancel) cancel = schedule(flush);
  };
  return { accept, flush, discard };
}

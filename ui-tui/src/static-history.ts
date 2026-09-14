export function limitRestoredHistory<T>(lines: T[], maxLines: number): T[] {
  if (lines.length <= maxLines) return lines;
  return lines.slice(lines.length - maxLines);
}

export function restoreRecentHistory<TMessage, TLine>(
  messages: TMessage[],
  maxLines: number,
  toLines: (message: TMessage, index: number) => TLine[],
): TLine[] {
  const boundedMax = Math.max(0, maxLines);
  if (boundedMax === 0 || messages.length === 0) return [];

  const chunks: TLine[][] = [];
  let lineCount = 0;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const lines = toLines(messages[index], index);
    chunks.push(lines);
    lineCount += lines.length;
    if (lineCount >= boundedMax) break;
  }

  const restored = chunks.reverse().flat();
  return limitRestoredHistory(restored, boundedMax);
}

// Ink <Static> advances an internal cursor from items.length. Live items must
// therefore remain append-only for the lifetime of a mounted <Static>.
export function appendLiveHistory<T>(current: T[], incoming: T[]): T[] {
  if (incoming.length === 0) return current;
  return [...current, ...incoming];
}

// Only use this appender within one synchronous reducer invocation. Arrays it
// creates are private to that invocation until the final state is returned.
// Published states and caller-owned replacement arrays are copied on first use.
export function createLiveHistoryBatchAppender(): typeof appendLiveHistory {
  const owned = new WeakSet<unknown[]>();
  return <T>(current: T[], incoming: T[]): T[] => {
    if (incoming.length === 0) return current;
    const next = owned.has(current) ? current : [...current];
    owned.add(next);
    // Avoid the argument-count limit of push(...incoming) for large histories.
    const count = incoming.length;
    for (let index = 0; index < count; index += 1) next.push(incoming[index]);
    return next;
  };
}

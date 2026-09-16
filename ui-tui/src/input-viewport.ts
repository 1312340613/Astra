import stringWidth from "string-width";

const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });

/** A display-only single-row viewport. Offsets always refer to the full draft. */
export function inputViewport(value: string, cursor: number, columns: number) {
  const parts = [...segmenter.segment(value)];
  const offset = Math.max(0, Math.min(cursor, value.length));
  const point = parts.findIndex(part => part.index >= offset);
  const index = point < 0 ? parts.length : point;
  // Reserve two clipping markers and one cell for the end-of-input cursor.
  const budget = Math.max(1, columns - 3);
  const widths = parts.map(part => stringWidth(part.segment));
  let start = index;
  let end = index;
  let used = widths[index] ?? 1;
  if (index < parts.length) end++;
  while (start > 0 && used + widths[start - 1]! <= budget) used += widths[--start]!;
  while (end < parts.length && used + widths[end]! <= budget) used += widths[end++]!;
  const first = parts[start]?.index ?? value.length;
  const last = parts[end]?.index ?? value.length;
  const prefix = start > 0 ? "…" : "";
  return {
    value: prefix + value.slice(first, last) + (end < parts.length ? "…" : ""),
    cursor: prefix.length + offset - first,
  };
}

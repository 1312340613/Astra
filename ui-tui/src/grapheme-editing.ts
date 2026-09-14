const graphemeSegmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });

export function graphemeBoundary(value: string, cursor: number, direction: -1 | 1): number {
  const boundaries = [...graphemeSegmenter.segment(value)].map((part) => part.index);
  boundaries.push(value.length);
  return direction === -1
    ? (boundaries.filter((boundary) => boundary < cursor).at(-1) ?? 0)
    : (boundaries.find((boundary) => boundary > cursor) ?? value.length);
}

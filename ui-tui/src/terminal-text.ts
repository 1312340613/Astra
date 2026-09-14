import stringWidth from "string-width";

export type TerminalToken = {
  text: string;
  width: number;
};

const graphemeSegmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });
const ansiSequence = /\x1b(?:\[[0-?]*[ -/]*[@-~]|\](?:(?!\x07|\x1b\\)[\s\S])*(?:\x07|\x1b\\)|[ -/]*[@-~])/y;

export function hasAmbiguousTerminalWidth(source: string): boolean {
  for (const item of graphemeSegmenter.segment(source)) {
    const cluster = item.segment;
    const pictographic = /\p{Extended_Pictographic}/u.test(cluster);
    const regionalFlag = /^(?:\p{Regional_Indicator}){2}$/u.test(cluster);
    if (pictographic || regionalFlag) continue;
    if (cluster.includes("\u200d")) return true;

    const hasMark = /\p{Mark}/u.test(cluster);
    const hasLatinBase = /\p{Script=Latin}/u.test(cluster);
    const componentWidth = [...cluster].reduce((total, character) => total + stringWidth(character), 0);
    if (
      hasMark
      && !hasLatinBase
      && stringWidth(cluster) !== componentWidth
    ) return true;
  }
  return false;
}

function incompleteAnsiSuffix(source: string, cursor: number): string {
  if (source.charCodeAt(cursor) !== 0x1b) return "";
  const suffix = source.slice(cursor);
  if (suffix === "\x1b") return suffix;
  if (suffix.startsWith("\x1b]")) {
    for (let index = 2; index < suffix.length; index += 1) {
      if (suffix[index] === "\x07" || (suffix[index] === "\x1b" && suffix[index + 1] === "\\")) return "";
    }
    return suffix;
  }
  if (/^\x1b\[[0-?]*[ -/]*$/u.test(suffix)) return suffix;
  if (/^\x1b[ -/]*$/u.test(suffix)) return suffix;
  return "";
}

function isControlCharacter(value: string): boolean {
  const codePoint = value.codePointAt(0) ?? 0;
  return codePoint <= 0x1f || (codePoint >= 0x7f && codePoint <= 0x9f);
}

export function terminalTokens(source: string): TerminalToken[] {
  const tokens: TerminalToken[] = [];
  let pendingControls = "";
  let cursor = 0;

  const appendVisible = (visible: string) => {
    for (const item of graphemeSegmenter.segment(visible)) {
      tokens.push({
        text: pendingControls + item.segment,
        width: stringWidth(item.segment),
      });
      pendingControls = "";
    }
  };

  while (cursor < source.length) {
    const incompleteAnsi = incompleteAnsiSuffix(source, cursor);
    if (incompleteAnsi) {
      pendingControls += incompleteAnsi;
      cursor = source.length;
      continue;
    }

    ansiSequence.lastIndex = cursor;
    const ansi = ansiSequence.exec(source);
    if (ansi?.index === cursor) {
      pendingControls += ansi[0];
      cursor += ansi[0].length;
      continue;
    }

    const character = String.fromCodePoint(source.codePointAt(cursor) ?? 0);
    if (isControlCharacter(character)) {
      pendingControls += character;
      cursor += character.length;
      continue;
    }

    let visibleEnd = cursor + character.length;
    while (visibleEnd < source.length) {
      ansiSequence.lastIndex = visibleEnd;
      if (ansiSequence.exec(source)?.index === visibleEnd) break;
      const next = String.fromCodePoint(source.codePointAt(visibleEnd) ?? 0);
      if (isControlCharacter(next)) break;
      visibleEnd += next.length;
    }
    appendVisible(source.slice(cursor, visibleEnd));
    cursor = visibleEnd;
  }

  if (pendingControls) {
    const previous = tokens.at(-1);
    if (previous) previous.text += pendingControls;
    else tokens.push({ text: pendingControls, width: 0 });
  }
  return tokens;
}

export function terminalWidth(source: string): number {
  return terminalTokens(source).reduce((total, token) => total + token.width, 0);
}

function rawProgressCut(source: string, maxChars: number): number {
  let cut = Math.min(source.length, Math.max(1, maxChars));
  const previous = source.charCodeAt(cut - 1);
  const next = source.charCodeAt(cut);
  if (previous >= 0xd800 && previous <= 0xdbff && next >= 0xdc00 && next <= 0xdfff) {
    cut -= 1;
  }
  return Math.max(1, cut);
}

// Find the largest UTF-16 cut that ends on a shared terminal-token boundary.
// When the source itself fills the buffer, continuation lets the trailing token
// be re-segmented across a chunk boundary without ever joining two full buffers.
export function terminalSafeCut(
  source: string,
  maxChars: number,
  continuation = "",
  holdTrailingToken = false,
): number {
  if (!source || maxChars <= 0) return 0;
  const tokens = terminalTokens(source);
  let cursor = 0;
  let boundary = 0;
  for (const token of tokens) {
    cursor += token.text.length;
    if (cursor > maxChars) break;
    boundary = cursor;
  }

  if (source.length > maxChars) {
    return boundary || rawProgressCut(source, maxChars);
  }

  const trailing = tokens.at(-1);
  if (!trailing) return 0;
  const trailingStart = source.length - trailing.text.length;
  if (!continuation) {
    return holdTrailingToken ? trailingStart : boundary;
  }

  const continuationCapacity = maxChars - trailing.text.length;
  if (continuationCapacity <= 0) return trailingStart;
  // The boundary window is capped at maxChars by construction.
  const boundaryWindow = trailing.text + continuation.slice(0, continuationCapacity);
  let windowCursor = 0;
  let safeTrailingBoundary = 0;
  for (const token of terminalTokens(boundaryWindow)) {
    windowCursor += token.text.length;
    if (windowCursor > trailing.text.length) break;
    safeTrailingBoundary = windowCursor;
  }
  return trailingStart + safeTrailingBoundary;
}

export function terminalTextFragments(source: string, maxChars: number): string[] {
  if (!source) return [];
  if (maxChars <= 0) throw new RangeError("maxChars must be positive");
  const fragments: string[] = [];
  let current = "";

  const flush = () => {
    if (!current) return;
    fragments.push(current);
    current = "";
  };

  for (const token of terminalTokens(source)) {
    if (token.text.length > maxChars) {
      flush();
      let rest = token.text;
      while (rest) {
        const cut = rawProgressCut(rest, maxChars);
        fragments.push(rest.slice(0, cut));
        rest = rest.slice(cut);
      }
      continue;
    }
    if (current.length + token.text.length > maxChars) flush();
    current += token.text;
  }
  flush();
  return fragments;
}
